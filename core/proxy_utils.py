"""Common parsing helpers for HTTP and SOCKS proxy URLs."""
from __future__ import annotations

import hashlib
import re
import secrets
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit


SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_1024PROXY_HOST_SUFFIXES = ("1024proxy.io", "1024proxy.com", "1024proxy.net")
_STICKY_SESSION_MARKER = re.compile(r"-sid-[^:@\s]+-t-\d+(?:$|[-_])", re.IGNORECASE)


@dataclass(frozen=True)
class ProxySpec:
    raw: str
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""


def parse_proxy_url(value: str, *, allow_bare: bool = False) -> ProxySpec:
    text = str(value or "").strip()
    if not text:
        raise ValueError("代理地址为空")
    text = text.replace("\\@", "@")
    if allow_bare and "://" not in text:
        text = f"socks5://{text}"
    parsed = urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议: {scheme or '-'}")
    if not parsed.hostname:
        raise ValueError("代理格式缺少 host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口无效") from exc
    if not port or not 1 <= port <= 65535:
        raise ValueError("代理格式缺少有效 port")
    return ProxySpec(
        raw=text,
        scheme=scheme,
        host=parsed.hostname,
        port=port,
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
    )


def is_1024proxy_dynamic_gateway(value: str) -> bool:
    """Return whether ``value`` is a supported 1024Proxy gateway URL.

    The provider uses subdomains such as ``us.1024proxy.io`` for dynamic
    gateways.  Only the provider's own DNS suffixes are accepted so an
    arbitrary hostname containing the provider name cannot be rewritten.
    """
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").rstrip(".").lower()
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.scheme.lower() in SUPPORTED_PROXY_SCHEMES
            and bool(port and 1 <= port <= 65535)
            and any(host == suffix or host.endswith("." + suffix)
                    for suffix in _1024PROXY_HOST_SUFFIXES)
        )
    except ValueError:
        return False


def _raw_proxy_username(parsed) -> str:
    """Return the URL-encoded username without decoding userinfo."""
    raw_at = parsed.netloc.rfind("@")
    if raw_at < 0:
        return ""
    raw_userinfo = parsed.netloc[:raw_at]
    raw_colon = raw_userinfo.find(":")
    return raw_userinfo if raw_colon < 0 else raw_userinfo[:raw_colon]


def _liveness_sticky_session_id(
    proxy_url: str,
    account_key: str,
    route_attempt: int,
    run_nonce: str | None = None,
) -> str:
    """Create a bounded opaque ID using a fresh in-memory run nonce."""
    parsed = urlsplit(proxy_url)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port or 0
    except ValueError:
        port = 0
    nonce = str(run_nonce or "").strip() or secrets.token_hex(16)
    material = (
        f"liveness:{nonce}:{str(account_key or '').strip().lower()}:"
        f"{int(route_attempt)}:{host}:{port}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def liveness_proxy_url(
    value: str,
    *,
    account_key: str,
    route_attempt: int,
    run_nonce: str | None = None,
) -> str:
    """Add a per-account, per-attempt 1024Proxy sticky session when needed.

    Existing provider sticky parameters are deliberately returned byte-for-byte
    unchanged.  A missing ``run_nonce`` creates a fresh cryptographically random
    nonce for this invocation; callers may pass one in-memory nonce for a whole
    liveness job.  For a new session only the raw username portion is extended;
    encoded usernames/passwords, host, path and query are not decoded or
    rebuilt, so credentials keep their original URL representation.
    """
    text = str(value or "").strip()
    if not text or not is_1024proxy_dynamic_gateway(text):
        return text
    parsed = urlsplit(text)
    raw_at = parsed.netloc.rfind("@")
    if raw_at < 0:
        return text
    raw_userinfo = parsed.netloc[:raw_at]
    raw_username = _raw_proxy_username(parsed)
    raw_password = raw_userinfo[len(raw_username):]
    if not raw_username or _STICKY_SESSION_MARKER.search(unquote(raw_username)):
        return text
    session_id = _liveness_sticky_session_id(
        text, account_key, route_attempt, run_nonce=run_nonce,
    )
    updated_netloc = f"{raw_username}-sid-{session_id}-t-10{raw_password}{parsed.netloc[raw_at:]}"
    return urlunsplit((parsed.scheme, updated_netloc, parsed.path, parsed.query, parsed.fragment))


def mask_proxy_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        spec = parse_proxy_url(text)
    except ValueError:
        return "***"
    auth = "***:***@" if spec.username or spec.password else ""
    return f"{spec.scheme}://{auth}{spec.host}:{spec.port}"


def validate_proxy_lines(lines) -> None:
    for index, line in enumerate(lines or [], 1):
        try:
            parse_proxy_url(line)
        except ValueError as exc:
            raise ValueError(f"代理池第 {index} 行格式错误: {exc}") from exc


def diagnose_proxy_endpoint(value: str, *, timeout: float = 2.0) -> str:
    try:
        spec = parse_proxy_url(value)
        with socket.create_connection((spec.host, spec.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"\x05\x01\x00")
            response = sock.recv(512)
    except Exception:
        return ""
    if response.startswith(b"HTTP/"):
        first_line = response.splitlines()[0].decode("latin1", errors="replace")
        return f"代理端口返回 {first_line}，当前端口可能不是 SOCKS5 或授权/白名单不匹配"
    if response[:1] == b"\x05":
        return "SOCKS5 握手响应正常，可能是账号密码或目标地址被拒绝"
    return "代理端口返回了非 SOCKS5 响应，请确认协议和端口"


def rotate_sticky_session(value: str, *, salt: str = "") -> str:
    """把 1024Proxy 粘性会话 id 换成新的，拿到另一个出口 IP。

    只改用户名里的 ``-sid-<id>-t-<ttl>`` 段；没有该段或不是 URL 形式时原样返回。
    用于 Cloudflare 边缘 403 时换出口重试，避免整条任务直接失败。
    """
    text = str(value or "").strip()
    if not text:
        return text
    parsed = urlsplit(text)
    raw_at = parsed.netloc.rfind("@")
    if raw_at < 0:
        return text
    raw_userinfo = parsed.netloc[:raw_at]
    raw_colon = raw_userinfo.find(":")
    raw_username = raw_userinfo if raw_colon < 0 else raw_userinfo[:raw_colon]
    raw_password = "" if raw_colon < 0 else raw_userinfo[raw_colon:]
    base = _STICKY_SESSION_MARKER.sub("", unquote(raw_username))
    session_id = hashlib.sha256(
        f"{salt}:{secrets.token_hex(16)}".encode("utf-8")
    ).hexdigest()[:12]
    netloc = f"{base}-sid-{session_id}-t-10{raw_password}{parsed.netloc[raw_at:]}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
