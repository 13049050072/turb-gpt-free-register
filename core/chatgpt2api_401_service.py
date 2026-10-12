# -*- coding: utf-8 -*-
"""Safe in-place replacement of invalid ChatGPT2API access tokens."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core import db, liveness_runner
from core.chatgpt2api_admin import ChatGPT2APIAdminClient

_MAX_ID = 160
_MAX_REASON = 240
_MAX_ITEMS = 50
_STATES = {"pending", "refreshing", "ready", "replaced", "failed", "ambiguous", "conflict"}
# Only dedicated secret endpoints are authoritative for credential evidence.
# Email is verified by list mapping; password/client_id/last_refresh/id_token are
# sanitized or lack a supported getter and must not be guessed.
_PRESERVED_FIELDS = ("refresh_token",)
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
_OPERATION_LOCKS: dict[str, threading.RLock] = {}
_OPERATION_LOCKS_GUARD = threading.Lock()


def _shared_operation_lock(path: Path) -> threading.RLock:
    try:
        key = str(path.resolve(strict=False))
    except OSError:
        key = str(path.absolute())
    with _OPERATION_LOCKS_GUARD:
        lock = _OPERATION_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _OPERATION_LOCKS[key] = lock
        return lock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, limit: int) -> str:
    raw = "" if value is None else str(value)
    return " ".join(raw.replace("\x00", "").replace("\r", " ").replace("\n", " ").split())[:limit]


def _reason(value: object, fallback: str) -> str:
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


def _public_remote_id(value: object) -> str:
    remote_id = _remote_id(value)
    redacted = _SECRET_RE.sub(r"\1=[redacted]", remote_id)
    redacted = _BEARER_RE.sub("bearer [redacted]", redacted)
    redacted = _URL_RE.sub("[url-redacted]", redacted)
    return remote_id if redacted == remote_id else "[redacted]"


def _email(value: object) -> str:
    text = _text(value, 320).lower()
    return text if _EMAIL_RE.fullmatch(text) else ""


def _local_id(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def _bounded_count(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(1_000_000, parsed))


def _hash_token(value: object) -> str:
    token = str(value or "").strip()
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def _hash_value(*, present: bool, value: object) -> str:
    payload = json.dumps(
        {"present": bool(present), "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _SHA256_RE.fullmatch(text) else ""


def _selected_ids(values: object, *, limit: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _remote_id(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _remote_invalid(row: dict) -> bool:
    if row.get("invalid") is True:
        return True
    access_status = _text(row.get("access_status") or row.get("access_token_status") or row.get("token_status") or row.get("status"), 48).lower()
    remote_result = _text(row.get("remote_check_result") or row.get("last_remote_check_result") or row.get("last_remote_check_status") or row.get("check_result"), 48).lower()
    reason = _text(
        row.get("reason")
        or row.get("status_reason")
        or row.get("status_raw_error")
        or row.get("last_remote_check_error")
        or row.get("error")
        or row.get("last_error")
        or row.get("message"),
        1000,
    )
    return (
        access_status in {"invalid", "expired", "token_expired", "unauthorized", "401"}
        or remote_result in {"invalid", "expired", "token_expired", "unauthorized", "401"}
        or bool(_INVALID_RE.search(reason))
    )


def _default_local_accounts() -> list[dict]:
    return db.list_accounts(limit=100_000, archived="all")


class ChatGPT2API401Service:
    """Verification-first state machine for exact-ID, access-token-only PATCHes."""

    def __init__(
        self,
        *,
        list_remote_fn: Callable[[], list[dict]] | None = None,
        snapshot_fn: Callable[[str], dict] | None = None,
        patch_fn: Callable[[str, str], object] | None = None,
        list_local_fn: Callable[[], list[dict]] | None = None,
        liveness_fn: Callable[[int], dict] | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        if list_remote_fn is None or snapshot_fn is None or patch_fn is None:
            client = ChatGPT2APIAdminClient()
        self._list_remote_fn = list_remote_fn or client.list_accounts
        self._snapshot_fn = snapshot_fn or client.credential_snapshot
        self._patch_fn = patch_fn or client.patch_access_token
        self._list_local_fn = list_local_fn or _default_local_accounts
        self._liveness_fn = liveness_fn or liveness_runner.run_liveness_check
        self._audit_path = Path(audit_path) if audit_path is not None else Path(__file__).resolve().parent.parent / "chatgpt2api_401_audit.json"
        self._lock = threading.RLock()
        self._operation_lock = _shared_operation_lock(self._audit_target())
        self._checked_at = ""
        self._remote_counts = {"total": 0, "invalid": 0, "healthy": 0}
        self._tracked = self._load_audit()

    def _audit_target(self) -> Path:
        try:
            if self._audit_path.is_symlink():
                return self._audit_path.resolve(strict=False)
        except OSError:
            pass
        return self._audit_path

    def _normalize_snapshot(self, raw: object) -> dict:
        data = raw if isinstance(raw, dict) else {}
        at_sha256 = _sha256(data.get("at_sha256") or data.get("access_token_sha256"))
        if not at_sha256:
            at_sha256 = _hash_token(data.get("access_token") or data.get("accessToken"))
        raw_preserved = data.get("preserved_sha256") if isinstance(data.get("preserved_sha256"), dict) else {}
        preserved: dict[str, str] = {}
        for field in _PRESERVED_FIELDS:
            digest = _sha256(raw_preserved.get(field))
            if not digest and field in data:
                digest = _hash_value(present=True, value=data.get(field))
            preserved[field] = digest
        return {
            "remote_id": _remote_id(data.get("remote_id") or data.get("id") or data.get("management_id") or data.get("account_id")),
            "at_sha256": at_sha256,
            "preserved_sha256": preserved,
        }

    @staticmethod
    def _snapshot_complete(snapshot: dict, remote_id: str) -> bool:
        snapshot_id = _remote_id(snapshot.get("remote_id"))
        if snapshot_id and snapshot_id != remote_id:
            return False
        return bool(
            _sha256(snapshot.get("at_sha256"))
            and all(_sha256((snapshot.get("preserved_sha256") or {}).get(field)) for field in _PRESERVED_FIELDS)
        )

    def _sanitize_entry(self, raw: dict) -> dict:
        state = _text(raw.get("state") or "ambiguous", 32).lower()
        if state not in _STATES:
            state = "ambiguous"
        preserved = raw.get("preserved_sha256") if isinstance(raw.get("preserved_sha256"), dict) else {}
        return {
            "remote_id": _remote_id(raw.get("remote_id")),
            "email": _email(raw.get("email")),
            "local_account_id": _local_id(raw.get("local_account_id")),
            "state": state,
            "reason": _reason(raw.get("reason"), state),
            "old_at_sha256": _sha256(raw.get("old_at_sha256")),
            "new_at_sha256": _sha256(raw.get("new_at_sha256")),
            "preserved_sha256": {
                field: _sha256(preserved.get(field))
                for field in _PRESERVED_FIELDS
            },
            "detected_at": _text(raw.get("detected_at"), 64),
            "refresh_started_at": _text(raw.get("refresh_started_at"), 64),
            "refresh_completed_at": _text(raw.get("refresh_completed_at"), 64),
            "replaced_at": _text(raw.get("replaced_at"), 64),
            "retryable": bool(raw.get("retryable")) and state in {"pending", "failed"},
            "replaceable": bool(raw.get("replaceable")) and state == "ready",
        }

    def _load_audit(self) -> dict[str, dict]:
        try:
            payload = json.loads(self._audit_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if isinstance(payload, dict):
            self._checked_at = _text(payload.get("checked_at"), 64)
            counts = payload.get("remote_counts")
            if isinstance(counts, dict):
                self._remote_counts = {
                    key: _bounded_count(counts.get(key))
                    for key in ("total", "invalid", "healthy")
                }
        raw_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(raw_items, dict):
            return {}
        result: dict[str, dict] = {}
        for key, value in raw_items.items():
            if not isinstance(value, dict):
                continue
            stored_remote_id = _remote_id(value.get("remote_id"))
            if stored_remote_id or not str(key).startswith("ambiguous-"):
                value = {**value, "remote_id": stored_remote_id or key}
            item = self._sanitize_entry(value)
            if item["state"] == "refreshing":
                item = self._sanitize_entry({
                    **item,
                    "state": "failed",
                    "reason": "上次查活在完成前中断，必须重新执行并验证新 AT",
                    "new_at_sha256": "",
                    "retryable": True,
                    "replaceable": False,
                })
            track_key = item["remote_id"] or _remote_id(key)
            if track_key:
                result[track_key] = item
        return result

    def _reload_audit_locked(self) -> None:
        with self._lock:
            self._checked_at = ""
            self._remote_counts = {"total": 0, "invalid": 0, "healthy": 0}
            self._tracked = self._load_audit()

    def _persist_locked(self) -> None:
        target = self._audit_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now(),
            "checked_at": self._checked_at,
            "remote_counts": dict(self._remote_counts),
            "items": {
                remote_id: self._sanitize_entry(item)
                for remote_id, item in sorted(self._tracked.items())
            },
        }
        temp_path = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(temp_path, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
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

    @staticmethod
    def _remote_maps(rows: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        by_id: dict[str, list[dict]] = {}
        by_email: dict[str, list[dict]] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            remote_id = _remote_id(raw.get("remote_id") or raw.get("id") or raw.get("management_id") or raw.get("account_id"))
            email = _email(raw.get("email"))
            row = {**raw, "remote_id": remote_id, "email": email}
            if remote_id:
                by_id.setdefault(remote_id, []).append(row)
            if email:
                by_email.setdefault(email, []).append(row)
        return by_id, by_email

    @staticmethod
    def _local_maps(rows: list[dict]) -> tuple[dict[int, dict], dict[str, list[dict]]]:
        by_id: dict[int, dict] = {}
        by_email: dict[str, list[dict]] = {}
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            local_id = _local_id(raw.get("id") or raw.get("account_id"))
            email = _email(raw.get("email"))
            if local_id and local_id not in by_id:
                by_id[local_id] = raw
            if email:
                by_email.setdefault(email, []).append(raw)
        return by_id, by_email

    def _public_item(self, item: dict) -> dict:
        clean = self._sanitize_entry(item)
        return {
            "remote_id": clean["remote_id"],
            "email": clean["email"],
            "local_account_id": clean["local_account_id"],
            "state": clean["state"],
            "reason": clean["reason"],
            "old_at_fingerprint": clean["old_at_sha256"][:12],
            "new_at_fingerprint": clean["new_at_sha256"][:12],
            "detected_at": clean["detected_at"],
            "refresh_started_at": clean["refresh_started_at"],
            "refresh_completed_at": clean["refresh_completed_at"],
            "replaced_at": clean["replaced_at"],
            "retryable": clean["retryable"],
            "replaceable": clean["replaceable"],
        }

    def _state_locked(self) -> dict:
        items = [self._public_item(item) for _, item in sorted(self._tracked.items())]
        counts = {state: 0 for state in sorted(_STATES)}
        for item in items:
            counts[item["state"]] += 1
        counts.update(self._remote_counts)
        return {
            "ok": True,
            "supported": True,
            "items": items,
            "counts": counts,
            "checked_at": self._checked_at,
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()

    def detect(self) -> dict:
        with self._operation_lock:
            self._reload_audit_locked()
            remote_rows = list(self._list_remote_fn() or [])
            local_rows = list(self._list_local_fn() or [])
            by_remote_id, by_remote_email = self._remote_maps(remote_rows)
            _by_local_id, by_local_email = self._local_maps(local_rows)
            checked_at = _now()
            invalid_rows = [row for row in remote_rows if isinstance(row, dict) and _remote_invalid(row)]
            next_tracked: dict[str, dict] = {}

            for raw in invalid_rows:
                remote_id = _remote_id(raw.get("remote_id") or raw.get("id") or raw.get("management_id") or raw.get("account_id"))
                email = _email(raw.get("email"))
                base = {
                    "remote_id": remote_id,
                    "email": email,
                    "local_account_id": 0,
                    "state": "ambiguous",
                    "reason": "远端账号无法安全映射",
                    "old_at_sha256": "",
                    "new_at_sha256": "",
                    "preserved_sha256": {},
                    "detected_at": checked_at,
                    "refresh_started_at": "",
                    "refresh_completed_at": "",
                    "replaced_at": "",
                    "retryable": False,
                    "replaceable": False,
                }
                key = remote_id or f"ambiguous-{len(next_tracked) + 1}"
                if not remote_id or len(by_remote_id.get(remote_id, [])) != 1:
                    base["reason"] = "远端稳定 ID 缺失或重复，禁止刷新与替换"
                elif raw.get("email_ambiguous") is True or not email:
                    base["reason"] = "远端邮箱缺失或冲突，禁止刷新与替换"
                elif len(by_remote_email.get(email, [])) != 1:
                    base["reason"] = "同一邮箱对应多个远端账号，禁止刷新与替换"
                elif len(by_local_email.get(email, [])) != 1:
                    base["reason"] = "邮箱无法唯一映射到本地账号，禁止刷新与替换"
                else:
                    local = by_local_email[email][0]
                    local_id = _local_id(local.get("id") or local.get("account_id"))
                    if not local_id:
                        base["reason"] = "本地账号缺少稳定 ID，禁止刷新与替换"
                    else:
                        try:
                            snapshot = self._normalize_snapshot(self._snapshot_fn(remote_id))
                        except Exception as exc:
                            base["reason"] = _reason(exc, "远端凭证快照读取失败")
                        else:
                            if not self._snapshot_complete(snapshot, remote_id):
                                base["reason"] = "远端快照缺少旧 AT 或保留字段摘要，禁止刷新与替换"
                            else:
                                base.update({
                                    "local_account_id": local_id,
                                    "state": "pending",
                                    "reason": _reason(raw.get("reason"), "远端 AT 已失效或返回 401"),
                                    "old_at_sha256": snapshot["at_sha256"],
                                    "preserved_sha256": snapshot["preserved_sha256"],
                                    "retryable": True,
                                })
                                previous = self._tracked.get(remote_id) or {}
                                previous_clean = self._sanitize_entry(previous) if previous else {}
                                local_sha = _hash_token(local.get("access_token"))
                                if (
                                    previous_clean.get("state") == "ready"
                                    and previous_clean.get("old_at_sha256") == snapshot["at_sha256"]
                                    and previous_clean.get("preserved_sha256") == snapshot["preserved_sha256"]
                                    and previous_clean.get("new_at_sha256") == local_sha
                                ):
                                    base.update(previous_clean)
                                elif (
                                    previous_clean.get("state") == "replaced"
                                    and previous_clean.get("new_at_sha256") == snapshot["at_sha256"]
                                    and previous_clean.get("preserved_sha256") == snapshot["preserved_sha256"]
                                ):
                                    base.update(previous_clean)
                next_tracked[key] = self._sanitize_entry(base)

            with self._lock:
                self._tracked = next_tracked
                self._checked_at = checked_at
                self._remote_counts = {
                    "total": len(remote_rows),
                    "invalid": len(invalid_rows),
                    "healthy": max(0, len(remote_rows) - len(invalid_rows)),
                }
                self._persist_locked()
                return self._state_locked()

    def select_refreshable(self, values: object, *, limit: int = _MAX_ITEMS) -> tuple[list[dict], list[dict]]:
        selected = _selected_ids(values, limit=limit)
        accepted: list[dict] = []
        skipped: list[dict] = []
        with self._lock:
            for remote_id in selected:
                item = self._tracked.get(remote_id)
                if item and item.get("retryable") and item.get("state") in {"pending", "failed"}:
                    accepted.append(self._sanitize_entry(item))
                else:
                    skipped.append({"remote_id": _public_remote_id(remote_id), "reason": "账号不在可查活替换状态"})
        return accepted, skipped

    def _set_item(self, remote_id: str, updates: dict) -> dict:
        with self._lock:
            current = self._tracked.get(remote_id) or {"remote_id": remote_id}
            item = self._sanitize_entry({**current, **updates})
            self._tracked[remote_id] = item
            self._persist_locked()
            return item

    def refresh(self, values: object, *, limit: int = _MAX_ITEMS) -> dict:
        with self._operation_lock:
            self._reload_audit_locked()
            selected, skipped = self.select_refreshable(values, limit=limit)
            results: list[dict] = []
            for item in selected:
                remote_id = item["remote_id"]
                email = item["email"]
                local_id = item["local_account_id"]
                remote_rows = list(self._list_remote_fn() or [])
                local_rows = list(self._list_local_fn() or [])
                by_remote_id, by_remote_email = self._remote_maps(remote_rows)
                by_local_id, by_local_email = self._local_maps(local_rows)
                remote_matches = by_remote_id.get(remote_id, [])
                local = by_local_id.get(local_id)
                conflict_reason = ""
                if len(remote_matches) != 1 or len(by_remote_email.get(email, [])) != 1:
                    conflict_reason = "远端 ID 或邮箱映射已变化，查活已取消"
                elif len(by_local_email.get(email, [])) != 1 or local is None or _email(local.get("email")) != email:
                    conflict_reason = "本地账号映射已变化，查活已取消"
                else:
                    try:
                        live_snapshot = self._normalize_snapshot(self._snapshot_fn(remote_id))
                    except Exception as exc:
                        conflict_reason = _reason(exc, "远端凭证快照复核失败")
                    else:
                        if not self._snapshot_complete(live_snapshot, remote_id):
                            conflict_reason = "远端凭证快照不完整或账号 ID 不一致"
                        elif live_snapshot["at_sha256"] != item["old_at_sha256"]:
                            conflict_reason = "远端旧 AT 已变化，查活已取消"
                        elif live_snapshot["preserved_sha256"] != item["preserved_sha256"]:
                            conflict_reason = "远端保留字段已变化，查活已取消"
                if conflict_reason:
                    updated = self._set_item(remote_id, {
                        "state": "conflict",
                        "reason": conflict_reason,
                        "retryable": False,
                        "replaceable": False,
                    })
                    results.append({"remote_id": remote_id, "ok": False, "state": updated["state"], "reason": updated["reason"]})
                    continue

                before_local_sha = _hash_token(local.get("access_token"))
                self._set_item(remote_id, {
                    "state": "refreshing",
                    "reason": "正在执行本地账号查活并持久化新 AT",
                    "refresh_started_at": _now(),
                    "retryable": False,
                    "replaceable": False,
                })
                try:
                    liveness = self._liveness_fn(local_id)
                except Exception as exc:
                    liveness = {"ok": False, "error": _reason(exc, "本地查活执行失败")}
                local_after_rows = list(self._list_local_fn() or [])
                local_after_by_id, local_after_by_email = self._local_maps(local_after_rows)
                local_after = local_after_by_id.get(local_id)
                returned_sha = _hash_token(
                    (liveness or {}).get("access_token") or (liveness or {}).get("accessToken")
                ) if isinstance(liveness, dict) else ""
                new_sha = _hash_token((local_after or {}).get("access_token"))
                failure = ""
                if not isinstance(liveness, dict) or liveness.get("ok") is not True:
                    failure = _reason((liveness or {}).get("error") if isinstance(liveness, dict) else "", "本地查活失败")
                elif not returned_sha:
                    failure = "查活成功响应未返回可验证的新 AT"
                elif local_after is None or _email(local_after.get("email")) != email or len(local_after_by_email.get(email, [])) != 1:
                    failure = "查活后本地账号映射发生变化"
                elif not new_sha:
                    failure = "查活未在本地持久化可验证的新 AT"
                elif returned_sha != new_sha:
                    failure = "查活返回的 AT 与本地持久化 AT 不一致"
                elif returned_sha == item["old_at_sha256"]:
                    failure = "查活返回的 AT 与远端失效 AT 相同"
                elif before_local_sha and new_sha == before_local_sha:
                    failure = "查活未生成新的本地 AT"
                if failure:
                    updated = self._set_item(remote_id, {
                        "state": "failed",
                        "reason": failure,
                        "new_at_sha256": "",
                        "refresh_completed_at": _now(),
                        "retryable": True,
                        "replaceable": False,
                    })
                    results.append({"remote_id": remote_id, "ok": False, "state": updated["state"], "reason": updated["reason"]})
                    continue
                updated = self._set_item(remote_id, {
                    "state": "ready",
                    "reason": "本地查活成功且新 AT 已持久化；等待显式确认 PATCH",
                    "new_at_sha256": new_sha,
                    "refresh_completed_at": _now(),
                    "retryable": False,
                    "replaceable": True,
                })
                results.append({"remote_id": remote_id, "ok": True, "state": updated["state"], "reason": updated["reason"]})
            return {
                "ok": True,
                "items": results,
                "refreshed_count": sum(1 for item in results if item["ok"]),
                "skipped": skipped,
            }

    def replace(self, values: object, *, opt_in: bool = False, limit: int = _MAX_ITEMS) -> dict:
        selected_ids = _selected_ids(values, limit=limit)
        with self._operation_lock:
            self._reload_audit_locked()
            with self._lock:
                candidates = [
                    self._sanitize_entry(self._tracked[remote_id])
                    for remote_id in selected_ids
                    if remote_id in self._tracked
                    and self._tracked[remote_id].get("state") == "ready"
                    and self._tracked[remote_id].get("replaceable")
                ]
            preview = [self._public_item(item) for item in candidates]
            if not opt_in:
                return {
                    "ok": True,
                    "replacement_performed": False,
                    "requires_confirmation": True,
                    "items": preview,
                }

            results: list[dict] = []
            attempted_count = 0
            for item in candidates:
                remote_id = item["remote_id"]
                email = item["email"]
                local_id = item["local_account_id"]
                remote_rows = list(self._list_remote_fn() or [])
                local_rows = list(self._list_local_fn() or [])
                by_remote_id, by_remote_email = self._remote_maps(remote_rows)
                by_local_id, by_local_email = self._local_maps(local_rows)
                local = by_local_id.get(local_id)
                failure = ""
                if len(by_remote_id.get(remote_id, [])) != 1 or len(by_remote_email.get(email, [])) != 1:
                    failure = "PATCH 前远端 ID 或邮箱映射已变化"
                elif local is None or _email(local.get("email")) != email or len(by_local_email.get(email, [])) != 1:
                    failure = "PATCH 前本地账号映射已变化"
                else:
                    try:
                        before = self._normalize_snapshot(self._snapshot_fn(remote_id))
                    except Exception as exc:
                        failure = _reason(exc, "PATCH 前远端快照读取失败")
                    else:
                        if not self._snapshot_complete(before, remote_id):
                            failure = "PATCH 前远端快照不完整或账号 ID 不一致"
                        elif before["at_sha256"] != item["old_at_sha256"]:
                            failure = "PATCH 前远端旧 AT 摘要已变化"
                        elif before["preserved_sha256"] != item["preserved_sha256"]:
                            failure = "PATCH 前远端保留字段已变化"
                new_token = str((local or {}).get("access_token") or "").strip()
                new_token_sha = _hash_token(new_token)
                if not failure and (
                    not new_token_sha
                    or new_token_sha != item["new_at_sha256"]
                    or new_token_sha == item["old_at_sha256"]
                ):
                    failure = "PATCH 前本地新 AT 摘要已变化、缺失或与锁定旧 AT 相同"
                if failure:
                    updated = self._set_item(remote_id, {
                        "state": "conflict",
                        "reason": failure,
                        "retryable": False,
                        "replaceable": False,
                    })
                    results.append({"remote_id": remote_id, "ok": False, "state": updated["state"], "reason": updated["reason"]})
                    continue

                try:
                    attempted_count += 1
                    self._patch_fn(remote_id, new_token)
                    after_rows = list(self._list_remote_fn() or [])
                    after_by_id, after_by_email = self._remote_maps(after_rows)
                    if len(after_by_id.get(remote_id, [])) != 1:
                        raise RuntimeError("PATCH 后锁定的远端 ID 不再存在或不唯一")
                    if len(after_by_email.get(email, [])) != 1 or after_by_email[email][0].get("remote_id") != remote_id:
                        raise RuntimeError("PATCH 后远端邮箱映射发生变化")
                    after = self._normalize_snapshot(self._snapshot_fn(remote_id))
                    if not self._snapshot_complete(after, remote_id):
                        raise RuntimeError("PATCH 后远端快照不完整或账号 ID 不一致")
                    if after["at_sha256"] != item["new_at_sha256"] or after["at_sha256"] == item["old_at_sha256"]:
                        raise RuntimeError("PATCH 后远端 AT 摘要未按预期变化")
                    if after["preserved_sha256"] != item["preserved_sha256"]:
                        raise RuntimeError("PATCH 后远端保留字段发生变化")
                except Exception as exc:
                    updated = self._set_item(remote_id, {
                        "state": "conflict",
                        "reason": _reason(exc, "PATCH 后验证失败"),
                        "retryable": False,
                        "replaceable": False,
                    })
                    results.append({"remote_id": remote_id, "ok": False, "state": updated["state"], "reason": updated["reason"]})
                    continue

                updated = self._set_item(remote_id, {
                    "state": "replaced",
                    "reason": "远端旧 AT 摘要已变化，锁定 ID 保持存在且保留字段未变",
                    "replaced_at": _now(),
                    "retryable": False,
                    "replaceable": False,
                })
                results.append({"remote_id": remote_id, "ok": True, "state": updated["state"], "reason": updated["reason"]})
            return {
                "ok": True,
                "replacement_performed": attempted_count > 0,
                "items": results,
                "replaced_count": sum(1 for item in results if item["ok"]),
            }


__all__ = ["ChatGPT2API401Service"]
