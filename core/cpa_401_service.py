# -*- coding: utf-8 -*-
"""Verification-first, non-destructive CPA 401 credential rotation state."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_MAX_REASON = 180
_MAX_FILE_NAME = 512
_STATES = {
    "pending",
    "retrying",
    "verified",
    "cleanup_ready",
    "cleaned",
    "failed",
    "ambiguous",
}
_RUNNING_LOCAL_STATES = {"retrying", "queued", "running"}
_FAILED_LOCAL_STATES = {"failed", "stopped", "cancelled", "canceled", "error"}
_UNAUTHORIZED_RE = re.compile(r"(?:\b401\b|unauthori[sz]ed|invalid[_ -]?grant|invalid[_ -]?token)", re.I)
_SECRET_RE = re.compile(
    r"(?i)(access[_ -]?token|refresh[_ -]?token|authorization|bearer|api[_ -]?key|password|secret|callback|code)\s*[:=]\s*\S+"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_reason(value: object, fallback: str) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    text = _SECRET_RE.sub(r"\1=[redacted]", text)
    if not text:
        text = fallback
    return text[:_MAX_REASON]


def _email(value: object) -> str:
    return str(value or "").strip().lower()[:320]


def _bounded_int(value: object, *, minimum: int = 0, maximum: int = 10_000) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _filename(value: object) -> str:
    return str(value or "").replace("\x00", "").strip()[:_MAX_FILE_NAME]


def _dedupe_files(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        name = _filename(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return sorted(result)[:50]


def _status_kind(item: dict, refresh_error: str = "", refresh_outcome: str = "") -> str:
    if refresh_outcome in {"healthy", "unauthorized", "timeout", "unknown"}:
        return refresh_outcome
    status = str(item.get("status") or "").strip().lower()
    message = " ".join((str(item.get("status_message") or ""), str(refresh_error or ""))).strip()
    if _UNAUTHORIZED_RE.search(message):
        return "unauthorized"
    if "timeout" in message.lower() or "timed out" in message.lower():
        return "timeout"
    if item.get("disabled") is True:
        return "unknown"
    if status in {"active", "ready", "ok", "success", "healthy", "valid"} and item.get("unavailable") is not True:
        return "healthy"
    if status in {"error", "failed", "unavailable", "disabled", "unknown"} or item.get("unavailable") is True:
        return "unknown"
    return "healthy" if not message and status not in {"", "unknown"} else "unknown"


class CpaRefreshUnsupported(RuntimeError):
    pass


class CpaSnapshotConflict(RuntimeError):
    """The live CPA filenames no longer match the persisted retry selection."""

    def __init__(self, emails: list[str]):
        self.emails = sorted({_email(email) for email in emails if _email(email)})[:50]
        super().__init__("CPA 文件快照已变化，请重新检测后再补跑")


class Cpa401Service:
    """Persists non-sensitive CPA rotation evidence and gates remote cleanup."""

    def __init__(
        self,
        *,
        refresh_fn: Callable[[], dict] | None = None,
        list_fn: Callable[[], list[dict]] | None = None,
        download_fn: Callable[..., object] | None = None,
        cleanup_adapter: Callable[[str], object] | None = None,
        audit_path: str | Path | None = None,
    ) -> None:
        if refresh_fn is None or list_fn is None or download_fn is None:
            from core.codex_oauth import (
                download_cpa_codex_auth_text,
                list_cpa_codex_auth_files,
                refresh_all_cpa_credentials,
            )
        self._refresh_fn = refresh_fn or refresh_all_cpa_credentials
        self._list_fn = list_fn or list_cpa_codex_auth_files
        self._download_fn = download_fn or download_cpa_codex_auth_text
        self._cleanup_adapter = cleanup_adapter
        self._audit_path = Path(audit_path) if audit_path is not None else Path(__file__).resolve().parent.parent / "cpa_401_audit.json"
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._tracked: dict[str, dict] = self._load_audit()

    @property
    def cleanup_supported(self) -> bool:
        return callable(self._cleanup_adapter)

    @staticmethod
    def _refresh_maps(payload: dict) -> tuple[dict[str, str], dict[str, str]]:
        results = payload.get("results") if isinstance(payload, dict) else None
        outcomes: dict[str, str] = {}
        reasons: dict[str, str] = {}
        if not isinstance(results, list):
            return outcomes, reasons
        for row in results:
            if not isinstance(row, dict):
                continue
            success = row.get("success")
            raw_error = str(row.get("error") or "")
            if success is True:
                outcome, reason = "healthy", "CPA 凭证刷新成功"
            elif success is False and _UNAUTHORIZED_RE.search(raw_error):
                outcome, reason = "unauthorized", "CPA 返回 unauthorized/401"
            elif success is False and ("timeout" in raw_error.lower() or "timed out" in raw_error.lower()):
                outcome, reason = "timeout", "CPA 凭证刷新超时"
            elif success is False:
                outcome, reason = "unknown", "CPA 凭证刷新失败"
            else:
                continue
            for key in ("id", "name", "auth_index", "index", "email"):
                ident = str(row.get(key) or "").strip().lower()
                if ident:
                    outcomes[ident] = outcome
                    reasons[ident] = reason
        return outcomes, reasons

    @staticmethod
    def _row_identity_values(row: dict) -> list[str]:
        return [
            str(row.get(key) or "").strip().lower()
            for key in ("id", "name", "auth_index", "index", "email")
            if str(row.get(key) or "").strip()
        ]

    def _row_kind(self, row: dict, outcomes: dict[str, str], reasons: dict[str, str]) -> tuple[str, str]:
        outcome = ""
        reason = ""
        for ident in self._row_identity_values(row):
            if ident in outcomes:
                outcome = outcomes[ident]
                reason = reasons.get(ident, "")
                break
        return _status_kind(row, reason, outcome), reason

    def _load_audit(self) -> dict[str, dict]:
        try:
            payload = json.loads(self._audit_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        raw_items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(raw_items, dict):
            return {}
        tracked: dict[str, dict] = {}
        for raw_email, raw_item in raw_items.items():
            email = _email(raw_email)
            if not email or not isinstance(raw_item, dict):
                continue
            state = str(raw_item.get("state") or "").strip().lower()
            if state not in _STATES:
                continue
            tracked[email] = self._sanitize_entry({**raw_item, "email": email})
        return tracked

    def _persist_locked(self) -> None:
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _now(),
            "items": {email: self._sanitize_entry(item) for email, item in sorted(self._tracked.items())},
        }
        temp_path = self._audit_path.with_name(f".{self._audit_path.name}.{os.getpid()}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, self._audit_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _sanitize_entry(self, raw: dict) -> dict:
        state = str(raw.get("state") or "ambiguous").strip().lower()
        if state not in _STATES:
            state = "ambiguous"
        entry = {
            "email": _email(raw.get("email")),
            "state": state,
            "reason": _bounded_reason(raw.get("reason"), state),
            "files": _dedupe_files(raw.get("files")),
            "pre_retry_files": _dedupe_files(raw.get("pre_retry_files")),
            "post_retry_files": _dedupe_files(raw.get("post_retry_files")),
            "cleanup_files": _dedupe_files(raw.get("cleanup_files")),
            "replacement_file": _filename(raw.get("replacement_file")),
            "candidate_count": _bounded_int(raw.get("candidate_count")),
            "checked_at": str(raw.get("checked_at") or "")[:64],
            "pre_retry_snapshot_at": str(raw.get("pre_retry_snapshot_at") or "")[:64],
            "post_retry_snapshot_at": str(raw.get("post_retry_snapshot_at") or "")[:64],
            "retry_started_at": str(raw.get("retry_started_at") or "")[:64],
            "retry_batch_id": str(raw.get("retry_batch_id") or "")[:64],
            "local_codex_status": str(raw.get("local_codex_status") or "")[:64],
            "retryable": bool(raw.get("retryable")),
            "cleanup_supported": self.cleanup_supported,
            "manual_action": "在 CPA 管理界面按精确文件名人工确认后删除旧文件" if state == "cleanup_ready" and not self.cleanup_supported else "",
        }
        if state not in {"pending", "failed"}:
            entry["retryable"] = False
        return entry

    def _public_items_locked(self) -> list[dict]:
        return [self._sanitize_entry(item) for _, item in sorted(self._tracked.items())]

    @staticmethod
    def _counts(items: list[dict], *, health_counts: dict[str, int] | None = None) -> dict[str, int]:
        counts = {state: 0 for state in sorted(_STATES)}
        for item in items:
            state = str(item.get("state") or "")
            if state in counts:
                counts[state] += 1
        counts["verified"] = counts.get("verified", 0) + counts.get("cleanup_ready", 0) + counts.get("cleaned", 0)
        for key in ("unauthorized", "healthy", "unknown"):
            counts[key] = int((health_counts or {}).get(key, 0))
        return counts

    def state(self) -> dict:
        with self._lock:
            items = self._public_items_locked()
        return {
            "ok": True,
            "supported": True,
            "cleanup_supported": self.cleanup_supported,
            "items": items,
            "counts": self._counts(items),
            "checked_at": max((item.get("checked_at") or "" for item in items), default=""),
        }

    def _verify_download(self, filename: str, *, expected_email: str = "") -> tuple[bool, str]:
        try:
            result = self._download_fn(cpa_name=filename)
        except Exception:
            return False, "CPA 新凭证下载校验失败"
        if not isinstance(result, tuple) or len(result) < 2:
            return False, "CPA 新凭证下载校验响应无效"
        returned_name = _filename(result[1])
        if returned_name != filename:
            return False, "CPA 下载校验返回了不同文件名"
        meta = result[2] if len(result) >= 3 and isinstance(result[2], dict) else {}
        meta_name = _filename(meta.get("name"))
        if meta_name and meta_name != filename:
            return False, "CPA 下载校验元数据文件名不一致"
        expected = _email(expected_email)
        meta_email = _email(meta.get("email"))
        if expected and meta_email and meta_email != expected:
            return False, "CPA 下载校验元数据账号不一致"
        content = result[0]
        try:
            if isinstance(content, dict):
                payload = content
            elif isinstance(content, bytes):
                payload = json.loads(content.decode("utf-8"))
            elif isinstance(content, str):
                payload = json.loads(content)
            else:
                return False, "CPA 新凭证下载校验响应无效"
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return False, "CPA 新凭证下载内容不是有效 JSON"
        if not isinstance(payload, dict) or not payload:
            return False, "CPA 新凭证下载内容不是有效凭证对象"
        credential_markers = ("access_token", "refresh_token", "id_token", "account_id")
        if str(payload.get("type") or "").strip().lower() != "codex" and not any(payload.get(key) for key in credential_markers):
            return False, "CPA 新凭证下载内容缺少 Codex 凭证标识"
        payload_email = _email(payload.get("email"))
        if expected and payload_email and payload_email != expected:
            return False, "CPA 下载校验凭证账号不一致"
        return True, ""

    def _evaluate_after_retry(
        self,
        old: dict,
        rows: list[dict],
        row_kinds: dict[str, str],
        *,
        local_status: str,
        checked_at: str,
    ) -> dict:
        current_files = _dedupe_files([row.get("name") for row in rows])
        pre_files = _dedupe_files(old.get("pre_retry_files") or old.get("files"))
        rows_by_name: dict[str, list[dict]] = {}
        unnamed_rows = 0
        for row in rows:
            name = _filename(row.get("name"))
            if not name:
                unnamed_rows += 1
                continue
            rows_by_name.setdefault(name, []).append(row)
        duplicate_names = sorted(name for name, matches in rows_by_name.items() if len(matches) != 1)
        new_files = [name for name in current_files if name not in set(pre_files)]
        base = {
            **old,
            "files": current_files or _dedupe_files(old.get("files")),
            "pre_retry_files": pre_files,
            "post_retry_files": current_files,
            "candidate_count": len(rows),
            "checked_at": checked_at,
            "post_retry_snapshot_at": checked_at,
            "local_codex_status": local_status,
            "retryable": False,
            "cleanup_files": [],
            "replacement_file": "",
        }

        if unnamed_rows or duplicate_names:
            return self._sanitize_entry({
                **base,
                "state": "ambiguous",
                "reason": "补跑后 CPA 文件身份不唯一，旧文件保持不变",
            })

        replacement = _filename(old.get("replacement_file"))
        if replacement and replacement in rows_by_name and row_kinds.get(replacement) == "healthy":
            ok, reason = self._verify_download(replacement, expected_email=old.get("email") or "")
            if ok:
                cleanup_files = [name for name in pre_files if name in rows_by_name and name != replacement]
                state = "cleaned" if old.get("state") == "cleaned" else "cleanup_ready" if cleanup_files else "verified"
                return self._sanitize_entry({
                    **base,
                    "state": state,
                    "reason": "新 CPA 凭证已验证；旧文件等待人工清理" if cleanup_files else "CPA 凭证已验证可用，无待清理旧文件",
                    "replacement_file": replacement,
                    "cleanup_files": cleanup_files,
                })
            return self._sanitize_entry({**base, "state": "failed", "reason": reason})

        if len(new_files) > 1:
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "补跑后出现多个新 CPA 文件，无法唯一确认替代凭证"})
        if len(new_files) == 1:
            candidate = new_files[0]
            if row_kinds.get(candidate) != "healthy":
                return self._sanitize_entry({**base, "state": "failed", "reason": "唯一新 CPA 文件未通过状态验证"})
            ok, reason = self._verify_download(candidate, expected_email=old.get("email") or "")
            if not ok:
                return self._sanitize_entry({**base, "state": "failed", "reason": reason})
            cleanup_files = [name for name in pre_files if name in rows_by_name and name != candidate]
            return self._sanitize_entry({
                **base,
                "state": "cleanup_ready" if cleanup_files else "verified",
                "reason": "已唯一验证新 CPA 凭证；旧文件仅进入人工清理清单" if cleanup_files else "已唯一验证新 CPA 凭证；旧文件已不存在",
                "replacement_file": candidate,
                "cleanup_files": cleanup_files,
            })

        if len(pre_files) == 1 and current_files == pre_files and row_kinds.get(pre_files[0]) == "healthy":
            ok, reason = self._verify_download(pre_files[0], expected_email=old.get("email") or "")
            if ok:
                return self._sanitize_entry({
                    **base,
                    "state": "verified",
                    "reason": "CPA 在原文件名上完成更新并通过下载校验",
                    "replacement_file": pre_files[0],
                })
            return self._sanitize_entry({**base, "state": "failed", "reason": reason})

        if not rows:
            return self._sanitize_entry({**base, "state": "ambiguous", "reason": "补跑后未找到该邮箱的 CPA 凭证"})
        if local_status in _RUNNING_LOCAL_STATES:
            return self._sanitize_entry({**base, "state": "retrying", "reason": "Codex 补跑进行中；尚未出现唯一可验证替代凭证"})
        if local_status in _FAILED_LOCAL_STATES:
            return self._sanitize_entry({**base, "state": "failed", "reason": "Codex 补跑失败；旧 CPA 文件保持不变", "retryable": True})
        kinds = {row_kinds.get(name, "unknown") for name in current_files}
        if "unauthorized" in kinds:
            return self._sanitize_entry({**base, "state": "failed", "reason": "补跑后 CPA 仍返回 unauthorized/401", "retryable": True})
        return self._sanitize_entry({**base, "state": "ambiguous", "reason": "补跑后没有唯一且可验证的替代凭证"})

    def _persist_post_snapshot_error(self, files: list[dict], reason: str) -> None:
        grouped: dict[str, list[dict]] = {}
        for row in files:
            if not isinstance(row, dict):
                continue
            email = _email(row.get("email"))
            if email:
                grouped.setdefault(email, []).append(row)
        checked_at = _now()
        changed = False
        with self._lock:
            for email, item in list(self._tracked.items()):
                if item.get("state") != "retrying" or not item.get("pre_retry_files"):
                    continue
                rows = grouped.get(email, [])
                current_files = _dedupe_files([row.get("name") for row in rows])
                self._tracked[email] = self._sanitize_entry({
                    **item,
                    "state": "ambiguous",
                    "reason": _bounded_reason(reason, "CPA 后置验证失败"),
                    "files": current_files or _dedupe_files(item.get("files")),
                    "post_retry_files": current_files,
                    "post_retry_snapshot_at": checked_at,
                    "candidate_count": len(rows),
                    "cleanup_files": [],
                    "replacement_file": "",
                    "retryable": False,
                })
                changed = True
            if changed:
                self._persist_locked()

    def detect(self, local_status: dict[str, str] | None = None) -> dict:
        local_status = {_email(k): str(v or "").strip().lower() for k, v in (local_status or {}).items()}
        with self._operation_lock:
            try:
                refresh_payload = self._refresh_fn()
            except Exception as exc:
                try:
                    snapshot_files = self._list_fn()
                except Exception:
                    snapshot_files = None
                if snapshot_files is not None:
                    self._persist_post_snapshot_error(snapshot_files, "CPA 后置状态刷新失败；已保存补跑后文件快照，旧文件保持不变")
                text = str(exc or "")
                if "status=404" in text or "not_found" in text.lower():
                    raise CpaRefreshUnsupported("当前 CPA 不支持凭证刷新接口，请检查 CPA 版本或接口配置") from exc
                raise
            files = self._list_fn()

        outcomes, refresh_reasons = self._refresh_maps(refresh_payload)
        grouped: dict[str, list[dict]] = {}
        unknown_without_email = 0
        for row in files:
            if not isinstance(row, dict):
                continue
            email = _email(row.get("email"))
            if not email:
                unknown_without_email += 1
                continue
            grouped.setdefault(email, []).append(row)

        checked_at = _now()
        health_counts = {"unauthorized": 0, "healthy": 0, "unknown": unknown_without_email}
        with self._lock:
            previous = {email: dict(item) for email, item in self._tracked.items()}
            next_tracked: dict[str, dict] = {}
            all_emails = sorted(set(grouped) | set(previous))
            for email in all_emails:
                rows = grouped.get(email, [])
                old = previous.get(email) or {}
                local = local_status.get(email, "")
                row_kinds: dict[str, str] = {}
                reasons: list[str] = []
                for row in rows:
                    name = _filename(row.get("name"))
                    kind, reason = self._row_kind(row, outcomes, refresh_reasons)
                    if name:
                        row_kinds[name] = kind
                    if reason:
                        reasons.append(reason)
                kinds = list(row_kinds.values())
                if kinds and all(kind == "unauthorized" for kind in kinds) and len(rows) == 1:
                    health_counts["unauthorized"] += 1
                elif kinds and all(kind == "healthy" for kind in kinds):
                    health_counts["healthy"] += 1
                else:
                    health_counts["unknown"] += 1

                if old.get("state") in {"retrying", "verified", "cleanup_ready", "cleaned", "failed", "ambiguous"} and old.get("pre_retry_files"):
                    next_tracked[email] = self._evaluate_after_retry(
                        old,
                        rows,
                        row_kinds,
                        local_status=local,
                        checked_at=checked_at,
                    )
                    continue

                filenames = _dedupe_files([row.get("name") for row in rows])
                if len(rows) == 1 and kinds == ["unauthorized"]:
                    state = "pending"
                    reason = reasons[0] if reasons else "CPA 返回 unauthorized/401"
                    retryable = True
                elif rows and "unauthorized" in kinds:
                    state = "ambiguous"
                    reason = "同一邮箱匹配到多个 CPA 凭证，无法唯一确定 401 文件"
                    retryable = False
                elif old:
                    state = "ambiguous"
                    reason = "CPA 检测结果不再包含可确认的 401 凭证"
                    retryable = False
                else:
                    continue
                next_tracked[email] = self._sanitize_entry({
                    "email": email,
                    "state": state,
                    "reason": reason,
                    "files": filenames,
                    "pre_retry_files": [],
                    "post_retry_files": [],
                    "cleanup_files": [],
                    "replacement_file": "",
                    "candidate_count": len(rows),
                    "checked_at": checked_at,
                    "retryable": retryable,
                    "local_codex_status": local,
                })

            self._tracked = next_tracked
            self._persist_locked()
            visible = self._public_items_locked()

        return {
            "ok": True,
            "supported": True,
            "cleanup_supported": self.cleanup_supported,
            "items": visible,
            "counts": self._counts(visible, health_counts=health_counts),
            "checked_at": checked_at,
        }

    def select_retryable(self, values: list[object], *, limit: int = 50) -> tuple[list[dict], list[dict]]:
        selected: list[dict] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        with self._lock:
            for raw in values[: limit + 1]:
                email = _email(raw)
                if not email or "@" not in email:
                    skipped.append({"email": str(raw or "")[:120], "reason": "邮箱非法"})
                    continue
                if email in seen:
                    continue
                seen.add(email)
                item = self._tracked.get(email)
                if not item:
                    skipped.append({"email": email, "reason": "不在最近一次 CPA 401 检测结果中"})
                elif not item.get("retryable"):
                    skipped.append({"email": email, "reason": "当前状态不可补跑"})
                else:
                    selected.append(self._sanitize_entry(item))
        if len(values) > limit:
            skipped.append({"email": "", "reason": f"单次最多选择 {limit} 个账号"})
        return selected, skipped

    def mark_retrying(self, emails: list[str], *, batch_id: str = "") -> None:
        now = _now()
        selected = list(dict.fromkeys(_email(raw) for raw in emails if _email(raw)))[:50]
        with self._operation_lock:
            live_files = self._list_fn()
        grouped: dict[str, list[dict]] = {}
        for row in live_files:
            if not isinstance(row, dict):
                continue
            email = _email(row.get("email"))
            if email:
                grouped.setdefault(email, []).append(row)
        conflicts: list[str] = []
        with self._lock:
            for email in selected:
                item = self._tracked.get(email)
                if not item or not item.get("retryable"):
                    conflicts.append(email)
                    continue
                rows = grouped.get(email, [])
                live_names = [_filename(row.get("name")) for row in rows]
                live_snapshot = _dedupe_files(live_names)
                tracked_snapshot = _dedupe_files(item.get("files"))
                if len(rows) != 1 or len(live_names) != 1 or not live_names[0] or live_snapshot != tracked_snapshot:
                    self._tracked[email] = self._sanitize_entry({
                        **item,
                        "state": "ambiguous",
                        "reason": "补跑前 CPA 文件快照已变化，请重新检测；旧文件保持不变",
                        "files": live_snapshot or tracked_snapshot,
                        "pre_retry_files": [],
                        "post_retry_files": [],
                        "cleanup_files": [],
                        "replacement_file": "",
                        "candidate_count": len(rows),
                        "checked_at": now,
                        "retryable": False,
                    })
                    conflicts.append(email)
            if conflicts:
                self._persist_locked()
                raise CpaSnapshotConflict(conflicts)
            for email in selected:
                item = self._tracked[email]
                snapshot = _dedupe_files([row.get("name") for row in grouped.get(email, [])])
                self._tracked[email] = self._sanitize_entry({
                    **item,
                    "state": "retrying",
                    "reason": "已启动 Codex OAuth 补跑，等待 CPA 后置验证",
                    "files": snapshot,
                    "pre_retry_files": snapshot,
                    "post_retry_files": [],
                    "cleanup_files": [],
                    "replacement_file": "",
                    "pre_retry_snapshot_at": now,
                    "post_retry_snapshot_at": "",
                    "retry_started_at": now,
                    "retry_batch_id": str(batch_id or "")[:64],
                    "retryable": False,
                })
            self._persist_locked()

    def mark_verification_error(self, emails: list[str], reason: str) -> None:
        with self._lock:
            for raw in emails:
                email = _email(raw)
                item = self._tracked.get(email)
                if not item or item.get("state") != "retrying":
                    continue
                self._tracked[email] = self._sanitize_entry({
                    **item,
                    "state": "ambiguous",
                    "reason": _bounded_reason(reason, "CPA 后置验证失败"),
                    "retryable": False,
                })
            self._persist_locked()

    def cleanup(self, values: list[object], *, opt_in: bool = False, limit: int = 50) -> dict:
        emails: list[str] = []
        seen: set[str] = set()
        for raw in values[: limit + 1]:
            email = _email(raw)
            if email and email not in seen:
                seen.add(email)
                emails.append(email)
        with self._lock:
            review = []
            for email in emails:
                item = self._tracked.get(email)
                if not item or item.get("state") != "cleanup_ready":
                    continue
                review.append({
                    "email": email,
                    "state": item.get("state"),
                    "replacement_file": item.get("replacement_file"),
                    "cleanup_files": _dedupe_files(item.get("cleanup_files")),
                    "manual_action": "在 CPA 管理界面按精确文件名人工确认后删除旧文件",
                })
        if not self.cleanup_supported or not opt_in:
            return {
                "ok": True,
                "cleanup_supported": self.cleanup_supported,
                "cleanup_performed": False,
                "items": review,
                "manual_action": "当前未配置已验证的 CPA 删除适配器；仅生成精确文件名审阅清单",
            }

        results: list[dict] = []
        with self._operation_lock:
            refresh_payload = self._refresh_fn()
            rows = self._list_fn()
        outcomes, refresh_reasons = self._refresh_maps(refresh_payload)
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            if isinstance(row, dict) and _email(row.get("email")):
                grouped.setdefault(_email(row.get("email")), []).append(row)

        with self._lock:
            for review_item in review:
                email = review_item["email"]
                item = self._tracked.get(email) or {}
                current_rows = grouped.get(email, [])
                rows_by_name: dict[str, list[dict]] = {}
                for row in current_rows:
                    name = _filename(row.get("name"))
                    if name:
                        rows_by_name.setdefault(name, []).append(row)
                replacement = _filename(item.get("replacement_file"))
                replacement_rows = rows_by_name.get(replacement, [])
                if len(replacement_rows) != 1:
                    reason = "替代凭证已不存在，未执行清理" if not replacement_rows else "替代凭证文件身份不唯一，未执行清理"
                    results.append({"email": email, "ok": False, "reason": reason, "files": []})
                    continue
                cleanup_files = _dedupe_files(item.get("cleanup_files"))
                if any(len(rows_by_name.get(filename, [])) > 1 for filename in cleanup_files):
                    results.append({"email": email, "ok": False, "reason": "旧 CPA 文件身份不唯一，未执行清理", "files": []})
                    continue
                kind, _ = self._row_kind(replacement_rows[0], outcomes, refresh_reasons)
                verified, verify_reason = self._verify_download(replacement, expected_email=email) if kind == "healthy" else (False, "替代凭证状态未验证")
                if not verified:
                    results.append({"email": email, "ok": False, "reason": verify_reason, "files": []})
                    continue
                file_results = []
                all_ok = True
                for filename in cleanup_files:
                    if filename not in rows_by_name:
                        file_results.append({"filename": filename, "ok": True, "result": "already_absent"})
                        continue
                    try:
                        raw_result = self._cleanup_adapter(filename)  # type: ignore[misc]
                        ok = bool(raw_result.get("ok", raw_result.get("success"))) if isinstance(raw_result, dict) else raw_result is True
                    except Exception:
                        ok = False
                    file_results.append({"filename": filename, "ok": ok, "result": "deleted" if ok else "failed"})
                    all_ok = all_ok and ok
                if all_ok:
                    self._tracked[email] = self._sanitize_entry({
                        **item,
                        "state": "cleaned",
                        "reason": "已通过已验证适配器清理旧 CPA 文件",
                        "cleanup_files": [],
                        "files": [replacement],
                        "post_retry_files": [replacement],
                    })
                else:
                    self._tracked[email] = self._sanitize_entry({**item, "state": "cleanup_ready", "reason": "部分旧 CPA 文件清理失败，可安全重试"})
                results.append({"email": email, "ok": all_ok, "reason": "" if all_ok else "部分文件清理失败", "files": file_results})
            self._persist_locked()

        return {
            "ok": True,
            "cleanup_supported": True,
            "cleanup_performed": True,
            "items": results,
        }
