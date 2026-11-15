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

import functools
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


# Befund 108: our own nodes have PUBLIC addresses, so "globally routable" let
# a fetch reach this host and its siblings through their public IP (nginx
# vhosts, services bound to 0.0.0.0). These are refused like loopback.
# Same list as api-ai (#3033, Cloud 15.11.): arkserver/mac-NAT, arkturian v4+v6,
# oneal (.68 and .66), tommy (NAT, both addresses seen). pdrei's old
# 172.104.235.73 is NOT ours any more (#2595) and must not be listed.
_DEFAULT_OWN_IPS = ("188.65.73.164,192.46.232.130,2a01:7e01::f03c:94ff:feb3:d807,"
                    "62.225.187.68,62.225.187.66,217.149.160.166,217.149.167.157")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")


def _own_ips() -> frozenset:
    return _own_ips_for(os.getenv("STORAGE_OUTBOUND_DENY_IPS", _DEFAULT_OWN_IPS))


@functools.lru_cache(maxsize=4)
def _own_ips_for(raw: str) -> frozenset:
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.add(ipaddress.ip_address(part))
            except ValueError:
                pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(ipaddress.ip_address(info[4][0].split("%")[0]))
    except (OSError, ValueError):
        pass
    return frozenset(out)


def _allowed_ports() -> set:
    raw = os.getenv("STORAGE_OUTBOUND_PORTS", "80,443")
    return {int(p) for p in raw.split(",") if p.strip().isdigit()}


def _as_v4(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """IPv4 hidden in an IPv6 form (mapped, NAT64, 6to4) is judged as IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if ip in _SIXTOFOUR and ip.sixtofour is not None:
            return ip.sixtofour
    return ip


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    ip = _as_v4(ip)
    if ip in _own_ips():
        return False
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
    if port is not None and int(port) not in _allowed_ports():
        raise OutboundBlocked("destination port is not allowed")
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


def check_url_resolvable(url: str) -> str:
    """check_url plus the address and port check, for accepting a URL that is
    stored now and fetched later (external_uri). The connect-time check still
    runs on every fetch, so a later DNS change cannot slip through."""
    check_url(url)
    parts = urlsplit(str(url))
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    resolve_public(parts.hostname, port)
    return str(url)
