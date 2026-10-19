# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import random
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse

from core.session import BrowserSession, close_browser_session

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
PAYMENT_CHECKOUT_PATH = "/backend-api/payments/checkout"
_PAYMENT_CURRENCY_BY_COUNTRY = {
    "US": "USD", "CA": "CAD", "GB": "GBP", "JP": "JPY", "CN": "CNY",
    "HK": "HKD", "TW": "TWD", "KR": "KRW", "SG": "SGD", "AU": "AUD",
    "NZ": "NZD", "IN": "INR", "ID": "IDR", "MY": "MYR", "PH": "PHP",
    "TH": "THB", "VN": "VND", "BR": "BRL", "MX": "MXN", "AR": "ARS",
    "CL": "CLP", "CO": "COP", "PE": "PEN", "ZA": "ZAR", "TR": "TRY",
    "IL": "ILS", "AE": "AED", "SA": "SAR", "EG": "EGP", "CH": "CHF",
    "PL": "PLN", "SE": "SEK", "NO": "NOK", "DK": "DKK", "CZ": "CZK",
    "HU": "HUF", "RO": "RON", "BG": "BGN", "UA": "UAH", "IS": "ISK",
}
_PAYMENT_EURO_COUNTRIES = frozenset({
    "AT", "BE", "CY", "DE", "EE", "ES", "FI", "FR", "GR", "HR", "IE",
    "IT", "LT", "LU", "LV", "MT", "NL", "PT", "SI", "SK",
})

PAYMENT_METHOD_LABELS = {
    "card": "银行卡",
    "link": "Link",
    "paypal": "PayPal",
    "cashapp": "Cash App",
    "apple_pay": "Apple Pay",
    "google_pay": "Google Pay",
    "amazon_pay": "Amazon Pay",
    "klarna": "Klarna",
    "afterpay_clearpay": "Afterpay / Clearpay",
    "us_bank_account": "美国银行账户",
    "sepa_debit": "SEPA 借记卡",
    "ideal": "iDEAL",
    "bancontact": "Bancontact",
    "sofort": "Sofort",
    "alipay": "支付宝",
    "wechat_pay": "微信支付",
    "upi": "UPI",
    "pix": "PIX",
    "momo": "MoMo",
    "gopay": "GoPay",
    "grabpay": "GrabPay",
    "gcash": "GCash",
    "blik": "BLIK",
    "twint": "TWINT",
}
PAYMENT_METHOD_GENERIC_LABEL = "其他支付方式"
_PAYMENT_METHOD_CHECKED = "checked"
_PAYMENT_METHOD_NOT_APPLICABLE = "not_applicable"
_PAYMENT_METHOD_ERROR = "error"
_PAYMENT_METHOD_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_PAYMENT_METHOD_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_PAYMENT_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_PAYMENT_METHOD_TYPE_ALIASES = {
    "credit_card": "card",
    "debit_card": "card",
    "creditcard": "card",
    "debitcard": "card",
    "applepay": "apple_pay",
    "googlepay": "google_pay",
    "amazonpay": "amazon_pay",
    "cash_app": "cashapp",
    "afterpay": "afterpay_clearpay",
    "clearpay": "afterpay_clearpay",
    "sepa": "sepa_debit",
}
_PAYMENT_METHOD_KEYS = {
    "payment_method_types", "payment_methods", "available_payment_methods",
    "supported_payment_methods", "methods", "custom_payment_methods",
    "payment_method_options", "payment_method_configurations",
}
_PAYMENT_AMOUNT_PATHS = (
    ("payment_amount",),
    ("checkout_amount_minor",),
    ("total_summary", "due"),
    ("invoice", "amount_due"),
    ("amount_due",),
    ("amount_total",),
    ("total", "due"),
    ("total", "total"),
    ("total",),
    ("amount",),
    ("unit_amount",),
)
_PAYMENT_CURRENCY_PATHS = (
    ("payment_currency",),
    ("currency",),
    ("currency_code",),
    ("billing_currency",),
    ("total_summary", "currency"),
    ("invoice", "currency"),
    ("total", "currency"),
)
_PAYMENT_SESSION_KEYS = {
    "payment_method_check_session_type", "payment_session_type", "session_type",
    "checkout_session_type", "payment_session", "session", "mode",
}
_PAYMENT_SENSITIVE_PREFIXES = (
    "sk_", "pk_", "rk_", "whsec_", "cs_", "pi_", "seti_", "tok_",
)
_PAYMENT_SENSITIVE_TEXT_RE = re.compile(
    r"(?:^|[_\-.])(access_token|refresh_token|authorization|bearer|client_secret|secret|cookie|"
    r"session(?:_id)?|payment_intent(?:_id)?|checkout(?:_session)?(?:_id)?)(?:$|[_\-.])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PaymentMethodCheckResult:
    """Bounded, presentation-ready result for one checkout initialization attempt."""

    payment_method_check_status: str
    payment_method_checked_at: str | None = None
    payment_method_error: str | None = None
    payment_method_types: tuple[str, ...] = ()
    payment_method_labels: tuple[str, ...] = ()
    payment_session_type: str | None = None
    payment_amount: int | float | None = None
    payment_currency: str | None = None

    def as_dict(self) -> dict:
        return {
            "payment_method_check_status": self.payment_method_check_status,
            "payment_method_checked_at": self.payment_method_checked_at,
            "payment_method_error": self.payment_method_error,
            "payment_method_types": list(self.payment_method_types),
            "payment_method_labels": list(self.payment_method_labels),
            "payment_session_type": self.payment_session_type,
            "payment_amount": self.payment_amount,
            "payment_currency": self.payment_currency,
        }

    to_dict = as_dict


# The comparison project calls this result model PaymentMethodResult. Keep the
# alias local so callers can use either descriptive name without another model.
PaymentMethodResult = PaymentMethodCheckResult


def _bounded_payment_text(value: object, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:limit]


def _payment_text_is_safe_identifier(value: str) -> bool:
    lowered = value.casefold()
    if "://" in value or "@" in value or any(ch.isspace() for ch in value):
        return False
    if lowered in {"id", "url", "session", "mode", "token", "secret", "cookie", "authorization"}:
        return False
    if lowered.startswith(_PAYMENT_SENSITIVE_PREFIXES):
        return False
    return _PAYMENT_SENSITIVE_TEXT_RE.search(lowered) is None


def _bounded_payment_identifier(value: object) -> str | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text or not _PAYMENT_SAFE_ID_RE.fullmatch(text):
        return None
    if not _payment_text_is_safe_identifier(text):
        return None
    return text[:96]


def normalize_payment_method_identifier(value: object) -> str | None:
    """Normalize a provider method name while retaining safe bounded unknowns."""
    text = _bounded_payment_text(value, 96)
    if not text or not _payment_text_is_safe_identifier(text):
        return None
    text = re.sub(r"[\s/-]+", "_", text.casefold())
    text = re.sub(r"[^a-z0-9_.-]", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.-")
    for prefix in ("payment_method_", "paymentmethod_"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = _PAYMENT_METHOD_TYPE_ALIASES.get(text, text)
    text = text[:64].rstrip("_.-")
    if not text or not _PAYMENT_METHOD_TYPE_RE.fullmatch(text):
        return None
    return text


def payment_method_label(method_type: object) -> str:
    normalized = normalize_payment_method_identifier(method_type)
    return PAYMENT_METHOD_LABELS.get(normalized or "", PAYMENT_METHOD_GENERIC_LABEL)


def _payment_nodes(value: object, depth: int = 0):
    if depth > 5:
        return
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _payment_nodes(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:32]:
            yield from _payment_nodes(child, depth + 1)


def _payment_key(value: object) -> str:
    """Normalize snake, kebab, and provider camelCase field names."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    return text.strip().casefold().replace("-", "_")


def _method_values(value: object) -> list[object]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value[:32]:
            out.extend(_method_values(item))
        return out
    if isinstance(value, dict):
        # OAICS custom methods may be objects while Stripe uses a list of
        # {type: ...}; map-shaped method options use their keys.
        direct = []
        for key in ("type", "method", "id", "code", "payment_method_type"):
            if isinstance(value.get(key), str):
                direct.append(value[key])
        if direct:
            return direct
        return [key for key in list(value.keys())[:32] if isinstance(key, str)]
    return []


def _collect_payment_method_types(data: dict) -> tuple[str, ...]:
    found: list[str] = []
    for node in _payment_nodes(data):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            normalized_key = _payment_key(key)
            if normalized_key not in _PAYMENT_METHOD_KEYS:
                continue
            for candidate in _method_values(value):
                method = normalize_payment_method_identifier(candidate)
                if method and method not in found:
                    found.append(method)
                    if len(found) >= 16:
                        return tuple(found)
    return tuple(found)


def _first_payment_value(data: dict, keys: set[str]) -> object:
    for node in _payment_nodes(data):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if _payment_key(key) in keys:
                return value
    return None


def _lookup_payment_path(node: dict, path: tuple[str, ...]) -> object:
    current: object = node
    for segment in path:
        if not isinstance(current, dict):
            return None
        wanted = _payment_key(segment)
        current = next(
            (value for key, value in current.items() if _payment_key(key) == wanted),
            None,
        )
    return current


def _first_payment_path_value(data: dict, paths: tuple[tuple[str, ...], ...]) -> object:
    for node in _payment_nodes(data):
        if not isinstance(node, dict):
            continue
        for path in paths:
            value = _lookup_payment_path(node, path)
            if value is not None:
                return value
    return None


def _normalize_payment_session_type(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("type") or value.get("session_type") or value.get("mode")
    if not isinstance(value, str):
        return None
    text = _bounded_payment_text(value, 32)
    if not text:
        return None
    lower = text.casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "checkout.session": "checkout",
        "checkout_session": "checkout_session",
        "checkout": "checkout",
        "paymentintent": "payment_intent",
        "payment_intent": "payment_intent",
        "setupintent": "setup_intent",
        "setup_intent": "setup_intent",
        "payment": "payment",
        "subscription": "subscription",
        "setup": "setup",
        "payment_page": "payment_page",
        "oaics": "oaics",
        "stripe": "stripe",
    }
    return aliases.get(lower) if lower in aliases else None


def _normalize_payment_amount(value: object) -> int | float | None:
    if isinstance(value, dict):
        for key in ("minorUnitsAmount", "minor_units_amount", "amount", "value", "due", "total"):
            if value.get(key) is not None:
                return _normalize_payment_amount(value[key])
        return None
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0 or number > 1_000_000_000 or number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number) if number.is_integer() else round(number, 4)


def _normalize_payment_currency(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = _bounded_payment_text(value, 16)
    if not text:
        return None
    text = text.upper()
    return text if _PAYMENT_METHOD_CURRENCY_RE.fullmatch(text) else None


def _parse_payment_response(data: object, *, source: str) -> PaymentMethodCheckResult:
    if not isinstance(data, dict):
        raise ValueError("malformed payment response")
    method_types = _collect_payment_method_types(data)
    if not method_types:
        raise ValueError("payment method types missing")
    labels = tuple(payment_method_label(item) for item in method_types)
    session_type = _normalize_payment_session_type(_first_payment_value(data, _PAYMENT_SESSION_KEYS))
    amount = _normalize_payment_amount(_first_payment_path_value(data, _PAYMENT_AMOUNT_PATHS))
    currency = _normalize_payment_currency(_first_payment_path_value(data, _PAYMENT_CURRENCY_PATHS))
    # Stripe's object discriminator is useful when the response omits mode;
    # OAICS uses the explicit payment_session/session_type fields. If the
    # provider omits its discriminator, retain only the parser-level type.
    if session_type is None and source == "stripe":
        session_type = _normalize_payment_session_type(data.get("object"))
    if session_type is None and source in {"oaics", "stripe"}:
        session_type = source
    return PaymentMethodCheckResult(
        payment_method_check_status=_PAYMENT_METHOD_CHECKED,
        payment_method_checked_at=now_iso(),
        payment_method_types=method_types,
        payment_method_labels=labels,
        payment_session_type=session_type,
        payment_amount=amount,
        payment_currency=currency,
    )


def parse_oaics_payment_response(data: object) -> dict:
    return _parse_payment_response(data, source="oaics").as_dict()


def parse_stripe_payment_response(data: object) -> dict:
    return _parse_payment_response(data, source="stripe").as_dict()


def parse_payment_method_response(data: object, *, source: str = "auto") -> dict:
    """Parse OAICS or Stripe initialization JSON without retaining its payload."""
    source = str(source or "auto").strip().lower()
    if source == "oaics":
        return parse_oaics_payment_response(data)
    if source == "stripe":
        return parse_stripe_payment_response(data)
    if isinstance(data, dict) and (
        str(data.get("object") or "").casefold() == "checkout.session"
        or "client_secret" in data
        or "payment_intent" in data
    ):
        return parse_stripe_payment_response(data)
    return parse_oaics_payment_response(data)


def _payment_error_result(message: str) -> dict:
    return PaymentMethodCheckResult(
        payment_method_check_status=_PAYMENT_METHOD_ERROR,
        payment_method_checked_at=now_iso(),
        payment_method_error=_bounded_payment_text(message, 160) or "payment method check failed",
    ).as_dict()


def _is_exact_one_month_free_trial(plan_result: dict) -> bool:
    """Require an unambiguous one-month, fully discounted free-trial result."""
    if not isinstance(plan_result, dict) or plan_result.get("ok") is not True:
        return False
    plan_type = str(
        plan_result.get("current_plan_type") or plan_result.get("plan_type") or ""
    ).strip().casefold()
    if plan_type != "free" or plan_result.get("plus_trial_eligible") is not True:
        return False
    discount = plan_result.get("plus_trial_discount_percentage")
    periods = plan_result.get("plus_trial_duration_num_periods")
    if (
        isinstance(discount, bool)
        or not isinstance(discount, (int, float))
        or discount != 100
        or isinstance(periods, bool)
        or not isinstance(periods, (int, float))
        or periods != 1
    ):
        return False
    period = plan_result.get("plus_trial_duration_period")
    return isinstance(period, str) and period.strip().casefold() == "month"


def _payment_check_offer(plan_result: dict) -> tuple[str | None, str | None]:
    if not _is_exact_one_month_free_trial(plan_result):
        return None, None
    plan_type = str(plan_result.get("current_plan_type") or plan_result.get("plan_type") or "").strip().casefold()
    campaigns = plan_result.get("eligible_promo_campaigns")
    if plan_type != "free" or not isinstance(campaigns, dict) or not campaigns:
        return None, None
    offer_ids = plan_result.get("eligible_offer_ids")
    if not isinstance(offer_ids, list) or not offer_ids:
        return None, None
    bounded_offers = []
    for item in offer_ids:
        offer = _bounded_payment_identifier(item)
        if not offer:
            return None, None
        if offer.casefold() not in {existing.casefold() for existing in bounded_offers}:
            bounded_offers.append(offer)
    if not bounded_offers:
        return None, None

    # Prefer an explicitly matching campaign. If campaign metadata does not
    # carry an offer name, the existing eligible-offer list is the only
    # checkout eligibility signal available from the plan response.
    for campaign in campaigns.values():
        if not isinstance(campaign, dict):
            continue
        metadata = campaign.get("metadata") if isinstance(campaign.get("metadata"), dict) else {}
        candidate_names = (
            metadata.get("plan_name"),
            campaign.get("offer_id"),
            campaign.get("plan"),
        )
        wanted = {
            value.casefold()
            for value in (_bounded_payment_identifier(item) for item in candidate_names)
            if value
        }
        offer = next((item for item in bounded_offers if item.casefold() in wanted), None)
        if offer is None and not any(candidate_names):
            offer = bounded_offers[0]
        if offer is None:
            continue
        campaign_id = _bounded_payment_identifier(campaign.get("id"))
        if campaign_id:
            return offer, campaign_id
    return None, None


def _payment_checkout_region(
    session: BrowserSession, plan_result: dict,
) -> tuple[str, str] | None:
    profile = getattr(session, "browser_profile", {}) or {}
    geo = getattr(session, "exit_geo", {}) or {}
    profile_geo = profile.get("geo") if isinstance(profile, dict) else {}
    country_candidates = (
        geo.get("country") if isinstance(geo, dict) else None,
        profile_geo.get("country") if isinstance(profile_geo, dict) else None,
    )
    country = next(
        (
            str(value).strip().upper()
            for value in country_candidates
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2}", value.strip())
        ),
        None,
    )
    if not country and isinstance(profile, dict):
        language = str(profile.get("navigator_language") or "")
        locale_country = language.rsplit("-", 1)[-1].strip().upper()
        if re.fullmatch(r"[A-Z]{2}", locale_country):
            country = locale_country
    if not country:
        return None

    currency = _PAYMENT_CURRENCY_BY_COUNTRY.get(country)
    if not currency and country in _PAYMENT_EURO_COUNTRIES:
        currency = "EUR"
    if not currency:
        candidate = str(plan_result.get("billing_currency") or "").strip().upper()
        if _PAYMENT_METHOD_CURRENCY_RE.fullmatch(candidate):
            currency = candidate
    return (country, currency) if currency else None


def check_payment_method_availability(
    session: BrowserSession | None,
    token: str,
    plan_result: dict,
    *,
    timeout: float = 15.0,
) -> dict:
    """Best-effort checkout initialization using the already authenticated session."""
    offer_id, campaign_id = _payment_check_offer(plan_result)
    if not offer_id:
        return PaymentMethodCheckResult(
            payment_method_check_status=_PAYMENT_METHOD_NOT_APPLICABLE,
        ).as_dict()
    if session is None:
        return _payment_error_result("payment transport error")
    region = _payment_checkout_region(session, plan_result)
    if not region:
        return _payment_error_result("payment region unavailable")
    country, currency = region
    try:
        claims = token_claims(token)
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": offer_id,
            "billing_details": {"country": country, "currency": currency},
            "checkout_ui_mode": "custom",
            "promo_campaign": {
                "promo_campaign_id": campaign_id,
                "is_coupon_from_query_param": False,
            },
        }
        response = session.post(
            f"https://chatgpt.com{PAYMENT_CHECKOUT_PATH}",
            headers=_common_headers(
                session, token, claims, target_path=PAYMENT_CHECKOUT_PATH,
            ),
            json=body,
            allow_redirects=False,
            timeout=max(1.0, min(60.0, float(timeout or 15.0))),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            return _payment_error_result(f"HTTP {status}" if status else "HTTP status missing")
        try:
            data = response.json()
        except Exception:
            return _payment_error_result("malformed payment response")
        try:
            return parse_payment_method_response(data)
        except (TypeError, ValueError):
            return _payment_error_result("payment method types missing")
    except Exception:
        # Exception text can contain URLs, request bodies, or provider secrets;
        # only expose the bounded failure class.
        return _payment_error_result("payment transport error")


# Short compatibility name for callers/tests that use the operation wording.
check_payment_methods = check_payment_method_availability


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _proxy_lines(value: Any) -> list[str]:
    """兼容 .env 多行代理和旧的单行代理值。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def open_plan_check_proxy(route: dict, selected_proxy: str, *, timeout: float):
    """返回实际请求代理；配置了上游时启动本地 HTTP CONNECT 中继。"""
    selected_proxy = str(selected_proxy or "").strip()
    upstream = str(route.get("upstream_proxy") or "").strip()
    if selected_proxy and upstream:
        from core.proxy_chain import ProxyChainRelay

        relay = ProxyChainRelay(selected_proxy, upstream, timeout=timeout).start()
        return relay.proxy_url, relay
    return selected_proxy, None


def resolve_plan_check_route(explicit_proxy: Optional[str] = None) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        return {
            "proxy": selected,
            "proxy_mode": "request",
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "upstream_proxy": "",
            "upstream_proxy_used": None,
            "proxy_fallback_reason": None,
        }

    from config import proxy as proxy_cfg

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if mode == "direct":
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "upstream_proxy": "",
            "upstream_proxy_used": None,
            "proxy_fallback_reason": None,
        }

    candidates = _proxy_lines(getattr(proxy_cfg, "PLAN_CHECK_PROXY", ""))
    # 专用代理配置为代理池时，每次新的套餐查询随机选择一个；
    # 若未配置专用池，则继续从通用 PROXY_POOL 随机选择。
    selected = random.choice(candidates) if candidates else str(proxy_cfg.pick_proxy() or "").strip()
    if not selected:
        if mode == "proxy":
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "upstream_proxy": "",
            "upstream_proxy_used": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct_fallback",
            "proxy_used": _mask_proxy(selected),
            "upstream_proxy": "",
            "upstream_proxy_used": None,
            "proxy_fallback_reason": reason,
        }
    upstream = str(getattr(proxy_cfg, "PLAN_CHECK_UPSTREAM_PROXY", "") or "").strip()
    return {
        "proxy": selected,
        "upstream_proxy": upstream,
        "upstream_proxy_used": _mask_proxy(upstream) or None,
        "proxy_mode": mode,
        "network_route": "proxy_chain" if upstream else "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": None,
    }


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)):
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        expired = datetime.now(tz=timezone.utc).timestamp() >= float(exp)
    return {
        "payload": payload,
        "email": profile.get("email"),
        "user_name": profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _common_headers(
    env: BrowserSession,
    token: str,
    claims: dict | None = None,
    *,
    target_path: str = ACCOUNTS_CHECK_PATH,
) -> dict[str, str]:
    """生成登录态 ChatGPT API 请求头，并标记对应的目标路由。"""
    headers = env.get_chatgpt_headers(referer="https://chatgpt.com/")
    # GET 导航后的前端 fetch 不主动设置 content-type。
    headers.pop("content-type", None)
    headers.update({
        "authorization": f"Bearer {normalize_token(token)}",
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    })
    account_id = str((claims or {}).get("account_id") or "").strip()
    if account_id:
        headers["chatgpt-account-id"] = account_id
    return headers


def parse_accounts_check(data: dict, *, token: str = "") -> dict:
    """从 accounts/check 响应提取套餐和 Plus 试用资格。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item = None
    account_key = None
    if claim_account_id and isinstance(accounts.get(claim_account_id), dict):
        item = accounts.get(claim_account_id)
        account_key = claim_account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        account_key = account.get("account_id") or "default"
    else:
        for k, v in accounts.items():
            if k != "default" and isinstance(v, dict):
                item = v
                account_key = k
                break
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}
    eligible_promo_campaigns = item.get("eligible_promo_campaigns") or {}
    plus_campaign = eligible_promo_campaigns.get("plus") if isinstance(eligible_promo_campaigns, dict) else None
    plus_meta = (plus_campaign or {}).get("metadata") or {}
    discount = plus_meta.get("discount") or {}
    duration = plus_meta.get("duration") or {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = bool(entitlement.get("has_active_subscription"))
    is_free = str(plan_type).lower() == "free" or str(subscription_plan).lower() == "chatgptfreeplan"
    plus_trial_eligible = bool(is_free and plus_campaign)

    # 保留 Free 账号返回的全部促销活动，供前端查看详情；Plus 资格判定仍然
    # 只使用上面的 eligible_promo_campaigns.plus，保持原有语义不变。
    promo_campaigns = {}
    if is_free and isinstance(eligible_promo_campaigns, dict):
        for campaign_key, campaign in eligible_promo_campaigns.items():
            if isinstance(campaign, dict):
                promo_campaigns[str(campaign_key)] = campaign

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": has_active_subscription,
        "is_active_subscription_gratis": bool(entitlement.get("is_active_subscription_gratis")),
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "cancels_at": entitlement.get("cancels_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "is_delinquent": bool(entitlement.get("is_delinquent")),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
        "last_will_renew": bool(last_sub.get("will_renew")),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "eligible_promo_campaigns": promo_campaigns,
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0)
    attempts_value = max_attempts if max_attempts is not None else getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2)
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 1.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 15.0))),
        max(1, min(4, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _retryable_plan_error(http_status: int | None) -> bool:
    if http_status is None:
        return True
    return http_status in {403, 408, 409, 425, 429} or http_status >= 500


def _clear_plan_circuit(env: BrowserSession) -> None:
    """清除可重试响应产生的本地熔断，同时保留 Cookie Jar。"""
    reset = getattr(env, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
    else:
        env.blocked_until = 0.0
        env.blocked_reason = ""


def _warm_plan_session(env: BrowserSession) -> None:
    """先访问 ChatGPT document 建立同一会话的边缘 Cookie；失败不阻断正式查询。"""
    try:
        resp = env.get(
            "https://chatgpt.com/",
            headers=env.get_chatgpt_navigate_headers(
                referer="https://chatgpt.com/", user_initiated=False,
            ),
            allow_redirects=True,
        )
        if int(getattr(resp, "status_code", 0) or 0) >= 400:
            logger.info("[Plan] document 预热返回 HTTP %s，保留响应 Cookie 后继续", resp.status_code)
    except Exception as exc:
        logger.debug("[Plan] document 预热失败，继续正式查询：%s: %s", type(exc).__name__, str(exc)[:160])
    finally:
        _clear_plan_circuit(env)


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    session: BrowserSession | None = None,
) -> dict:
    """查询套餐；传入 session 时借用现有会话，不更换出口、不预热、不关闭它。"""
    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy) if session is None else {
            "proxy_used": _mask_proxy(session.proxy or "") or None,
            "network_route": "proxy" if session.proxy else "direct",
            "proxy_mode": "live_session",
        }
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    route_meta = {k: v for k, v in route.items() if k not in {"proxy", "upstream_proxy"}}
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"套餐查询重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    last_result: dict | None = None
    identity = str(
        claims.get("email") or claims.get("account_id") or normalize_token(token)[:32]
    ).lower()
    # 任务级随机 seed：同一查询的所有重试统一 device/session/Cookie；不同账号
    # 或下一次查询不会复用旧浏览器身份。
    task_seed = f"plan-check:{identity}:{uuid.uuid4()}"
    env: BrowserSession | None = session
    owns_session = session is None
    relay = None
    try:
        # 首次按代理真实出口自动生成语言/时区画像，随后整条查询链固定不漂移。
        if owns_session:
            effective_proxy, relay = open_plan_check_proxy(
                route, route["proxy"], timeout=timeout_seconds,
            )
            env = BrowserSession(
                proxy=effective_proxy, detect_exit_geo=True, fingerprint_seed=task_seed,
            )
        effective_tz = str(timezone_offset_min or "").strip()
        if not effective_tz or effective_tz == "-":
            effective_tz = str(env.js_timezone_offset_min())
        url = (
            f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}"
            f"?timezone_offset_min={quote(effective_tz)}"
        )
        logger.info(
            "[Plan] 查询会话（新建或借用）：proxy=%s device_id=%s oai_session_id=%s %s",
            route_meta.get("proxy_used") or route_meta.get("network_route") or "direct",
            str(env.device_id)[:12] + "...",
            str(env.oai_session_id)[:12] + "...",
            env.fingerprint_summary_text(),
        )
        if owns_session:
            _warm_plan_session(env)

        for attempt in range(1, attempts + 1):
            resp = None
            try:
                resp = env.get(
                    url,
                    headers=_common_headers(env, token, claims),
                    allow_redirects=False,
                    timeout=timeout_seconds,
                )
                response_text = resp.text or ""
                http_status = int(resp.status_code)
                if not (200 <= http_status < 300):
                    is_auth_expired = http_status == 401
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "http_status": http_status,
                        "error": "AT已过期/失效，请手动查活刷新" if is_auth_expired else f"HTTP {http_status}",
                        "response_preview": response_text[:500],
                        "retryable": _retryable_plan_error(http_status),
                        "token_expired": True if is_auth_expired else claims.get("token_expired"),
                        "needs_live_check": True if is_auth_expired else False,
                    }
                else:
                    try:
                        data: Any = resp.json()
                    except Exception:
                        data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
                    if not isinstance(data, dict):
                        last_result = {
                            "ok": False,
                            "checked_at": now_iso(),
                            "http_status": http_status,
                            "error": "响应不是 JSON 对象",
                            "response_preview": response_text[:500],
                            "retryable": True,
                        }
                    else:
                        parsed = parse_accounts_check(data, token=token)
                        parsed["http_status"] = http_status
                        parsed["attempt_count"] = attempt
                        parsed["max_attempts"] = attempts
                        parsed["request_timeout"] = timeout_seconds
                        parsed["retryable"] = False
                        parsed["timezone_offset_min"] = effective_tz
                        parsed.update(route_meta)
                        try:
                            parsed.update(check_payment_method_availability(
                                env, token, parsed, timeout=timeout_seconds,
                            ))
                        except Exception:
                            # Payment detection is deliberately subordinate to a
                            # successful plan result and must never break it.
                            parsed.update(_payment_error_result("payment method check error"))
                        return parsed
            except Exception as exc:
                logger.debug("套餐查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
                last_result = {
                    "ok": False,
                    "checked_at": now_iso(),
                    "http_status": int(resp.status_code) if resp is not None and getattr(resp, "status_code", None) else None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": True,
                }

            last_result = last_result or {"ok": False, "checked_at": now_iso(), "error": "未知错误", "retryable": True}
            last_result.update({
                "attempt_count": attempt,
                "max_attempts": attempts,
                "request_timeout": timeout_seconds,
                "timezone_offset_min": effective_tz,
                **route_meta,
                **{k: v for k, v in claims.items() if k != "payload"},
            })
            if not last_result.get("retryable") or attempt >= attempts:
                return last_result

            # 403/429 会打开 BrowserSession 熔断。保留服务端刚下发的 CF Cookie，
            # 只清除本地熔断并在同一会话内退避重试。
            _clear_plan_circuit(env)
            wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
            logger.warning(
                "套餐查询临时失败，第 %s/%s 次，保留 session/deviceId/CF Cookie，%.1fs 后重试: %s",
                attempt,
                attempts,
                wait_seconds,
                last_result.get("error"),
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
    except Exception as exc:
        logger.debug("套餐查询会话初始化失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"{type(exc).__name__}: {exc}",
            "retryable": True,
            "attempt_count": 0,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    finally:
        if owns_session and env is not None:
            try:
                close_browser_session(env)
            except Exception:
                pass
        if relay is not None:
            try:
                relay.close()
            except Exception:
                # 清理辅助资源失败不能覆盖已经取得的套餐业务结果。
                logger.warning("[Plan] 代理中继关闭失败", exc_info=True)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": "套餐查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
