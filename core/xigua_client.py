# -*- coding: utf-8 -*-
"""西瓜邮箱接码平台客户端（https://xigua.dasiqo.com/#docs）。

西瓜按“订单”出码，和随机生成邮箱类服务不同：

1. 用 API Key 选择项目下的邮箱方案（offer），``POST /api/v1/orders`` 创建订单并
   拿到交付邮箱；
2. 订单处于 ``waiting`` 时由平台收信，收到一个有效新码后变为 ``succeeded``，
   ``GET /api/v1/orders/{id}`` 返回 ``code``；
3. 一个订单只结算一个验证码。同一邮箱再次取码（登录、查活、OTP 重发）需要调用
   原订单的 ``/repeat`` 创建新订单；首次成功付费后再次取码免费；
4. 未收到验证码的等待订单可 ``/cancel`` 释放冻结余额。

平台不会用订单创建前的旧邮件结算新订单，所以首次订单在领取邮箱时就创建；之后的
再次取码在等待验证码时才创建，并尽量紧跟在 OpenAI 发码之后。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://xigua.dasiqo.com"
REQUEST_TIMEOUT = 20
# 文档建议每 5–10 秒查询一次订单。
MIN_POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 30
_CREATE_ATTEMPTS = 3
_CODE_RE = re.compile(r"\b(\d{6})\b")
_STATE_WAITING = "waiting"
_STATE_SUCCEEDED = "succeeded"


class XiguaError(RuntimeError):
    """西瓜 API 请求、下单或取码失败。"""

    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class XiguaAccount:
    """一个邮箱当前订单的取码上下文。"""

    email: str
    order_id: str
    offer_id: str = ""
    # 当前订单上的验证码是否已经交给调用方；已交出的码不会再次返回。
    consumed: bool = False


_CONTEXT_CACHE: dict[str, XiguaAccount] = {}
_CONTEXT_LOCK = threading.RLock()


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url(value: str | None = None) -> str:
    """返回 API 根地址，兼容直接粘贴文档地址 ``https://xigua.dasiqo.com/#docs``。"""
    raw = str(
        value if value is not None else getattr(_email_cfg, "XIGUA_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE
    ).strip()
    if not raw:
        raw = DEFAULT_API_BASE
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw

    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise XiguaError("西瓜 API 地址无效，请填写 https://xigua.dasiqo.com（不要填写接口路径）")

    path = parsed.path.rstrip("/")
    for suffix in ("/docs", "/api/v1", "/api"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _request_timeout() -> int:
    try:
        value = int(getattr(_email_cfg, "XIGUA_REQUEST_TIMEOUT", REQUEST_TIMEOUT) or REQUEST_TIMEOUT)
    except (TypeError, ValueError):
        value = REQUEST_TIMEOUT
    return max(1, min(120, value))


def _api_key() -> str:
    value = str(getattr(_email_cfg, "XIGUA_API_KEY", "") or "").strip()
    if not value:
        raise XiguaError("西瓜 API Key 未配置，请在配置 → 邮箱 / OTP 填写 XIGUA_API_KEY")
    return value


def _error_detail(payload, response) -> tuple[str, str]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or ""), str(error.get("message") or "")
        if isinstance(error, str):
            return "", error
        message = payload.get("message") or payload.get("detail")
        if message:
            return "", str(message)
    text = str(getattr(response, "text", "") or "").strip()
    return "", (text[:240] if text else "服务端未返回错误信息")


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    idempotency_key: str | None = None,
):
    """调用西瓜客户 API 并返回 JSON payload。

    带 ``idempotency_key`` 的建单请求在网络错误或 5xx 时用同一个 key 重试，
    取回已创建的订单，避免重复扣费。
    """
    method = method.upper()
    headers = {"Accept": "application/json", "Authorization": f"Bearer {_api_key()}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    url = _base_url() + (path if str(path).startswith("/") else f"/{path}")
    attempts = _CREATE_ATTEMPTS if idempotency_key else 1
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=_request_timeout(),
            )
        except requests.RequestException as exc:
            if attempt < attempts:
                logger.warning("[西瓜] %s %s 网络异常，使用同一 Idempotency-Key 重试: %s", method, path, exc)
                time.sleep(1)
                continue
            raise XiguaError(f"西瓜请求失败 ({method} {path}): {type(exc).__name__}: {exc}") from exc

        if response.status_code >= 500 and attempt < attempts:
            logger.warning("[西瓜] %s %s 返回 HTTP %s，使用同一 Idempotency-Key 重试", method, path, response.status_code)
            time.sleep(1)
            continue
        break

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if response.status_code >= 400:
        code, message = _error_detail(payload, response)
        if response.status_code in (401, 403):
            message = f"西瓜 API Key 无效或无权限: {message}"
        raise XiguaError(
            f"西瓜请求失败 ({method} {path}): HTTP {response.status_code}"
            + (f" [{code}]" if code else "")
            + f"; {message}",
            status=response.status_code,
            code=code,
        )
    if payload is None:
        raise XiguaError(f"西瓜响应不是 JSON ({method} {path})", status=response.status_code)
    return payload


def _unwrap_object(payload, *keys: str) -> dict:
    if isinstance(payload, dict):
        if "id" in payload:
            return payload
        for key in keys:
            value = payload.get(key)
            if isinstance(value, dict):
                return value
    raise XiguaError("西瓜响应缺少订单数据")


def _unwrap_list(payload, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _order_from_payload(payload) -> dict:
    order = _unwrap_object(payload, "order", "data")
    if not str(order.get("id") or "").strip():
        raise XiguaError("西瓜订单响应缺少订单 id")
    return order


def _parse_timestamp(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value / 1000.0 if value > 10_000_000_000 else value
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ------------------------------------------------------------
# 项目 / 方案
# ------------------------------------------------------------

def list_projects() -> list[dict]:
    """查询项目及邮箱方案，供配置/诊断使用。"""
    return _unwrap_list(_request("GET", "/api/v1/projects"), "projects", "items", "data")


def _resolve_offer_id() -> str:
    """按配置选出下单用的方案 ID。

    填了 ``XIGUA_OFFER_ID`` 时直接使用；否则在 ``XIGUA_PROJECT``（slug/名称/ID）下选
    启用且有库存的方案，可用 ``XIGUA_MAILBOX_TYPE`` 限定邮箱类型，多个时取最便宜。
    """
    offer_id = str(getattr(_email_cfg, "XIGUA_OFFER_ID", "") or "").strip()
    if offer_id:
        return offer_id

    wanted = str(getattr(_email_cfg, "XIGUA_PROJECT", "openai") or "openai").strip().casefold()
    mailbox_type = str(getattr(_email_cfg, "XIGUA_MAILBOX_TYPE", "") or "").strip().casefold()
    projects = list_projects()
    project = next(
        (
            item for item in projects
            if wanted in {
                str(item.get(key) or "").strip().casefold() for key in ("slug", "name", "id")
            }
        ),
        None,
    )
    if project is None:
        names = ", ".join(str(item.get("slug") or item.get("name") or "") for item in projects[:20])
        raise XiguaError(f"西瓜找不到项目 {wanted!r}，可用项目: {names or '无'}")
    if project.get("enabled") is False:
        raise XiguaError(f"西瓜项目 {project.get('name') or wanted} 暂未开放")

    offers = [
        offer for offer in (project.get("offers") or [])
        if isinstance(offer, dict)
        and offer.get("enabled") is not False
        and str(offer.get("id") or "").strip()
        and (not mailbox_type or str(offer.get("mailbox_type") or "").strip().casefold() == mailbox_type)
    ]
    if not offers:
        raise XiguaError(
            f"西瓜项目 {project.get('name') or wanted} 没有可用方案"
            + (f"（邮箱类型 {mailbox_type}）" if mailbox_type else "")
        )

    def _stock(offer: dict) -> int:
        try:
            return int(offer.get("stock") or 0)
        except (TypeError, ValueError):
            return 0

    def _price(offer: dict) -> int:
        try:
            return int(offer.get("price_cents") or 0)
        except (TypeError, ValueError):
            return 0

    in_stock = [offer for offer in offers if _stock(offer) > 0]
    if not in_stock:
        raise XiguaError(f"西瓜项目 {project.get('name') or wanted} 的方案暂无库存")
    in_stock.sort(key=_price)
    return str(in_stock[0]["id"]).strip()


# ------------------------------------------------------------
# 订单
# ------------------------------------------------------------

def _new_idempotency_key() -> str:
    return f"turb-gpt-free-register-{uuid.uuid4()}"


def _get_order(order_id: str) -> dict:
    return _order_from_payload(_request("GET", f"/api/v1/orders/{quote(order_id, safe='')}"))


def _list_orders() -> list[dict]:
    return _unwrap_list(_request("GET", "/api/v1/orders"), "orders", "items", "data")


def _latest_order_for_email(email: str, *, state: str | None = None) -> dict | None:
    """在最近 100 笔订单中找该邮箱最新的一笔（邮箱完全匹配）。"""
    target = str(email or "").strip().casefold()
    candidates = [
        order for order in _list_orders()
        if str(order.get("email") or "").strip().casefold() == target
        and str(order.get("id") or "").strip()
        and (state is None or str(order.get("state") or "").strip().lower() == state)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda order: _parse_timestamp(order.get("created_at")) or float("-inf"), reverse=True)
    return candidates[0]


def _cache_context(account: XiguaAccount) -> XiguaAccount:
    with _CONTEXT_LOCK:
        _CONTEXT_CACHE[_cache_key(account.email)] = account
    return account


def pick_account() -> XiguaAccount:
    """按配置的项目方案创建订单并返回交付邮箱。"""
    offer_id = _resolve_offer_id()
    order = _order_from_payload(
        _request(
            "POST",
            "/api/v1/orders",
            json_body={"offer_id": offer_id},
            idempotency_key=_new_idempotency_key(),
        )
    )
    email = str(order.get("email") or "").strip()
    if "@" not in email:
        raise XiguaError(f"西瓜订单 {order.get('id')} 未返回邮箱地址")
    account = _cache_context(
        XiguaAccount(email=email, order_id=str(order["id"]).strip(), offer_id=offer_id)
    )
    logger.info(
        "[西瓜] 已创建邮箱订单: %s order=%s price_cents=%s expires_at=%s",
        email,
        account.order_id,
        order.get("price_cents"),
        order.get("expires_at") or "-",
    )
    return account


def get_email() -> str:
    """兼容其他临时邮箱客户端的旧入口。"""
    return pick_account().email


def get_account_context(email: str) -> XiguaAccount | None:
    with _CONTEXT_LOCK:
        return _CONTEXT_CACHE.get(_cache_key(email))


def _repeat_order(account: XiguaAccount) -> XiguaAccount:
    """为同一邮箱、同一方案创建新订单，用于再次取码。"""
    try:
        order = _order_from_payload(
            _request(
                "POST",
                f"/api/v1/orders/{quote(account.order_id, safe='')}/repeat",
                json_body={},
                idempotency_key=_new_idempotency_key(),
            )
        )
    except XiguaError as exc:
        # 同一邮箱同一时间只处理一笔等待订单；若已有等待订单（例如另一进程
        # 刚创建），直接接管它而不是失败。
        if exc.status != 409:
            raise
        waiting = _latest_order_for_email(account.email, state=_STATE_WAITING)
        if waiting is None:
            raise
        order = waiting
        logger.info("[西瓜] 已存在等待中的订单，直接接管: %s order=%s", account.email, order.get("id"))

    email = str(order.get("email") or account.email).strip()
    if email.casefold() != account.email.casefold():
        raise XiguaError(f"西瓜再次取码返回了不同邮箱: {email}")
    previous = account.order_id
    account.order_id = str(order["id"]).strip()
    account.offer_id = str(order.get("offer_id") or account.offer_id or "").strip()
    account.consumed = False
    logger.info(
        "[西瓜] 已为同一邮箱创建再次取码订单: %s order=%s previous=%s price_cents=%s",
        account.email,
        account.order_id,
        previous,
        order.get("price_cents"),
    )
    return account


def _saved_context_metadata(email: str) -> dict:
    """读取账号 extra_json 中保存的西瓜订单信息，失败时不影响查活主流程。"""
    try:
        from core import db

        row = db.get_account_by_email(email)
    except Exception as exc:
        logger.debug("[西瓜] 读取已注册账号订单信息失败: %s: %s", type(exc).__name__, exc)
        return {}
    if not row:
        return {}
    raw = row.get("extra_json")
    if isinstance(raw, str) and raw.strip():
        try:
            extra = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    elif isinstance(raw, dict):
        extra = raw
    else:
        return {}
    value = extra.get("email_service") if isinstance(extra, dict) else None
    if isinstance(value, dict) and str(value.get("source") or "").strip().lower() == "xigua":
        return dict(value)
    return {}


def get_account_context_metadata(email: str) -> dict | None:
    """返回可持久化的西瓜订单上下文，用于注册账号落库。"""
    account = get_account_context(email)
    if account is None:
        return None
    return {
        "source": "xigua",
        "email": account.email,
        "order_id": account.order_id,
        "offer_id": account.offer_id,
    }


def restore_account_context(email: str) -> XiguaAccount | None:
    """进程重启后恢复已注册账号的订单上下文。

    优先使用账号保存的订单号，其次按邮箱在最近订单中查找。恢复出的订单视为
    已交码：若它不是等待中的订单，取码时会通过 ``/repeat`` 创建新订单。
    """
    target = str(email or "").strip()
    if not target:
        return None
    cached = get_account_context(target)
    if cached is not None:
        return cached

    metadata = _saved_context_metadata(target)
    saved_email = str(metadata.get("email") or "").strip()
    order_id = str(metadata.get("order_id") or "").strip()
    if order_id and (not saved_email or saved_email.casefold() == target.casefold()):
        logger.info("[西瓜] 已从账号保存信息恢复订单上下文: %s order=%s", target, order_id)
        return _cache_context(
            XiguaAccount(
                email=saved_email or target,
                order_id=order_id,
                offer_id=str(metadata.get("offer_id") or "").strip(),
                consumed=True,
            )
        )

    try:
        order = _latest_order_for_email(target)
    except XiguaError as exc:
        logger.debug("[西瓜] 按邮箱查找订单失败: email=%s error=%s", target, exc)
        return None
    if order is None:
        return None
    logger.info("[西瓜] 已按邮箱查找恢复订单上下文: %s order=%s", target, order.get("id"))
    return _cache_context(
        XiguaAccount(
            email=str(order.get("email") or target).strip(),
            order_id=str(order["id"]).strip(),
            offer_id=str(order.get("offer_id") or "").strip(),
            consumed=True,
        )
    )


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """释放本地上下文；仍在等待验证码的订单会被取消以释放冻结余额。"""
    with _CONTEXT_LOCK:
        account = _CONTEXT_CACHE.pop(_cache_key(email), None)
    if account is None:
        return
    cancelled = False
    try:
        order = _get_order(account.order_id)
        if str(order.get("state") or "").strip().lower() == _STATE_WAITING:
            _request("POST", f"/api/v1/orders/{quote(account.order_id, safe='')}/cancel", json_body={})
            cancelled = True
    except XiguaError as exc:
        logger.warning("[西瓜] 取消等待订单失败: %s order=%s error=%s", email, account.order_id, exc)
    logger.info(
        "[西瓜] 已释放订单上下文: %s order=%s cancelled=%s status=%s note=%s",
        email,
        account.order_id,
        cancelled,
        status,
        note or "",
    )


def _order_code(order: dict) -> str | None:
    raw = str(order.get("code") or "").strip()
    if not raw:
        return None
    match = _CODE_RE.search(raw)
    return match.group(1) if match else raw


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询当前订单，返回一个尚未交出的验证码。

    当前订单的码已交出、或订单已取消/过期/退款时，先用 ``/repeat`` 创建新订单
    再等待。``after_ts``/``settle_seconds`` 为兼容统一签名：每个西瓜订单只会结算
    订单创建后的一个新码，无需再按时间过滤或等待更晚的邮件。
    """
    target = str(email or "").strip()
    if not target:
        raise XiguaError("西瓜取码缺少邮箱地址")
    account = get_account_context(target) or restore_account_context(target)
    if account is None:
        raise XiguaError(
            "西瓜找不到该邮箱的订单，且无法从已保存信息或最近订单恢复；"
            "请确认账号注册来源为西瓜并已配置西瓜 API Key"
        )

    try:
        wait_seconds = int(max_wait if max_wait is not None else getattr(_email_cfg, "OTP_MAX_WAIT", 90))
    except (TypeError, ValueError):
        wait_seconds = 90
    try:
        interval = int(poll_interval if poll_interval is not None else getattr(_email_cfg, "OTP_POLL_INTERVAL", 3))
    except (TypeError, ValueError):
        interval = MIN_POLL_INTERVAL
    interval = max(MIN_POLL_INTERVAL, interval)
    deadline = time.monotonic() + max(0, wait_seconds)
    last_error = "订单仍在等待验证码"

    logger.info("[西瓜] 开始轮询订单 %s（%s），最长 %ss", account.order_id, target, wait_seconds)
    while True:
        try:
            order = _get_order(account.order_id)
            state = str(order.get("state") or "").strip().lower()
            if state == _STATE_WAITING:
                account.consumed = False
                last_error = f"订单 {account.order_id} 仍在等待验证码"
            elif state == _STATE_SUCCEEDED and not account.consumed and _order_code(order):
                account.consumed = True
                logger.info("[西瓜] 订单 %s 已收到验证码（%s）", account.order_id, target)
                return _order_code(order)
            else:
                # 已交出的码、取消/过期/退款的订单都不能再用于本次取码。
                if after_ts is not None and time.time() - after_ts > 15:
                    logger.warning(
                        "[西瓜] 距离发码已 %.0fs 才创建再次取码订单，平台可能不会用订单前的邮件结算",
                        time.time() - after_ts,
                    )
                logger.info("[西瓜] 订单 %s 状态 %s，为同一邮箱再次取码", account.order_id, state or "unknown")
                _repeat_order(account)
                last_error = f"订单 {account.order_id} 仍在等待验证码"
        except XiguaError as exc:
            # 参数/鉴权/订单不存在/余额或状态冲突，重试也不会恢复。
            if exc.status in (400, 401, 403, 404, 409):
                raise
            last_error = str(exc)
            if exc.status == 429:
                interval = min(MAX_POLL_INTERVAL, interval * 2)
                logger.warning("[西瓜] 查询被限流，轮询间隔调整为 %ss", interval)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    raise XiguaError(f"等待西瓜验证码超时: {target}; {last_error}")
