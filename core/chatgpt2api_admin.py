# -*- coding: utf-8 -*-
"""Redacted management adapter for ChatGPT2API account replacement."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from config import chatgpt2api as _cfg

_MAX_ID = 160
_MAX_REASON = 240
_MAX_PAGE_SIZE = 500
_MAX_PAGES = 20
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_INVALID_RE = re.compile(
    r"(?:\b401\b|unauthori[sz]ed|invalid[_ -]?(?:access[_ -]?)?token|token[_ -]?expired|expired[_ -]?(?:access[_ -]?)?token)",
    re.I,
)
_SECRET_RE = re.compile(
    r"(?i)([\"']?\b(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|authorization|bearer|"
    r"api[_ -]?key|password|secret|callback(?:_url)?|redirect_uri|oauth[_ -]?code|code)\b[\"']?)"
    r"\s*(?::|=|\s)\s*[\"']?[^\s,;}\]\"']+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_RE = re.compile(r"https?://[^\s,;}\]\\\"'，。；！？、）】》]+", re.I)
_TOKENISH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{28,}(?:\.[A-Za-z0-9_+/=-]{8,})*")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Only refresh-token evidence has a dedicated endpoint. Email/identity mapping is
# verified from sanitized list rows; other credential fields are not guessed.
_PRESERVED_FIELDS = ("refresh_token",)


def _text(value: object, limit: int) -> str:
    raw = "" if value is None else str(value)
    return " ".join(raw.replace("\x00", "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _safe_reason(value: object, fallback: str = "ChatGPT2API 请求失败") -> str:
    text = _text(value, 1200)
    text = _SECRET_RE.sub(r"\1=[redacted]", text)
    text = _BEARER_RE.sub("bearer [redacted]", text)
    text = _URL_RE.sub("[url-redacted]", text)
    text = _TOKENISH_RE.sub("[token-redacted]", text)
    return (text or fallback)[:_MAX_REASON]


def _remote_id(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return _text(value, _MAX_ID)


def _email(value: object) -> str:
    text = _text(value, 320).lower()
    return text if _EMAIL_RE.fullmatch(text) else ""


def _sha256(value: object) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _containers(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    result: list[dict] = []
    pending = [payload]
    seen: set[int] = set()
    while pending:
        container = pending.pop(0)
        identity = id(container)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(container)
        for key in (
            "data", "item", "account", "result", "credential", "credentials",
            "oauth", "auth", "user", "metadata", "meta", "pagination",
        ):
            value = container.get(key)
            if isinstance(value, dict):
                pending.append(value)
    return result


def _rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    for container in _containers(payload):
        for key in ("items", "accounts", "records", "rows", "list", "data"):
            value = container.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _first_value(containers: list[dict], key: str) -> tuple[bool, object]:
    aliases = {
        "access_token": ("access_token", "accessToken", "token"),
        "refresh_token": ("refresh_token", "refreshToken"),
        "client_id": ("client_id", "clientId"),
        "last_refresh": ("last_refresh", "lastRefresh"),
    }.get(key, (key,))
    for container in containers:
        for alias in aliases:
            if alias in container:
                return True, container.get(alias)
    return False, None


def _first_text(containers: list[dict], keys: tuple[str, ...], limit: int) -> str:
    for container in containers:
        for key in keys:
            if key in container:
                value = _text(container.get(key), limit)
                if value:
                    return value
    return ""


def _digest(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _SHA256_RE.fullmatch(text) else ""


def _sanitize_account(row: dict) -> dict:
    containers = _containers(row)
    remote_id = _remote_id(_first_text(containers, ("id", "management_id", "account_id"), _MAX_ID))
    candidates: list[str] = []
    for container in containers:
        for key in ("email", "account_email", "username"):
            candidate = _email(container.get(key))
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    email = candidates[0] if len(candidates) == 1 else ""
    access_status = _first_text(
        containers,
        ("access_token_status", "token_status", "access_status", "status"),
        48,
    ).lower()
    remote_result = _first_text(
        containers,
        ("last_remote_check_result", "remote_check_result", "last_remote_check_status", "check_result"),
        48,
    ).lower()
    raw_reason = _first_text(
        containers,
        ("status_reason", "status_raw_error", "last_remote_check_error", "error", "last_error", "message"),
        1200,
    )
    reason = _safe_reason(raw_reason, "")
    invalid = (
        access_status in {"invalid", "expired", "token_expired", "unauthorized", "401"}
        or remote_result in {"invalid", "expired", "token_expired", "unauthorized", "401"}
        or bool(_INVALID_RE.search(access_status))
        or bool(_INVALID_RE.search(remote_result))
        or bool(_INVALID_RE.search(raw_reason))
    )
    return {
        "remote_id": remote_id,
        "email": email,
        "email_ambiguous": len(candidates) > 1,
        "access_status": access_status,
        "remote_check_result": remote_result,
        "reason": reason,
        "invalid": invalid,
    }


class ChatGPT2APIAdminError(RuntimeError):
    """Bounded management error that never includes credentials or response bodies."""

    def __init__(self, message: str, *, status_code: int = 502, category: str = "remote") -> None:
        self.status_code = int(status_code)
        self.category = _text(category, 32) or "remote"
        super().__init__(_safe_reason(message))


class ChatGPT2APIAdminClient:
    """List accounts, fingerprint credentials, and update one stable remote ID."""

    def __init__(self, request_fn: Callable[[str, str, dict | None], object] | None = None) -> None:
        self._request_fn = request_fn
        # GET /api/accounts is sanitized; credentials are fetched only through
        # the dedicated secret endpoints and are never retained in list cache.
        self._listed_ids: set[str] = set()
        self._snapshot_by_id: dict[str, dict] = {}

    @staticmethod
    def _base_url() -> str:
        value = str(getattr(_cfg, "CHATGPT2API_BASE_URL", "") or "").strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ChatGPT2APIAdminError(
                "ChatGPT2API 地址未配置",
                status_code=503,
                category="configuration",
            )
        return value

    @staticmethod
    def _management_key() -> str:
        key = str(getattr(_cfg, "CHATGPT2API_MANAGEMENT_KEY", "") or "").strip()
        if not key:
            raise ChatGPT2APIAdminError(
                "ChatGPT2API 管理密钥未配置",
                status_code=503,
                category="configuration",
            )
        return key

    def _http_request(self, method: str, path: str, body: dict | None) -> object:
        key = self._management_key()
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base_url()}{path}",
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "User-Agent": "turb-gpt-free-register/chatgpt2api-401",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        timeout = max(1, int(getattr(_cfg, "CHATGPT2API_REQUEST_TIMEOUT", 60) or 60))
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            try:
                exc.close()
            except Exception:
                pass
            if status in {401, 403}:
                raise ChatGPT2APIAdminError("ChatGPT2API 管理接口认证失败", status_code=502, category="auth") from exc
            if status == 404 and method == "POST" and path == "/api/accounts/update":
                raise ChatGPT2APIAdminError("ChatGPT2API 不支持账号更新接口", status_code=501, category="unsupported") from exc
            if status == 404:
                raise ChatGPT2APIAdminError("ChatGPT2API 账号或接口不存在", status_code=404, category="not_found") from exc
            if status == 409:
                raise ChatGPT2APIAdminError("ChatGPT2API 账号状态冲突", status_code=409, category="conflict") from exc
            if status == 405:
                raise ChatGPT2APIAdminError("ChatGPT2API 不支持账号更新接口", status_code=501, category="unsupported") from exc
            if status == 429 or status >= 500:
                raise ChatGPT2APIAdminError("ChatGPT2API 管理接口暂时不可用", status_code=502, category="remote") from exc
            raise ChatGPT2APIAdminError("ChatGPT2API 管理请求被拒绝", status_code=502, category="remote") from exc
        except (URLError, TimeoutError, OSError) as exc:
            text = str(exc or "").lower()
            category = "timeout" if "timeout" in text or "timed out" in text else "network"
            status_code = 504 if category == "timeout" else 502
            raise ChatGPT2APIAdminError("ChatGPT2API 管理接口请求超时" if category == "timeout" else "ChatGPT2API 管理接口不可达", status_code=status_code, category=category) from exc
        if not 200 <= status < 300:
            raise ChatGPT2APIAdminError("ChatGPT2API 管理请求失败")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (TypeError, ValueError) as exc:
            raise ChatGPT2APIAdminError("ChatGPT2API 返回了无效 JSON") from exc

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        try:
            return self._request_fn(method, path, body) if self._request_fn else self._http_request(method, path, body)
        except ChatGPT2APIAdminError:
            raise
        except Exception as exc:
            text = str(exc or "").lower()
            if "timeout" in text or "timed out" in text:
                raise ChatGPT2APIAdminError("ChatGPT2API 管理接口请求超时", status_code=504, category="timeout") from exc
            raise ChatGPT2APIAdminError("ChatGPT2API 管理接口请求失败") from exc

    def list_accounts(self, *, page_size: int = _MAX_PAGE_SIZE, max_pages: int = _MAX_PAGES) -> list[dict]:
        page_size = max(1, min(_MAX_PAGE_SIZE, int(page_size or _MAX_PAGE_SIZE)))
        max_pages = max(1, min(_MAX_PAGES, int(max_pages or _MAX_PAGES)))
        result: list[dict] = []
        seen_pages: set[tuple[str, ...]] = set()
        listed_ids: set[str] = set()
        for page in range(1, max_pages + 1):
            query = urlencode({"page": page, "page_size": page_size})
            payload = self._request("GET", f"/api/accounts?{query}")
            rows = _rows(payload)
            signature = tuple(_remote_id(row.get("id") or row.get("management_id") or row.get("account_id")) for row in rows)
            if signature and signature in seen_pages:
                break
            if signature:
                seen_pages.add(signature)
            for raw in rows:
                item = _sanitize_account(raw)
                remote_id = item["remote_id"]
                if remote_id:
                    listed_ids.add(remote_id)
                result.append(item)
            containers = _containers(payload)
            total = next((int(c.get("total") or 0) for c in containers if str(c.get("total") or "").isdigit()), 0)
            total_pages = next((int(c.get("total_pages") or c.get("pages") or 0) for c in containers if str(c.get("total_pages") or c.get("pages") or "").isdigit()), 0)
            if total_pages and page >= total_pages:
                break
            if total and page * page_size >= total:
                break
            if len(rows) < page_size:
                break
        self._listed_ids = listed_ids
        return result

    def _secret_snapshot(self, remote_id: str) -> dict:
        """Fetch dedicated credential endpoints and return hash-only evidence."""
        remote_id = _remote_id(remote_id)
        if not remote_id:
            raise ChatGPT2APIAdminError("ChatGPT2API 账号 ID 无效", status_code=400, category="validation")
        encoded_id = quote(remote_id, safe="")
        access_payload = self._request("GET", f"/api/accounts/{encoded_id}/access-token")
        access_present, access_token = _first_value(_containers(access_payload), "access_token")
        access_text = str(access_token or "").strip() if access_present else ""
        if not access_text:
            raise ChatGPT2APIAdminError("ChatGPT2API 账号缺少可验证的 AT", status_code=409, category="conflict")

        # A missing RT is a valid explicit state. Preserve presence/value
        # semantics rather than guessing an empty token.
        refresh_present = True
        refresh_token = ""
        try:
            refresh_payload = self._request("GET", f"/api/accounts/{encoded_id}/refresh-token")
        except ChatGPT2APIAdminError as exc:
            if exc.category != "not_found":
                raise
            refresh_present = False
        else:
            refresh_key_present, refresh_value = _first_value(_containers(refresh_payload), "refresh_token")
            refresh_token = str(refresh_value or "").strip() if refresh_key_present else ""
            if not refresh_token:
                raise ChatGPT2APIAdminError(
                    "ChatGPT2API 返回了不可验证的 RT",
                    status_code=409,
                    category="conflict",
                )
            refresh_present = True

        return {
            "remote_id": remote_id,
            "at_sha256": hashlib.sha256(access_text.encode("utf-8")).hexdigest(),
            "preserved_sha256": {
                "refresh_token": _sha256({
                    "present": refresh_present,
                    "value": refresh_token if refresh_present else "",
                }),
            },
        }

    def credential_snapshot(self, remote_id: str) -> dict:
        remote_id = _remote_id(remote_id)
        if not remote_id:
            raise ChatGPT2APIAdminError("ChatGPT2API 账号 ID 无效", status_code=400, category="validation")
        snapshot = self._secret_snapshot(remote_id)
        self._snapshot_by_id[remote_id] = dict(snapshot)
        return snapshot

    def patch_access_token(self, remote_id: str, access_token: str) -> dict:
        remote_id = _remote_id(remote_id)
        token = str(access_token or "").strip()
        if not remote_id or not token:
            raise ChatGPT2APIAdminError("ChatGPT2API 更新参数无效", status_code=400, category="validation")
        # A mutation is only safe when the old secret evidence is locked first.
        # Direct adapter callers get the same precondition as the service.
        expected = self._snapshot_by_id.get(remote_id) or self.credential_snapshot(remote_id)
        payload = self._request(
            "POST",
            "/api/accounts/update",
            {"id": remote_id, "access_token": token},
        )
        updated_ids: list[str] = []
        for container in _containers(payload):
            values = container.get("updated_ids")
            if isinstance(values, list):
                updated_ids.extend(_remote_id(value) for value in values)
        if remote_id not in updated_ids:
            raise ChatGPT2APIAdminError("ChatGPT2API 未确认锁定账号已更新", status_code=409, category="conflict")

        # The acknowledgement alone is insufficient. Re-fetch AT and RT and
        # compare hash-only evidence. There is no ID-token getter in this API,
        # so this client makes no unsupported ID-token preservation claim.
        observed = self._secret_snapshot(remote_id)
        expected_at = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if observed["at_sha256"] != expected_at:
            raise ChatGPT2APIAdminError("ChatGPT2API 更新后的 AT 校验失败", status_code=409, category="conflict")
        if observed["preserved_sha256"].get("refresh_token") != expected["preserved_sha256"].get("refresh_token"):
            raise ChatGPT2APIAdminError("ChatGPT2API 更新后的 RT 保留校验失败", status_code=409, category="conflict")
        self._snapshot_by_id[remote_id] = observed
        return {"ok": True, "remote_id": remote_id}


__all__ = ["ChatGPT2APIAdminClient", "ChatGPT2APIAdminError"]
