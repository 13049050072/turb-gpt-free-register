# -*- coding: utf-8 -*-
"""Bounded redaction for errors crossing persistence or API boundaries."""
from __future__ import annotations

import re

MAX_REDACTED_ERROR_LENGTH = 500

_SECRET_NAME = (
    r"access[_ -]?token|refresh[_ -]?token|id[_ -]?token|api[_ -]?key|password|secret|"
    r"client[_ -]?secret|private[_ -]?key|proxy[_ -]?(?:authorization|user(?:name)?|password|credential)|"
    r"cookie|session(?:[_ -]?id)?|jwt|token|credential|callback(?:_url)?|oauth[_ -]?code|authorization"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?is)([\"']?(?:{_SECRET_NAME})[\"']?\s*[:=]\s*)([\"']).*?\2"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)([\"']?(?:{_SECRET_NAME})[\"']?\s*[:=]\s*)(?:(?:bearer|basic)\s+)?[^\s,;]+"
)
_PROXY_USERINFO = r"[A-Za-z0-9._~!$&()*+=%-]{1,128}:[^@\s,;\"'\\]{1,256}"
_PROXY_HOST = r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)"
_PROXY_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:upstream[_ -]?proxy|socks(?:4a?|5h?)[_ -]?proxy|proxy)(?:[_ -]?url)?\s*[:=]\s*)([\"']?)"
    rf"(?:(?:https?|socks(?:4a?|5h?)?)://)?{_PROXY_USERINFO}@{_PROXY_HOST}(?::\d{{1,5}})?\2"
)
_BARE_PROXY_CREDENTIAL_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9._~%!$&()*+=-]){_PROXY_USERINFO}@{_PROXY_HOST}:\d{{1,5}}(?!\d)"
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+")
_URL_RE = re.compile(r"\b(?:https?|socks(?:4a?|5h?)?)://[^\s,;]+", re.I)
_TOKENISH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{20,}(?:\.[A-Za-z0-9_+/=-]{8,})*")


def _redact_proxy_credentials(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}[redacted]{match.group(2)}"


def redact_error(value: object) -> str:
    """Normalize, redact, and cap an error without hiding ordinary status text."""
    text = " ".join(str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ").split())
    text = _PROXY_CREDENTIAL_RE.sub(_redact_proxy_credentials, text)
    text = _BARE_PROXY_CREDENTIAL_RE.sub("[redacted]", text)
    text = _QUOTED_SECRET_RE.sub(r"\1\2[redacted]\2", text)
    text = _UNQUOTED_SECRET_RE.sub(r"\1[redacted]", text)
    text = _AUTH_SCHEME_RE.sub("authorization [redacted]", text)
    text = _URL_RE.sub("[url-redacted]", text)
    text = _TOKENISH_RE.sub("[token-redacted]", text)
    return text[:MAX_REDACTED_ERROR_LENGTH]
