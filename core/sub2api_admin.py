# -*- coding: utf-8 -*-
"""Bounded, non-sensitive adapter for the Sub2API account management endpoints."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from math import isfinite
from typing import Callable
from urllib.parse import quote, urlencode

_MAX_ID = 160
_MAX_NAME = 240
_MAX_REASON = 180
_MAX_PAGE_SIZE = 100
_MAX_PAGES = 20
_MAX_BATCH_REFRESH_IDS = 500
_BATCH_REFRESH_CHUNK_SIZE = 10
_MAX_REFRESH_ERRORS = 500
_MAX_REFRESH_WARNINGS = 50
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_EMAIL_SCAN_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SECRET_RE = re.compile(
    r"(?i)([\"']?\b(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|authorization|bearer|"
    r"api[_ -]?key|password|secret|callback(?:_url)?|redirect_uri|oauth[_ -]?code|code)\b[\"']?)"
    r"\s*[:=]\s*[\"']?[^\s,;}\]\"']+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_RE = re.compile(r"https?://[^\s,;}\]\\\"'，。；！？、）】》]+", re.I)


def _clean_text(value: object, limit: int) -> str:
    raw = "" if value is None else str(value)
    return " ".join(raw.replace("\x00", "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _safe_reason(value: object, fallback: str = "Sub2API 请求失败") -> str:
    text = _clean_text(value, 1000)
    text = _SECRET_RE.sub(r"\1=[redacted]", text)
    text = _BEARER_RE.sub("bearer [redacted]", text)
    text = _URL_RE.sub("[url-redacted]", text)
    return (text or fallback)[:_MAX_REASON]


def _account_id(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return _clean_text(value, _MAX_ID)


def _dict_account_id(value: dict) -> str:
    raw_id = value.get("account_id")
    if raw_id is None:
        raw_id = value.get("id")
    return _account_id(raw_id)


def _wire_account_id(value: str) -> int | str:
    return int(value) if re.fullmatch(r"[0-9]+", value) else value


def _email(value: object) -> str:
    text = _clean_text(value, 320).lower()
    return text if _EMAIL_RE.match(text) else ""


def _unwrap_dict(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _list_rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    containers = [data, payload]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("items", "accounts", "records", "list", "rows"):
            rows = container.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _pagination(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    for container in (data, payload):
        if not isinstance(container, dict):
            continue
        for key in ("pagination", "page", "meta"):
            value = container.get(key)
            if isinstance(value, dict):
                return value
        if any(
            key in container
            for key in ("current_page", "page", "page_size", "per_page", "total", "total_pages", "pages", "last_page", "has_next")
        ):
            return container
    return {}


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _created_at(value: object) -> str:
    """保留可排序的非敏感创建时间，拒绝任意长文本和无效值。"""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        if not isfinite(float(value)) or not 0 < float(value) < 4_102_444_800:
            return ""
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    text = _clean_text(value, 64)
    if not text or "@" in text or "http://" in text.lower() or "https://" in text.lower():
        return ""
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _extract_email(row: dict) -> tuple[str, bool]:
    candidates: list[str] = []
    for key in ("email", "account_email", "username", "name"):
        text = _clean_text(row.get(key), 320)
        for match in _EMAIL_SCAN_RE.finditer(text):
            candidate = _email(match.group(0))
            if candidate and candidate not in candidates:
                candidates.append(candidate)
    return (candidates[0], False) if len(candidates) == 1 else ("", len(candidates) > 1)


def _sanitize_account(row: dict) -> dict:
    email, ambiguous = _extract_email(row)
    account_id = _dict_account_id(row)
    return {
        "account_id": account_id,
        "account_name": _safe_reason(row.get("name") or row.get("display_name") or "", fallback="")[:_MAX_NAME],
        "email": email,
        "email_ambiguous": ambiguous,
        "platform": _clean_text(row.get("platform"), 40).lower(),
        "type": _clean_text(row.get("type") or row.get("account_type"), 40).lower(),
        "status": _clean_text(row.get("status"), 64).lower(),
        "disabled": row.get("disabled") is True,
        "unavailable": row.get("unavailable") is True,
        "created_at": _created_at(row.get("created_at") or row.get("createdAt")),
    }


class Sub2ApiAdminError(RuntimeError):
    """Safe Sub2API management error which never carries response credentials."""

    def __init__(self, message: str, *, status_code: int = 502, category: str = "remote") -> None:
        self.status_code = status_code
        self.category = category
        super().__init__(_safe_reason(message))


class Sub2ApiAdminClient:
    """Minimal adapter for listing, refreshing, and deleting OpenAI OAuth accounts."""

    def __init__(self, request_fn: Callable[[str, str, dict | None], dict] | None = None) -> None:
        if request_fn is None:
            from core.codex_oauth import sub2api_admin_request_json

            request_fn = sub2api_admin_request_json
        self._request_fn = request_fn

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            payload = self._request_fn(method, path, body)
        except Sub2ApiAdminError:
            raise
        except Exception as exc:
            text = str(exc or "").lower()
            if "timeout" in text or "timed out" in text:
                raise Sub2ApiAdminError("Sub2API 管理接口请求超时", status_code=504, category="timeout") from exc
            if "status=401" in text or "status=403" in text:
                raise Sub2ApiAdminError("Sub2API 管理接口认证失败", status_code=502, category="auth") from exc
            if "status=404" in text or "not_found" in text:
                raise Sub2ApiAdminError("当前 Sub2API 不支持所需的管理接口", status_code=501, category="unsupported") from exc
            raise Sub2ApiAdminError("Sub2API 管理接口请求失败", status_code=502, category="remote") from exc
        payload = payload if isinstance(payload, dict) else {}
        code = _int(payload.get("code")) if "code" in payload else 0
        if code:
            message = _safe_reason(payload.get("message") or payload.get("reason"), "Sub2API 管理接口请求失败")
            if code in {401, 403}:
                raise Sub2ApiAdminError(message, status_code=502, category="auth")
            if code == 404:
                raise Sub2ApiAdminError(message, status_code=501, category="unsupported")
            if "timeout" in message.lower() or "timed out" in message.lower():
                raise Sub2ApiAdminError(message, status_code=504, category="timeout")
            raise Sub2ApiAdminError(message, status_code=502, category="remote")
        return payload

    def list_openai_oauth_accounts(self, *, page_size: int = _MAX_PAGE_SIZE, max_pages: int = _MAX_PAGES) -> list[dict]:
        page_size = max(1, min(_MAX_PAGE_SIZE, _int(page_size, _MAX_PAGE_SIZE)))
        max_pages = max(1, min(_MAX_PAGES, _int(max_pages, _MAX_PAGES)))
        accounts: list[dict] = []
        seen_ids: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()

        for page in range(1, max_pages + 1):
            query = urlencode({"platform": "openai", "type": "oauth", "page": page, "page_size": page_size, "lite": 1})
            payload = self._request("GET", f"/api/v1/admin/accounts?{query}")
            rows = _list_rows(payload)
            page_signature = tuple(_dict_account_id(raw) for raw in rows if isinstance(raw, dict))
            if page_signature and page_signature in seen_pages:
                break
            if page_signature:
                seen_pages.add(page_signature)
            for raw in rows:
                account = _sanitize_account(raw)
                if account["platform"] != "openai" or account["type"] != "oauth":
                    continue
                account_id = account["account_id"]
                if not account_id or account_id in seen_ids:
                    continue
                seen_ids.add(account_id)
                accounts.append(account)

            paging = _pagination(payload)
            total_pages = _int(paging.get("total_pages") or paging.get("pages") or paging.get("last_page"))
            current_page = _int(paging.get("current_page") or paging.get("page"), page)
            effective_page_size = _int(paging.get("page_size") or paging.get("per_page"), page_size)
            total = _int(paging.get("total") or paging.get("total_count") or paging.get("count"))
            has_next = _bool(paging.get("has_next"))
            if total_pages and current_page >= total_pages:
                break
            if has_next is False:
                break
            if total and effective_page_size and current_page * effective_page_size >= total:
                break
            if not paging and len(rows) < page_size:
                break
            if paging and not total_pages and not total and has_next is None and len(rows) < effective_page_size:
                break
        return accounts

    def batch_refresh(self, account_ids: list[str]) -> dict:
        selected = []
        seen: set[str] = set()
        for raw in account_ids:
            account_id = _account_id(raw)
            if account_id and account_id not in seen:
                seen.add(account_id)
                selected.append(account_id)
                if len(selected) >= _MAX_BATCH_REFRESH_IDS:
                    break
        if not selected:
            return {"total": 0, "success": 0, "failed": 0, "errors": [], "warnings": []}

        total = len(selected)
        success = 0
        failed = 0
        errors: list[dict] = []
        error_ids: set[str] = set()
        warnings: list[str] = []
        for start in range(0, len(selected), _BATCH_REFRESH_CHUNK_SIZE):
            chunk = selected[start : start + _BATCH_REFRESH_CHUNK_SIZE]
            chunk_ids = set(chunk)
            payload = self._request(
                "POST",
                "/api/v1/admin/accounts/batch-refresh",
                {"account_ids": [_wire_account_id(account_id) for account_id in chunk]},
            )
            data = _unwrap_dict(payload)
            raw_errors = data.get("errors") if isinstance(data.get("errors"), list) else []
            chunk_error_ids: set[str] = set()
            for row in raw_errors:
                if not isinstance(row, dict):
                    continue
                account_id = _dict_account_id(row)
                if account_id not in chunk_ids:
                    continue
                if account_id not in chunk_error_ids:
                    chunk_error_ids.add(account_id)
                    if account_id not in error_ids and len(errors) < _MAX_REFRESH_ERRORS:
                        errors.append({"account_id": account_id, "error": _safe_reason(row.get("error") or row.get("message"))})
            new_error_ids = chunk_error_ids - error_ids
            error_ids.update(chunk_error_ids)

            raw_warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
            remaining_warnings = _MAX_REFRESH_WARNINGS - len(warnings)
            if remaining_warnings > 0:
                warnings.extend(
                    _safe_reason(value, "Sub2API 刷新警告")
                    for value in raw_warnings[:remaining_warnings]
                )

            # Normalize every chunk independently. Upstream counts are advisory:
            # explicit unique errors always consume capacity, missing success is
            # conservative, and an over-reported chunk cannot make another chunk
            # appear healthy when the service consumes the aggregate success count.
            chunk_size = len(chunk)
            reported_failed = max(0, min(chunk_size, _int(data.get("failed"), len(new_error_ids))))
            normalized_failed = max(len(new_error_ids), reported_failed)
            reported_success = max(0, min(chunk_size, _int(data.get("success"))))
            normalized_success = min(reported_success, chunk_size - normalized_failed)
            success += normalized_success
            failed += normalized_failed

        return {
            "total": total,
            "success": min(total, success),
            "failed": min(total, failed),
            "errors": errors[:_MAX_REFRESH_ERRORS],
            "warnings": warnings,
        }

    def delete_account(self, account_id: str) -> dict:
        normalized = _account_id(account_id)
        if not normalized:
            raise ValueError("account_id 不能为空")
        path = f"/api/v1/admin/accounts/{quote(normalized, safe='')}"
        self._request("PUT", path, {"status": "inactive"})
        self._request("DELETE", path)
        return {"ok": True, "account_id": normalized}
