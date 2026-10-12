# -*- coding: utf-8 -*-
"""Verification-first Sub2API OpenAI OAuth 401 credential rotation state."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.sub2api_admin import Sub2ApiAdminClient

_MAX_REASON = 180
_MAX_ID = 160
_MAX_NAME = 240
_STATES = {"pending", "retrying", "verified", "cleanup_ready", "cleaned", "failed", "ambiguous"}
_HEALTH_STATES = {"healthy", "unauthorized", "timeout", "unknown"}
_RUNNING_LOCAL_STATES = {"retrying", "queued", "running"}
_FAILED_LOCAL_STATES = {"failed", "stopped", "cancelled", "canceled", "error"}
_UNAUTHORIZED_RE = re.compile(r"(?:\b401\b|unauthori[sz]ed|invalid[_ -]?grant|invalid[_ -]?token)", re.I)
_SECRET_RE = re.compile(
    r"(?i)([\"']?\b(?:access[_ -]?token|refresh[_ -]?token|id[_ -]?token|authorization|bearer|"
    r"api[_ -]?key|password|secret|callback(?:_url)?|redirect_uri|oauth[_ -]?code|code)\b[\"']?)"
    r"\s*[:=]\s*[\"']?[^\s,;}\]\"']+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_RE = re.compile(r"https?://[^\s,;}\]\\\"'，。；！？、）】》]+", re.I)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, limit: int) -> str:
    raw = "" if value is None else str(value)
    return " ".join(raw.replace("\x00", "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _reason(value: object, fallback: str) -> str:
    text = _text(value, 1000)
    text = _SECRET_RE.sub(r"\1=[redacted]", text)
    text = _BEARER_RE.sub("bearer [redacted]", text)
    text = _URL_RE.sub("[url-redacted]", text)
    return (text or fallback)[:_MAX_REASON]


def _account_id(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return _text(value, _MAX_ID)


def _account_name(value: object) -> str:
    return _reason(value, "")[:_MAX_NAME]


def _email(value: object) -> str:
    text = _text(value, 320).lower()
    return text if _EMAIL_RE.match(text) else ""


def _created_at(value: object) -> str:
    text = _text(value, 64)
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


def _bounded_int(value: object, *, minimum: int = 0, maximum: int = 10_000) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _dedupe_ids(values: object, *, limit: int = 500) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _account_id(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return sorted(result)


def _health_from_error(value: object) -> str:
    text = _text(value, 1000)
    if _UNAUTHORIZED_RE.search(text):
        return "unauthorized"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return "timeout"
    return "unknown"


class Sub2ApiSnapshotConflict(RuntimeError):
    """The live Sub2API account IDs no longer match the persisted selection."""

    def __init__(self, account_ids: list[str]):
        self.account_ids = _dedupe_ids(account_ids, limit=50)
        super().__init__("Sub2API 账号快照已变化，请重新检测后再补跑")


class Sub2Api401Service:
    """Persist non-sensitive Sub2API rotation evidence and gate exact-ID cleanup."""

    def __init__(
        self,
        *,
        list_fn: Callable[[], list[dict]] | None = None,
        refresh_fn: Callable[[list[str]], dict] | None = None,
        delete_fn: Callable[[str], object] | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        if list_fn is None or refresh_fn is None or delete_fn is None:
            client = Sub2ApiAdminClient()
        self._list_fn = list_fn or client.list_openai_oauth_accounts
        self._refresh_fn = refresh_fn or client.batch_refresh
        self._delete_fn = delete_fn or client.delete_account
        self._audit_path = Path(audit_path) if audit_path is not None else Path(__file__).resolve().parent.parent / "sub2api_401_audit.json"
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._health_counts = {kind: 0 for kind in _HEALTH_STATES}
        self._tracked = self._load_audit()

    @property
    def cleanup_supported(self) -> bool:
        return callable(self._delete_fn)

    def _audit_target(self) -> Path:
        try:
            if self._audit_path.is_symlink():
                return self._audit_path.resolve(strict=False)
        except OSError:
            pass
        return self._audit_path

    def _load_audit(self) -> dict[str, dict]:
        try:
            payload = json.loads(self._audit_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        raw_counts = payload.get("health_counts") if isinstance(payload, dict) else None
        if isinstance(raw_counts, dict):
            self._health_counts = {kind: _bounded_int(raw_counts.get(kind)) for kind in _HEALTH_STATES}
        raw_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(raw_items, dict):
            return {}
        tracked: dict[str, dict] = {}
        for raw_id, raw_item in raw_items.items():
            account_id = _account_id(raw_id)
            if not account_id or not isinstance(raw_item, dict):
                continue
            item = self._sanitize_entry({**raw_item, "account_id": account_id})
            tracked[account_id] = item
        return tracked

    def _persist_locked(self) -> None:
        target = self._audit_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "updated_at": _now(),
            "health_counts": {kind: _bounded_int(self._health_counts.get(kind)) for kind in sorted(_HEALTH_STATES)},
            "items": {account_id: self._sanitize_entry(item) for account_id, item in sorted(self._tracked.items())},
        }
        temp_path = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _sanitize_entry(self, raw: dict) -> dict:
        state = _text(raw.get("state") or "ambiguous", 32).lower()
        if state not in _STATES:
            state = "ambiguous"
        health = _text(raw.get("health") or "unknown", 32).lower()
        if health not in _HEALTH_STATES:
            health = "unknown"
        account_id = _account_id(raw.get("account_id"))
        replacement_id = _account_id(raw.get("replacement_account_id"))
        cleanup_id = _account_id(raw.get("cleanup_account_id"))
        retryable = bool(raw.get("retryable")) and state in {"pending", "failed"}
        return {
            "account_id": account_id,
            "account_name": _account_name(raw.get("account_name")),
            "email": _email(raw.get("email")),
            "created_at": _created_at(raw.get("created_at")),
            "state": state,
            "health": health,
            "reason": _reason(raw.get("reason"), state),
            "observed_account_ids": _dedupe_ids(raw.get("observed_account_ids")),
            "pre_retry_account_ids": _dedupe_ids(raw.get("pre_retry_account_ids")),
            "post_retry_account_ids": _dedupe_ids(raw.get("post_retry_account_ids")),
            "replacement_account_id": replacement_id,
            "cleanup_account_id": cleanup_id,
            "candidate_count": _bounded_int(raw.get("candidate_count")),
            "checked_at": _text(raw.get("checked_at"), 64),
            "pre_retry_snapshot_at": _text(raw.get("pre_retry_snapshot_at"), 64),
            "post_retry_snapshot_at": _text(raw.get("post_retry_snapshot_at"), 64),
            "retry_started_at": _text(raw.get("retry_started_at"), 64),
            "retry_batch_id": _text(raw.get("retry_batch_id"), 64),
            "local_codex_status": _text(raw.get("local_codex_status"), 64).lower(),
            "retryable": retryable,
            "cleanup_supported": self.cleanup_supported,
            "manual_action": (
                "历史重复项或自动清理失败时，确认后仅按锁定的旧 account_id 重试清理"
                if state == "cleanup_ready" else ""
            ),
        }

    def _public_items_locked(self) -> list[dict]:
        return [self._sanitize_entry(item) for _, item in sorted(self._tracked.items())]

    def _counts(self, items: list[dict]) -> dict[str, int]:
        counts = {state: 0 for state in sorted(_STATES)}
        for item in items:
            state = item.get("state")
            if state in counts:
                counts[state] += 1
        counts["verified"] = counts.get("verified", 0) + counts.get("cleanup_ready", 0) + counts.get("cleaned", 0)
        for kind in _HEALTH_STATES:
            counts[kind] = _bounded_int(self._health_counts.get(kind))
        return counts

    def state(self) -> dict:
        with self._lock:
            items = self._public_items_locked()
            counts = self._counts(items)
        return {
            "ok": True,
            "supported": True,
            "cleanup_supported": self.cleanup_supported,
            "items": items,
            "counts": counts,
            "checked_at": max((item.get("checked_at") or "" for item in items), default=""),
        }

    @staticmethod
    def _account_maps(accounts: list[dict]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
        by_id: dict[str, dict] = {}
        by_email: dict[str, list[dict]] = {}
        for raw in accounts:
            if not isinstance(raw, dict):
                continue
            account_id = _account_id(raw.get("account_id") or raw.get("id"))
            if not account_id:
                continue
            email = _email(raw.get("email"))
            account = {
                "account_id": account_id,
                "account_name": _account_name(raw.get("account_name") or raw.get("name")),
                "email": email,
                "email_ambiguous": raw.get("email_ambiguous") is True,
                "created_at": _created_at(raw.get("created_at")),
            }
            if account_id not in by_id:
                by_id[account_id] = account
            if email:
                by_email.setdefault(email, []).append(account)
        return by_id, by_email

    @staticmethod
    def _refresh_maps(account_ids: list[str], payload: dict) -> tuple[dict[str, str], dict[str, str]]:
        selected = _dedupe_ids(account_ids)
        errors: dict[str, str] = {}
        raw_errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(raw_errors, list):
            for row in raw_errors:
                if not isinstance(row, dict):
                    continue
                account_id = _account_id(row.get("account_id") or row.get("id"))
                if account_id in selected:
                    errors[account_id] = _reason(row.get("error") or row.get("message"), "Sub2API 刷新失败")
        success_count = _bounded_int(payload.get("success") if isinstance(payload, dict) else 0)
        non_error = [account_id for account_id in selected if account_id not in errors]
        all_non_error_succeeded = success_count >= len(non_error)
        kinds: dict[str, str] = {}
        reasons: dict[str, str] = {}
        for account_id in selected:
            if account_id in errors:
                kinds[account_id] = _health_from_error(errors[account_id])
                reasons[account_id] = errors[account_id]
            elif all_non_error_succeeded:
                kinds[account_id] = "healthy"
                reasons[account_id] = "Sub2API 刷新成功"
            else:
                kinds[account_id] = "unknown"
                reasons[account_id] = "Sub2API 未返回可确认的刷新结果"
        return kinds, reasons

    def _evaluate_after_retry(
        self,
        old: dict,
        by_id: dict[str, dict],
        by_email: dict[str, list[dict]],
        kinds: dict[str, str],
        reasons: dict[str, str],
        *,
        local_status: str,
        checked_at: str,
    ) -> dict:
        old_id = _account_id(old.get("account_id"))
        email = _email(old.get("email"))
        current_rows = by_email.get(email, []) if email else []
        current_ids = _dedupe_ids([row.get("account_id") for row in current_rows])
        pre_ids = _dedupe_ids(old.get("pre_retry_account_ids") or old.get("observed_account_ids") or [old_id])
        replacement = _account_id(old.get("replacement_account_id"))
        old_exists = old_id in by_id
        base = {
            **old,
            "health": kinds.get(old_id, old.get("health") or "unknown"),
            "observed_account_ids": current_ids,
            "pre_retry_account_ids": pre_ids,
            "post_retry_account_ids": current_ids,
            "candidate_count": len(current_rows),
            "checked_at": checked_at,
            "post_retry_snapshot_at": checked_at,
            "local_codex_status": local_status,
            "retryable": False,
            "cleanup_account_id": "",
        }

        if old.get("state") == "cleaned" and replacement:
            if old_id not in by_id and replacement in by_id and kinds.get(replacement) == "healthy":
                return self._sanitize_entry({
                    **base,
                    "state": "cleaned",
                    "reason": "旧 Sub2API account_id 已删除，替代账号保持健康",
                    "replacement_account_id": replacement,
                    "cleanup_account_id": old_id,
                })
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "清理后 Sub2API 账号状态发生变化，需要人工复核"})

        if old.get("state") == "cleanup_ready" and replacement:
            expected_ids = _dedupe_ids(old.get("post_retry_account_ids") or old.get("observed_account_ids"))
            expected_current = set(expected_ids)
            if not old_exists:
                expected_current.discard(old_id)
            if not expected_ids or set(current_ids) != expected_current:
                return self._sanitize_entry({
                    **base,
                    "state": "ambiguous",
                    "reason": "已验证的新旧 account_id 快照发生变化，禁止清理",
                    "replacement_account_id": replacement,
                })
            replacement_row = by_id.get(replacement)
            if not replacement_row or replacement_row.get("email") != email or kinds.get(replacement) != "healthy":
                return self._sanitize_entry({
                    **base,
                    "state": "ambiguous",
                    "reason": "已记录的替代 account_id 不再满足健康与邮箱一致性条件",
                    "replacement_account_id": replacement,
                })
            if not old_exists:
                return self._sanitize_entry({
                    **base,
                    "state": "cleaned",
                    "reason": "锁定的旧 Sub2API account_id 已消失，并确认替代账号保持健康",
                    "replacement_account_id": replacement,
                    "cleanup_account_id": old_id,
                })

        if not email:
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "无法唯一提取邮箱，禁止自动补跑或清理"})
        if not old_exists:
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "检测时锁定的旧 account_id 已变化或不存在，禁止清理"})

        if replacement:
            replacement_row = by_id.get(replacement)
            if replacement_row and replacement_row.get("email") == email and kinds.get(replacement) == "healthy":
                return self._sanitize_entry({
                    **base,
                    "state": "cleanup_ready",
                    "reason": "已确认同邮箱新 account_id 且旧 account_id 仍存在，可显式清理旧账号",
                    "replacement_account_id": replacement,
                    "cleanup_account_id": old_id,
                })
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "已记录的替代 account_id 不再满足健康与邮箱一致性条件", "replacement_account_id": ""})

        new_ids = [account_id for account_id in current_ids if account_id not in set(pre_ids)]
        if local_status in _FAILED_LOCAL_STATES:
            return self._sanitize_entry({
                **base,
                "state": "failed",
                "reason": "Codex 补跑失败；旧 Sub2API account_id 保持不变",
                "retryable": kinds.get(old_id) == "unauthorized",
            })
        if len(new_ids) > 1:
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "补跑后出现多个同邮箱新 account_id，无法唯一确认替代账号"})
        if len(new_ids) == 1:
            candidate = new_ids[0]
            if kinds.get(candidate) != "healthy":
                return self._sanitize_entry({**base, "state": "failed", "reason": reasons.get(candidate) or "唯一新 account_id 未通过刷新验证"})
            return self._sanitize_entry({
                **base,
                "state": "cleanup_ready",
                "reason": "已确认同邮箱新 account_id 且旧 account_id 仍存在，可显式清理旧账号",
                "replacement_account_id": candidate,
                "cleanup_account_id": old_id,
            })
        if local_status in _RUNNING_LOCAL_STATES:
            return self._sanitize_entry({**base, "state": "retrying", "reason": "Codex 补跑进行中；尚未出现唯一同邮箱新 account_id"})
        return self._sanitize_entry({
            **base,
            "state": "ambiguous",
            "reason": "补跑后没有出现可唯一验证的同邮箱新 account_id，禁止清理",
            "retryable": kinds.get(old_id) == "unauthorized" and local_status in _FAILED_LOCAL_STATES,
        })

    @staticmethod
    def _historical_keeper(group: list[dict], kinds: dict[str, str]) -> tuple[str, str]:
        """Return deterministic keeper or an ambiguity reason for duplicate emails."""
        if any(row.get("email_ambiguous") for row in group):
            return "", "同邮箱候选存在邮箱歧义，禁止自动确认 keeper"
        healthy = [row for row in group if kinds.get(row["account_id"]) == "healthy"]
        if len(healthy) == 1 and all(
            kinds.get(row["account_id"]) == "unauthorized" for row in group if row not in healthy
        ):
            return healthy[0]["account_id"], "唯一健康账号为 keeper"
        if len(healthy) < 2 or len(healthy) != len(group):
            return "", "同邮箱账号健康状态无法唯一确认 keeper"
        dated = [row for row in healthy if row.get("created_at")]
        if len(dated) != len(healthy):
            return "", "同邮箱账号缺少合法 created_at，无法唯一确认 keeper"
        latest = max(row["created_at"] for row in dated)
        newest = [row for row in dated if row["created_at"] == latest]
        if len(newest) != 1:
            return "", "同邮箱账号 created_at 并列最新，无法唯一确认 keeper"
        return newest[0]["account_id"], "唯一最新账号为 keeper"

    def _persist_post_snapshot_error(self, accounts: list[dict], reason: str) -> None:
        by_id, by_email = self._account_maps(accounts)
        checked_at = _now()
        changed = False
        with self._lock:
            for account_id, item in list(self._tracked.items()):
                if item.get("state") != "retrying" or not item.get("pre_retry_account_ids"):
                    continue
                email = _email(item.get("email"))
                current_ids = _dedupe_ids([row.get("account_id") for row in by_email.get(email, [])])
                self._tracked[account_id] = self._sanitize_entry({
                    **item,
                    "state": "ambiguous",
                    "reason": _reason(reason, "Sub2API 后置验证失败"),
                    "observed_account_ids": current_ids or _dedupe_ids(item.get("observed_account_ids")),
                    "post_retry_account_ids": current_ids,
                    "post_retry_snapshot_at": checked_at,
                    "candidate_count": len(by_email.get(email, [])),
                    "replacement_account_id": "",
                    "cleanup_account_id": "",
                    "retryable": False,
                })
                changed = True
            if changed:
                self._persist_locked()

    def detect(self, local_status: dict[str, str] | None = None) -> dict:
        with self._operation_lock:
            return self._detect_locked(local_status)

    def _detect_locked(self, local_status: dict[str, str] | None = None) -> dict:
        local_status = {_email(key): _text(value, 64).lower() for key, value in (local_status or {}).items() if _email(key)}
        with self._operation_lock:
            accounts = self._list_fn()
            account_ids = _dedupe_ids([(row.get("account_id") or row.get("id")) for row in accounts if isinstance(row, dict)])
            try:
                refresh_payload = self._refresh_fn(account_ids)
            except Exception:
                self._persist_post_snapshot_error(accounts, "Sub2API 后置刷新失败；已保存补跑后 account_id 快照，旧账号保持不变")
                raise

        by_id, by_email = self._account_maps(accounts)
        kinds, reasons = self._refresh_maps(account_ids, refresh_payload)
        checked_at = _now()
        health_counts = {kind: 0 for kind in _HEALTH_STATES}
        for account_id in account_ids:
            health_counts[kinds.get(account_id, "unknown")] += 1

        with self._lock:
            previous = {account_id: dict(item) for account_id, item in self._tracked.items()}
            next_tracked: dict[str, dict] = {}
            processed_ids: set[str] = set()

            for account_id, old in previous.items():
                if not old.get("pre_retry_account_ids"):
                    continue
                item = self._evaluate_after_retry(
                    old,
                    by_id,
                    by_email,
                    kinds,
                    reasons,
                    local_status=local_status.get(_email(old.get("email")), ""),
                    checked_at=checked_at,
                )
                next_tracked[account_id] = item
                processed_ids.update(item.get("post_retry_account_ids") or [])
                processed_ids.update(item.get("pre_retry_account_ids") or [])

            for account_id, account in sorted(by_id.items()):
                if account_id in processed_ids:
                    continue
                email = _email(account.get("email"))
                group = by_email.get(email, []) if email else []
                health = kinds.get(account_id, "unknown")
                keeper = ""
                replacement_id = ""
                cleanup_id = ""
                preserve_decision_reason = False
                if account.get("email_ambiguous") or not email:
                    state, reason, retryable = "ambiguous", "无法唯一提取邮箱，禁止自动补跑或清理", False
                    preserve_decision_reason = True
                elif len(group) != 1:
                    preserve_decision_reason = True
                    keeper, keeper_reason = self._historical_keeper(group, kinds)
                    if keeper and account_id == keeper:
                        continue
                    if keeper:
                        state, reason, retryable = "cleanup_ready", f"{keeper_reason}，可精确清理旧 account_id", False
                        replacement_id, cleanup_id = keeper, account_id
                    else:
                        state, reason, retryable = "ambiguous", keeper_reason, False
                        replacement_id, cleanup_id = "", ""
                elif health == "unauthorized":
                    state, reason, retryable = "pending", "Sub2API 刷新返回 unauthorized/401", True
                elif health in {"timeout", "unknown"}:
                    state = "ambiguous"
                    reason = "Sub2API 刷新超时" if health == "timeout" else "Sub2API 刷新结果未知"
                    retryable = False
                else:
                    continue
                next_tracked[account_id] = self._sanitize_entry({
                    "account_id": account_id,
                    "account_name": account.get("account_name"),
                    "email": email,
                    "created_at": account.get("created_at"),
                    "state": state,
                    "health": health,
                    "reason": reason if preserve_decision_reason else reasons.get(account_id) or reason,
                    "observed_account_ids": [row.get("account_id") for row in group] or [account_id],
                    "pre_retry_account_ids": [],
                    "post_retry_account_ids": [],
                    "replacement_account_id": replacement_id if len(group) != 1 and keeper else "",
                    "cleanup_account_id": cleanup_id if len(group) != 1 and keeper else "",
                    "candidate_count": len(group) if email else 1,
                    "checked_at": checked_at,
                    "retryable": retryable,
                    "local_codex_status": local_status.get(email, ""),
                })

            self._tracked = next_tracked
            self._health_counts = health_counts
            self._persist_locked()
            visible = self._public_items_locked()

        return {
            "ok": True,
            "supported": True,
            "cleanup_supported": self.cleanup_supported,
            "items": visible,
            "counts": self._counts(visible),
            "checked_at": checked_at,
        }

    def select_retryable(self, values: list[object], *, limit: int = 50) -> tuple[list[dict], list[dict]]:
        selected: list[dict] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        with self._lock:
            for raw in values[: limit + 1]:
                account_id = _account_id(raw)
                if not account_id:
                    skipped.append({"account_id": "", "reason": "account_id 非法"})
                    continue
                if account_id in seen:
                    continue
                seen.add(account_id)
                item = self._tracked.get(account_id)
                if not item:
                    skipped.append({"account_id": account_id, "reason": "不在最近一次 Sub2API 401 检测结果中"})
                elif not item.get("retryable"):
                    skipped.append({"account_id": account_id, "reason": "当前状态不可补跑"})
                else:
                    selected.append(self._sanitize_entry(item))
        if len(values) > limit:
            skipped.append({"account_id": "", "reason": f"单次最多选择 {limit} 个账号"})
        return selected, skipped

    def mark_retrying(self, account_ids: list[str], *, batch_id: str = "") -> None:
        with self._operation_lock:
            self._mark_retrying_locked(account_ids, batch_id=batch_id)

    def _mark_retrying_locked(self, account_ids: list[str], *, batch_id: str = "") -> None:
        now = _now()
        selected = _dedupe_ids(account_ids, limit=50)
        with self._operation_lock:
            accounts = self._list_fn()
        by_id, by_email = self._account_maps(accounts)
        conflicts: list[str] = []
        with self._lock:
            for account_id in selected:
                item = self._tracked.get(account_id)
                email = _email(item.get("email")) if item else ""
                current_ids = _dedupe_ids([row.get("account_id") for row in by_email.get(email, [])])
                expected_ids = _dedupe_ids(item.get("observed_account_ids")) if item else []
                if not item or not item.get("retryable") or account_id not in by_id or current_ids != expected_ids:
                    if item:
                        self._tracked[account_id] = self._sanitize_entry({
                            **item,
                            "state": "ambiguous",
                            "reason": "补跑前 Sub2API account_id 快照已变化，请重新检测；旧账号保持不变",
                            "observed_account_ids": current_ids or expected_ids,
                            "pre_retry_account_ids": [],
                            "post_retry_account_ids": [],
                            "replacement_account_id": "",
                            "cleanup_account_id": "",
                            "candidate_count": len(by_email.get(email, [])),
                            "checked_at": now,
                            "retryable": False,
                        })
                    conflicts.append(account_id)
            if conflicts:
                self._persist_locked()
                raise Sub2ApiSnapshotConflict(conflicts)

            for account_id in selected:
                item = self._tracked[account_id]
                snapshot = _dedupe_ids(item.get("observed_account_ids") or [account_id])
                self._tracked[account_id] = self._sanitize_entry({
                    **item,
                    "state": "retrying",
                    "reason": "已启动 Codex OAuth 补跑，等待 Sub2API 新 account_id 后置验证",
                    "pre_retry_account_ids": snapshot,
                    "post_retry_account_ids": [],
                    "replacement_account_id": "",
                    "cleanup_account_id": "",
                    "pre_retry_snapshot_at": now,
                    "post_retry_snapshot_at": "",
                    "retry_started_at": now,
                    "retry_batch_id": _text(batch_id, 64),
                    "retryable": False,
                })
            self._persist_locked()

    def mark_verification_error(self, account_ids: list[str], reason: str) -> None:
        with self._operation_lock:
            self._mark_verification_error_locked(account_ids, reason)

    def _mark_verification_error_locked(self, account_ids: list[str], reason: str) -> None:
        with self._lock:
            for account_id in _dedupe_ids(account_ids, limit=50):
                item = self._tracked.get(account_id)
                if not item or item.get("state") != "retrying":
                    continue
                self._tracked[account_id] = self._sanitize_entry({
                    **item,
                    "state": "ambiguous",
                    "reason": _reason(reason, "Sub2API 后置验证失败"),
                    "retryable": False,
                })
            self._persist_locked()

    def cleanup(self, values: list[object], *, opt_in: bool = False, limit: int = 50) -> dict:
        with self._operation_lock:
            return self._cleanup_locked(values, opt_in=opt_in, limit=limit)

    def _cleanup_locked(self, values: list[object], *, opt_in: bool = False, limit: int = 50) -> dict:
        selected = _dedupe_ids(values, limit=limit)
        with self._lock:
            review = []
            for account_id in selected:
                item = self._tracked.get(account_id)
                if not item or item.get("state") != "cleanup_ready":
                    continue
                review.append({
                    "account_id": account_id,
                    "email": item.get("email"),
                    "replacement_account_id": item.get("replacement_account_id"),
                    "cleanup_account_id": item.get("cleanup_account_id"),
                    "manual_action": "仅删除检测与验证阶段锁定的旧 account_id",
                })
        if not opt_in or not review:
            return {
                "ok": True,
                "cleanup_supported": self.cleanup_supported,
                "cleanup_performed": False,
                "items": review,
                "manual_action": "确认替代账号和旧 account_id 后，勾选显式确认再执行删除",
            }

        def matches_review(current: dict | None, review_item: dict) -> bool:
            return bool(
                current
                and current.get("state") == "cleanup_ready"
                and _account_id(current.get("cleanup_account_id")) == _account_id(review_item.get("cleanup_account_id"))
                and _account_id(current.get("replacement_account_id")) == _account_id(review_item.get("replacement_account_id"))
                and _email(current.get("email")) == _email(review_item.get("email"))
            )

        def persist_operational_failure(message: str) -> None:
            with self._lock:
                for review_item in review:
                    account_id = _account_id(review_item.get("account_id"))
                    current = self._tracked.get(account_id)
                    if not matches_review(current, review_item):
                        continue
                    self._tracked[account_id] = self._sanitize_entry({
                        **current,
                        "state": "cleanup_ready",
                        "reason": message,
                        "cleanup_account_id": account_id,
                    })
                self._persist_locked()

        stale_review_ids: set[str] = set()
        tracked_snapshots: dict[str, dict] = {}
        cleaned_ids_by_keeper: dict[tuple[str, str], set[str]] = {}
        with self._lock:
            for tracked_id, tracked_item in self._tracked.items():
                if tracked_item.get("state") != "cleaned":
                    continue
                key = (
                    _email(tracked_item.get("email")),
                    _account_id(tracked_item.get("replacement_account_id")),
                )
                cleaned_ids_by_keeper.setdefault(key, set()).add(_account_id(tracked_id))
            for review_item in review:
                account_id = _account_id(review_item.get("account_id"))
                current = self._tracked.get(account_id)
                if matches_review(current, review_item):
                    tracked_snapshots[account_id] = self._sanitize_entry(current)
                else:
                    stale_review_ids.add(account_id)

        try:
            before = self._list_fn()
        except Exception:
            persist_operational_failure("清理前无法重新读取 Sub2API 账号，未执行删除；请重试清理")
            raise

        before_by_id, before_by_email = self._account_maps(before)
        replacement_ids = _dedupe_ids([
            item.get("replacement_account_id")
            for item in review
            if _account_id(item.get("account_id")) in tracked_snapshots
        ])
        try:
            refresh_payload = self._refresh_fn(replacement_ids) if replacement_ids else {
                "total": 0,
                "success": 0,
                "failed": 0,
                "errors": [],
            }
        except Exception:
            persist_operational_failure("清理前无法刷新替代 account_id，未执行删除；请重试清理")
            raise
        replacement_kinds, _ = self._refresh_maps(replacement_ids, refresh_payload)

        precheck_errors: dict[str, str] = {}
        ambiguous_ids: set[str] = set()
        reconciled_ids: set[str] = set()
        before_group_ids: dict[str, list[str]] = {}
        eligible_ids: list[str] = []
        for review_item in review:
            account_id = _account_id(review_item.get("account_id"))
            old_id = _account_id(review_item.get("cleanup_account_id"))
            replacement_id = _account_id(review_item.get("replacement_account_id"))
            email = _email(review_item.get("email"))
            if account_id in stale_review_ids:
                precheck_errors[account_id] = "清理状态已变化，未执行删除"
                continue
            tracked = tracked_snapshots.get(account_id) or {}
            if not old_id or old_id != account_id:
                precheck_errors[account_id] = "清理目标不是检测时锁定的旧 account_id"
                ambiguous_ids.add(account_id)
                continue

            current_ids = _dedupe_ids([
                row.get("account_id") for row in before_by_email.get(email, [])
            ])
            before_group_ids[account_id] = current_ids
            expected_ids = _dedupe_ids(
                tracked.get("post_retry_account_ids")
                or tracked.get("observed_account_ids")
            )
            known_cleaned = cleaned_ids_by_keeper.get((email, replacement_id), set())
            expected_live = [value for value in expected_ids if value not in known_cleaned]
            old_already_absent = (
                old_id not in current_ids
                and set(current_ids) == (set(expected_live) - {old_id})
            )
            if not expected_live or (
                set(current_ids) != set(expected_live) and not old_already_absent
            ):
                precheck_errors[account_id] = "清理前同邮箱 account_id 快照已变化，未执行删除；请重新检测"
                ambiguous_ids.add(account_id)
                continue

            replacement_row = before_by_id.get(replacement_id)
            if not replacement_row or replacement_row.get("email") != email:
                precheck_errors[account_id] = "替代 account_id 不存在或邮箱不一致，未执行删除；请重新检测"
                ambiguous_ids.add(account_id)
                continue
            if replacement_kinds.get(replacement_id) != "healthy":
                precheck_errors[account_id] = "替代 account_id 未通过刷新验证，未执行删除；请重试清理"
                if old_already_absent:
                    ambiguous_ids.add(account_id)
                continue
            if old_already_absent:
                reconciled_ids.add(account_id)
                continue
            if old_id not in before_by_id:
                precheck_errors[account_id] = "检测时锁定的旧 account_id 已不存在，但快照无法确认"
                ambiguous_ids.add(account_id)
                continue
            eligible_ids.append(account_id)

        attempts: dict[str, bool] = {}
        for account_id in eligible_ids:
            try:
                result = self._delete_fn(account_id)
                attempts[account_id] = (
                    bool(result.get("ok", result.get("success")))
                    if isinstance(result, dict)
                    else result is True
                )
            except Exception:
                attempts[account_id] = False

        after_error = ""
        after_refresh_error = ""
        if eligible_ids:
            try:
                after = self._list_fn()
            except Exception:
                after = []
                after_error = "删除请求后无法重新读取 Sub2API 账号，保留清理状态等待复核"
        else:
            after = before
        after_by_id, after_by_email = self._account_maps(after)

        post_replacement_kinds = replacement_kinds
        if not after_error and (eligible_ids or reconciled_ids):
            try:
                post_refresh = self._refresh_fn(replacement_ids) if replacement_ids else {
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "errors": [],
                }
                post_replacement_kinds, _ = self._refresh_maps(replacement_ids, post_refresh)
            except Exception:
                post_replacement_kinds = {}
                after_refresh_error = "清理后无法刷新替代 account_id，清理结果等待复核"

        absent_ids_by_email: dict[str, set[str]] = {}
        for review_item in review:
            old_id = _account_id(review_item.get("cleanup_account_id"))
            email = _email(review_item.get("email"))
            if old_id and old_id not in after_by_id:
                absent_ids_by_email.setdefault(email, set()).add(old_id)

        results = []
        with self._lock:
            for review_item in review:
                account_id = _account_id(review_item.get("account_id"))
                old_id = _account_id(review_item.get("cleanup_account_id"))
                replacement_id = _account_id(review_item.get("replacement_account_id"))
                email = _email(review_item.get("email"))
                tracked = self._tracked.get(account_id) or {}
                stale_review = account_id in stale_review_ids
                error = precheck_errors.get(account_id, "")
                state = "cleanup_ready"

                if not error and after_error:
                    error = after_error
                current_after_ids = _dedupe_ids([
                    row.get("account_id") for row in after_by_email.get(email, [])
                ])
                if not error:
                    expected_after = set(before_group_ids.get(account_id, []))
                    expected_after -= absent_ids_by_email.get(email, set())
                    if set(current_after_ids) != expected_after:
                        error = "清理后同邮箱 account_id 快照发生变化，结果无法确认"
                        state = "ambiguous"
                replacement_row = after_by_id.get(replacement_id)
                if not error and (
                    not replacement_row or replacement_row.get("email") != email
                ):
                    error = "清理后替代 account_id 不存在或邮箱不一致，结果无法确认"
                    state = "ambiguous"
                if not error and after_refresh_error:
                    error = after_refresh_error
                    state = "ambiguous" if old_id not in after_by_id else "cleanup_ready"
                if not error and post_replacement_kinds.get(replacement_id) != "healthy":
                    error = "清理后替代 account_id 未通过刷新验证，结果无法确认"
                    state = "ambiguous" if old_id not in after_by_id else "cleanup_ready"

                absent = not after_error and old_id not in after_by_id
                ok = not error and absent and (
                    account_id in eligible_ids or account_id in reconciled_ids
                )
                if ok:
                    attempted = account_id in eligible_ids
                    self._tracked[account_id] = self._sanitize_entry({
                        **tracked,
                        "state": "cleaned",
                        "reason": (
                            "已删除锁定的旧 Sub2API account_id，并确认该 ID 已消失且替代账号健康"
                            if attempted
                            else "锁定的旧 Sub2API account_id 已不存在，并确认替代账号保持健康"
                        ),
                        "observed_account_ids": current_after_ids,
                        "post_retry_account_ids": current_after_ids,
                        "cleanup_account_id": old_id,
                        "retryable": False,
                    })
                    result_text = "deleted_and_verified" if attempted else "absent_and_verified"
                    result_reason = ""
                else:
                    if not error:
                        error = (
                            "Sub2API 删除请求失败"
                            if not attempts.get(old_id, False)
                            else "删除后旧 account_id 仍存在"
                        )
                    if account_id in ambiguous_ids:
                        state = "ambiguous"
                    if not stale_review:
                        self._tracked[account_id] = self._sanitize_entry({
                            **tracked,
                            "state": state,
                            "reason": error,
                            "cleanup_account_id": old_id,
                            "retryable": False,
                        })
                    result_text = "not_deleted"
                    result_reason = error
                results.append({
                    "account_id": account_id,
                    "cleanup_account_id": old_id,
                    "replacement_account_id": replacement_id,
                    "ok": ok,
                    "result": result_text,
                    "reason": result_reason,
                })
            self._persist_locked()

        return {
            "ok": True,
            "cleanup_supported": self.cleanup_supported,
            "cleanup_performed": True,
            "items": results,
        }
