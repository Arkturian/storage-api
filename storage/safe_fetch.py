"""SSRF-safe outbound HTTP for user-supplied URLs (#2912).

storage-api fetches URLs that come from requests or from object metadata
(/storage/proxy, /storage/fetch, external_uri objects, the knowledge-graph
pipeline). Those fetches used plain httpx with follow_redirects=True and no
address check, so any caller could read loopback and LAN services
(127.0.0.1:8080/8082/8097, cloud-api, 10.192.26.0/24) through this host.

The guard sits where it cannot be bypassed: in the TCP connect of the httpx
transport. Every connection, including each redirect hop, resolves the host,
refuses the request if *any* resolved address is not globally routable, and
then connects to exactly the address it checked (no second DNS lookup, so
DNS rebinding between check and connect does not work). TLS still verifies
the certificate against the original host name.

Use ``safe_async_client()`` / ``safe_client()`` for every fetch of a URL that
did not originate in this service's own configuration.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Iterable, List, Optional
from urllib.parse import urlsplit

import httpcore
import httpx


class OutboundBlocked(httpx.TransportError):
    """Raised when a URL resolves to a non-public address or is malformed.

    Subclasses httpx.TransportError so existing ``except httpx.HTTPError``
    handlers treat it like any other failed fetch.
    """


_ALLOWED_SCHEMES = {"http", "https"}


def _extra_allowed_hosts() -> set:
    raw = os.getenv("STORAGE_OUTBOUND_ALLOW_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_reserved


def check_url(url: str) -> str:
    """Cheap syntactic pre-check (scheme, host, no userinfo). The address
    check happens at connect time."""
    try:
        parts = urlsplit(str(url))
        parts.port  # noqa: B018  (raises on a bad port)
    except ValueError:
        raise OutboundBlocked("invalid URL")
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.hostname:
        raise OutboundBlocked("only http(s) URLs with a host are allowed")
    if parts.username is not None or parts.password is not None:
        raise OutboundBlocked("credentials in URL are not allowed")
    return str(url)


def resolve_public(host: str, port: int) -> List[str]:
    """Resolve ``host`` and return its addresses if ALL are public."""
    host = (host or "").strip("[]")
    if host.lower() in _extra_allowed_hosts():
        return [host]
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OutboundBlocked(f"cannot resolve host: {exc}")
    addrs: List[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise OutboundBlocked("host has no addresses")
    for addr in addrs:
        if not _is_public(ipaddress.ip_address(addr.split("%")[0])):
            raise OutboundBlocked("destination address is not public")
    return addrs


class _SafeAsyncBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: Optional[httpcore.AsyncNetworkBackend] = None):
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last_exc = None
        for addr in resolve_public(host, port):
            try:
                return await self._inner.connect_tcp(addr, port, timeout=timeout,
                                                     local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, OSError) as exc:
                last_exc = exc
        raise httpcore.ConnectError(str(last_exc) if last_exc else "connect failed")

    async def connect_unix_socket(self, *a, **k):
        raise OutboundBlocked("unix sockets are not allowed")

    async def sleep(self, seconds):
        await self._inner.sleep(seconds)


class _SafeSyncBackend(httpcore.NetworkBackend):
    def __init__(self, inner: Optional[httpcore.NetworkBackend] = None):
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last_exc = None
        for addr in resolve_public(host, port):
            try:
                return self._inner.connect_tcp(addr, port, timeout=timeout,
                                               local_address=local_address,
                                               socket_options=socket_options)
            except (httpcore.ConnectError, OSError) as exc:
                last_exc = exc
        raise httpcore.ConnectError(str(last_exc) if last_exc else "connect failed")

    def connect_unix_socket(self, *a, **k):
        raise OutboundBlocked("unix sockets are not allowed")

    def sleep(self, seconds):
        self._inner.sleep(seconds)


def _install(transport, backend) -> None:
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        # Fail closed if httpx/httpcore internals ever change shape.
        raise RuntimeError("safe_fetch: cannot install network backend on this httpx version")
    pool._network_backend = backend


def safe_async_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient whose every connection is restricted to public IPs.

    Redirects may be followed: each hop opens a new connection through the
    same guard. ``trust_env`` is off so proxy env vars cannot route around it.
    """
    transport = httpx.AsyncHTTPTransport(retries=0)
    _install(transport, _SafeAsyncBackend())
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("max_redirects", 5)
    return httpx.AsyncClient(transport=transport, trust_env=False, **kwargs)


def safe_client(**kwargs) -> httpx.Client:
    transport = httpx.HTTPTransport(retries=0)
    _install(transport, _SafeSyncBackend())
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("max_redirects", 5)
    return httpx.Client(transport=transport, trust_env=False, **kwargs)
