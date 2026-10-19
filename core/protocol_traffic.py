# -*- coding: utf-8 -*-
"""Opt-in, per-registration curl_cffi traffic accounting.

This module records application-visible transfer metrics only. It deliberately does
not retain request/response objects, headers, cookies, bodies, or exception text.
"""
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from config import browser as _browser_cfg


logger = logging.getLogger(__name__)
_PROTOCOL_METHOD = "curl_cffi.protocol"
_PROTOCOL_LABEL = "Protocol"
_PROTOCOL_NOTE = (
    "Application-visible curl_cffi metrics; upload_bytes uses request_size only, "
    "and download_bytes requires response_size or encoded body plus header bytes. "
    "Missing native metrics remain unknown. "
    "TLS, proxy tunnel, DNS, retransmission, and email-provider traffic are excluded."
)
_DETAIL_MAX_DEFAULT = 2000
_DETAIL_MAX_HARD_LIMIT = 10000
_ALLOWED_SCHEMES = {"http", "https"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9._~-]")
_SECRET_WORDS = {"authorization", "bearer", "cookie", "password", "secret", "token", "otp"}
_SAFE_ROUTE_WORDS = {"code", "session", "state", "continue", "authorize"}
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def _normalise_detail_limit(value: object) -> int:
    try:
        value = int(value or _DETAIL_MAX_DEFAULT)
    except (TypeError, ValueError):
        value = _DETAIL_MAX_DEFAULT
    return max(1, min(value, _DETAIL_MAX_HARD_LIMIT))


def _looks_secret_path_segment(value: str) -> bool:
    lower = value.lower()
    if lower in _SAFE_ROUTE_WORDS:
        return False
    # Checkout identifiers can contain only letters; entropy/digit heuristics
    # alone do not reliably hide provider session paths.
    if lower.startswith(("cs_", "oaics_")):
        return True
    if any(word in lower for word in _SECRET_WORDS):
        return True
    if "@" in value or "=" in value or "%" in value:
        return True
    if _UUID_RE.fullmatch(value):
        return True
    if value.isdigit() and len(value) >= 4:
        return True
    if "." in value and value.count(".") >= 2 and len(value) >= 20:
        return True
    if len(value) >= 8 and re.search(r"[A-Za-z]", value) and re.search(r"\d", value):
        return True
    return False


def _safe_hostname(value: str) -> str | None:
    if not value or _CONTROL_RE.search(value):
        return None
    if ":" in value:
        return f"[{value}]"
    labels = value.split(".")
    if any(not label for label in labels):
        return None
    safe_labels = []
    for label in labels:
        if _looks_secret_path_segment(label):
            safe_labels.append("<redacted>")
        else:
            safe_labels.append(re.sub(r"[^a-z0-9-]", "-", label)[:63] or "-")
    return ".".join(safe_labels)


def safe_protocol_url(value: object, *, max_length: int = 320) -> str:
    """Return only a bounded scheme/host/path, never query, fragment, or userinfo."""
    try:
        parsed = urlsplit(str(value or ""))
        scheme = (parsed.scheme or "").lower()
        if scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            return "<redacted-url>"
        hostname = parsed.hostname.lower().rstrip(".")
        authority = _safe_hostname(hostname)
        if authority is None:
            return "<redacted-url>"
        port = parsed.port
        if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            authority = f"{authority}:{port}"
        raw_path = _CONTROL_RE.sub("", parsed.path or "/")
        parts = []
        for segment in raw_path.split("/"):
            if not segment:
                parts.append("")
                continue
            if _looks_secret_path_segment(segment):
                parts.append("<redacted>")
            else:
                parts.append(_SAFE_PATH_RE.sub("-", segment)[:80] or "-")
        path = "/".join(parts) or "/"
        result = f"{scheme}://{authority}{path}"
        return result[:max_length]
    except Exception:
        return "<redacted-url>"


def _is_http_url(value: object) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        return parsed.scheme.lower() in _ALLOWED_SCHEMES and bool(parsed.hostname)
    except Exception:
        return False


def _numeric_metric(response: object, name: str) -> tuple[object, bool]:
    """Read one already-captured response metric without materialising response data."""
    if response is None:
        return None, False
    try:
        value = getattr(response, name)
    except Exception:
        value = None
    present = value is not None
    if not present:
        try:
            infos = getattr(response, "infos", None)
            if isinstance(infos, dict) and name in infos:
                value = infos.get(name)
                present = value is not None
        except Exception:
            present = False
    return value, present


def _non_negative_int(value: object) -> tuple[int | None, bool]:
    if value is None or isinstance(value, bool):
        return None, False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    if not math.isfinite(number) or number < 0:
        return None, False
    return int(number), True


def _elapsed_seconds(value: object) -> tuple[float | None, bool]:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    if value is None or isinstance(value, bool):
        return None, False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    if not math.isfinite(number) or number < 0:
        return None, False
    return number, True


def _http_status(response: object) -> tuple[int | None, bool]:
    if response is None:
        return None, False
    try:
        value = getattr(response, "status_code")
    except Exception:
        return None, False
    if value is None or isinstance(value, bool):
        return None, False
    try:
        status = int(value)
    except (TypeError, ValueError, OverflowError):
        return None, False
    if status < 100 or status > 599:
        return None, False
    return status, True


class ProtocolTrafficTracker:
    """Bounded aggregate/detail collector owned by one protocol registration."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started = time.monotonic()
        self._frozen = False
        self._reported = False
        self._frozen_snapshot: dict | None = None
        self._started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._detail_enabled = bool(getattr(_browser_cfg, "BROWSER_TRAFFIC_DETAIL_LOG", False))
        self._detail_limit = _normalise_detail_limit(
            getattr(_browser_cfg, "BROWSER_TRAFFIC_DETAIL_MAX_ENTRIES", _DETAIL_MAX_DEFAULT)
        )
        self._details: list[dict] = []
        self._summary = {
            "request_count": 0,
            "completed_request_count": 0,
            "failed_request_count": 0,
            "http_error_count": 0,
            "transport_error_count": 0,
            "upload_bytes": 0,
            "download_bytes": 0,
            "request_body_bytes": 0,
            "response_body_bytes": 0,
            "response_header_bytes": 0,
            "request_elapsed_seconds": 0.0,
            "redirect_count": 0,
            "unknown_size_request_count": 0,
            "unknown_request_body_size_count": 0,
            "unknown_response_body_size_count": 0,
            "unknown_response_header_size_count": 0,
            "unknown_elapsed_request_count": 0,
            "unknown_status_request_count": 0,
            "incomplete_size_request_count": 0,
            "detail_recorded_count": 0,
            "detail_omitted_count": 0,
        }

    def observe(
        self,
        *,
        method: str,
        url: object,
        response: object = None,
        error_kind: str | None = None,
        elapsed_seconds: object = None,
        streaming: bool = False,
    ) -> None:
        """Record one actual HTTP transport invocation; all extraction is failure-isolated."""
        if not _is_http_url(url):
            return
        try:
            native_elapsed, elapsed_known = _elapsed_seconds(elapsed_seconds)
            response_elapsed, response_elapsed_known = _numeric_metric(response, "elapsed")
            response_elapsed, response_elapsed_known = _elapsed_seconds(response_elapsed)
            if response_elapsed_known:
                native_elapsed, elapsed_known = response_elapsed, True

            request_raw, _ = _numeric_metric(response, "request_size")
            upload_raw, _ = _numeric_metric(response, "upload_size")
            body_raw, _ = _numeric_metric(response, "download_size")
            header_raw, _ = _numeric_metric(response, "header_size")
            response_raw, _ = _numeric_metric(response, "response_size")
            request_size, request_known = _non_negative_int(request_raw)
            upload_size, upload_known = _non_negative_int(upload_raw)
            body_size, body_known = _non_negative_int(body_raw)
            header_size, header_known = _non_negative_int(header_raw)
            response_size, response_known = _non_negative_int(response_raw)
            status, status_known = _http_status(response)
            try:
                redirect_count, redirect_known = _non_negative_int(getattr(response, "redirect_count", 0))
            except Exception:
                redirect_count, redirect_known = 0, True
            redirect_count = redirect_count if redirect_known else 0

            transport_failed = bool(error_kind) or response is None
            http_failed = bool(not transport_failed and status_known and status >= 400)
            failed = transport_failed or http_failed
            if transport_failed:
                outcome = "transport_error"
            elif http_failed:
                outcome = "http_error"
            elif status_known:
                outcome = "success"
            else:
                outcome = "unknown_status"
            # curl_cffi 0.16 reports request_size including the request body. upload_size
            # remains body-only evidence and cannot substitute for the complete total.
            logical_upload = request_size if request_known else None
            upload_size_source = "request_size" if request_known else "unknown"
            if body_known and header_known:
                logical_download = body_size + header_size
                download_size_source = "download_size+header_size"
            elif response_known:
                logical_download = response_size
                download_size_source = "response_size"
            else:
                logical_download = None
                download_size_source = "unknown"
            incomplete = bool(streaming or redirect_count)
            size_complete = bool(logical_upload is not None and logical_download is not None and not incomplete)

            with self._lock:
                if self._frozen:
                    return
                self._summary["request_count"] += 1
                self._summary["completed_request_count" if not failed else "failed_request_count"] += 1
                if http_failed:
                    self._summary["http_error_count"] += 1
                if transport_failed:
                    self._summary["transport_error_count"] += 1
                if logical_upload is not None:
                    self._summary["upload_bytes"] += logical_upload
                if logical_download is not None:
                    self._summary["download_bytes"] += logical_download
                if upload_known:
                    self._summary["request_body_bytes"] += upload_size
                if body_known:
                    self._summary["response_body_bytes"] += body_size
                if header_known:
                    self._summary["response_header_bytes"] += header_size
                if elapsed_known and native_elapsed is not None:
                    self._summary["request_elapsed_seconds"] += native_elapsed
                self._summary["redirect_count"] += redirect_count
                if logical_upload is None or logical_download is None:
                    self._summary["unknown_size_request_count"] += 1
                if not upload_known:
                    self._summary["unknown_request_body_size_count"] += 1
                if not body_known:
                    self._summary["unknown_response_body_size_count"] += 1
                if not header_known:
                    self._summary["unknown_response_header_size_count"] += 1
                if not elapsed_known:
                    self._summary["unknown_elapsed_request_count"] += 1
                if not status_known:
                    self._summary["unknown_status_request_count"] += 1
                if incomplete:
                    self._summary["incomplete_size_request_count"] += 1

                if self._detail_enabled:
                    if len(self._details) < self._detail_limit:
                        row = {
                            "method": str(method or "GET").upper()[:12],
                            "url": safe_protocol_url(url),
                            "status": status,
                            "outcome": outcome,
                            "upload_bytes": logical_upload,
                            "download_bytes": logical_download,
                            "request_body_bytes": upload_size if upload_known else None,
                            "response_body_bytes": body_size if body_known else None,
                            "response_header_bytes": header_size if header_known else None,
                            "elapsed_seconds": round(native_elapsed, 6) if elapsed_known else None,
                            "redirect_count": redirect_count,
                            "size_complete": size_complete,
                            "upload_size_source": upload_size_source,
                            "download_size_source": download_size_source,
                        }
                        self._details.append(row)
                        self._summary["detail_recorded_count"] += 1
                    else:
                        self._summary["detail_omitted_count"] += 1
        except Exception:
            # Telemetry must never replace a response or transport exception.
            logger.debug("Protocol traffic accounting failed", exc_info=True)

    def _snapshot_locked(self) -> dict:
        result = {
            "available": True,
            "method": _PROTOCOL_METHOD,
            "label": _PROTOCOL_LABEL,
            "started_at": self._started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "duration_seconds": round(max(0.0, time.monotonic() - self._started), 3),
            "detail_log_enabled": self._detail_enabled,
            "detail_limit": self._detail_limit,
            "size_source": "curl_response_metrics",
            "note": _PROTOCOL_NOTE,
        }
        result.update(self._summary)
        result["request_elapsed_seconds"] = round(float(result["request_elapsed_seconds"]), 6)
        result["total_bytes"] = int(result["upload_bytes"] + result["download_bytes"])
        return result

    def snapshot(self) -> dict:
        with self._lock:
            if self._frozen_snapshot is not None:
                return dict(self._frozen_snapshot)
            return dict(self._snapshot_locked())

    def freeze(self) -> dict:
        with self._lock:
            if self._frozen_snapshot is not None:
                return dict(self._frozen_snapshot)
            self._frozen = True
            self._frozen_snapshot = dict(self._snapshot_locked())
            return dict(self._frozen_snapshot)

    def report(self, target_logger: logging.Logger | None = None) -> dict:
        """Emit bounded rows followed by one aggregate through the caller logger."""
        target_logger = target_logger or logger
        try:
            with self._lock:
                if self._reported:
                    return dict(self._frozen_snapshot or self._snapshot_locked())
                self._reported = True
                snapshot = dict(self._frozen_snapshot or self._snapshot_locked())
                details = [dict(item) for item in self._details]
            if self._detail_enabled:
                for row in details:
                    try:
                        target_logger.info("[协议流量明细] %s", json.dumps(row, ensure_ascii=True, sort_keys=True))
                    except Exception:
                        break
            try:
                target_logger.info("[协议流量汇总] %s", json.dumps(snapshot, ensure_ascii=True, sort_keys=True))
            except Exception:
                pass
            return snapshot
        except Exception:
            try:
                return self.snapshot()
            except Exception:
                return {"available": False, "method": _PROTOCOL_METHOD, "label": _PROTOCOL_LABEL}


__all__ = ["ProtocolTrafficTracker", "safe_protocol_url"]
