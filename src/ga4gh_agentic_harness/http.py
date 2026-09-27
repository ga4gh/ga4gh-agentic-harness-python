"""Resilient outbound HTTP with SSRF and credential-forwarding safeguards."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from .auth import OutboundCredential
from .settings import Settings

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


class UnsafeUrlError(ValueError):
    pass


@dataclass(slots=True)
class HttpResult:
    url: str
    status: int | None = None
    latency_ms: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    json: Any = None
    text: str | None = None
    error: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_loopback_host(host: str) -> bool:
    """True for "localhost" and literal loopback addresses; other names are not resolved."""
    host = host.lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not _is_public_ip(str(embedded)):
            return False
    # is_global excludes ranges that is_private does not, such as the RFC 6598 shared
    # address space that hosts some cloud metadata endpoints (100.100.100.200).
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class _PublicAddressBackend(httpcore.AsyncNetworkBackend):
    """Resolve at connect time and connect only to the public address that was checked.

    Validating a hostname before the request and letting the transport resolve it again
    leaves a DNS rebinding window: the second answer can be a loopback or metadata address.
    """

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        addresses = [str(info[4][0]) for info in infos]
        if not addresses or any(not _is_public_ip(address) for address in addresses):
            raise UnsafeUrlError("host resolves to a private, local, or reserved address")
        return await self._backend.connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeUrlError("unix socket targets are prohibited")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PublicAddressTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, verify: bool) -> None:
        super().__init__(verify=verify)
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        # httpx does not expose a network backend option, so the pool it built is replaced
        # with an equivalent one that uses the connect-time address check.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=5.0,
            network_backend=_PublicAddressBackend(),
        )


class SafeHttpClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self._settings.connect_timeout_seconds,
                read=self._settings.read_timeout_seconds,
                write=self._settings.read_timeout_seconds,
                pool=self._settings.connect_timeout_seconds,
            )
            self._client = httpx.AsyncClient(
                timeout=timeout,
                verify=self._settings.verify_tls,
                transport=(
                    None
                    if self._settings.allow_private_hosts
                    else _PublicAddressTransport(verify=self._settings.verify_tls)
                ),
                follow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": self._settings.user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def _validate_url(self, url: str) -> None:
        parts = urlsplit(url)
        # Plain HTTP to this machine never crosses a network, so it follows the private-host
        # setting instead of needing allow_http, which admits plain HTTP to any host.
        loopback_http = (
            parts.scheme == "http"
            and self._settings.allow_private_hosts
            and _is_loopback_host(parts.hostname or "")
        )
        if not loopback_http and parts.scheme not in (
            {"https", "http"} if self._settings.allow_http else {"https"}
        ):
            raise UnsafeUrlError("only HTTPS URLs are permitted")
        if parts.username or parts.password or not parts.hostname:
            raise UnsafeUrlError("URL credentials and missing hosts are prohibited")
        host = parts.hostname.lower()
        if self._settings.allowed_hosts and host not in self._settings.allowed_hosts:
            raise UnsafeUrlError(f"host {host!r} is not allowlisted")
        if self._settings.allow_private_hosts:
            return
        try:
            if not _is_public_ip(host):
                raise UnsafeUrlError("private, local, and reserved targets are prohibited")
            return
        except ValueError:
            pass
        try:
            addresses = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443)
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"DNS resolution failed for {host!r}: {exc}") from exc
        if not addresses or any(not _is_public_ip(str(item[4][0])) for item in addresses):
            raise UnsafeUrlError("host resolves to a private, local, or reserved address")

    async def request(
        self,
        method: str,
        url: str,
        *,
        credential: OutboundCredential | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> HttpResult:
        method = method.upper()
        current_url = url
        await self._validate_url(current_url)
        credential_origin = self._credential_origin(current_url, credential)
        request_headers = dict(headers or {})
        # Every header a credential provider supplies is secret-bearing, whatever its name
        # (API keys, custom token headers), in addition to the well-known ambient ones.
        sensitive = set(_SENSITIVE_HEADERS)
        if credential:
            request_headers.update(credential.headers)
            sensitive.update(key.lower() for key in credential.headers)
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        attempts = self._settings.max_retries + 1 if method in _SAFE_METHODS else 1

        for _redirect in range(self._settings.max_redirects + 1):
            result: HttpResult | None = None
            for attempt in range(attempts):
                started = time.monotonic()
                try:
                    response = await self._ensure_client().request(
                        method,
                        current_url,
                        headers=request_headers,
                        params=params,
                        json=json_body,
                        data=data,
                        files=files,
                    )
                except Exception as exc:  # transport errors are part of the public result contract
                    result = self._transport_error(current_url, exc, started)
                    retryable_transport = result.error_kind not in {"dns", "tls", "security"}
                    if attempt + 1 < attempts and retryable_transport:
                        await asyncio.sleep(self._settings.retry_backoff_seconds * (2**attempt))
                        continue
                    return result
                result = self._response_result(response, started)
                if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                    await asyncio.sleep(self._settings.retry_backoff_seconds * (2**attempt))
                    continue
                break

            assert result is not None
            if result.status not in {301, 302, 303, 307, 308}:
                return result
            location = result.headers.get("location")
            if not location:
                return result
            next_url = urljoin(current_url, location)
            await self._validate_url(next_url)
            if _origin(next_url) != _origin(current_url) or (
                credential_origin is not None and _origin(next_url) != credential_origin
            ):
                if any(key.lower() in sensitive for key in request_headers):
                    return HttpResult(
                        url=current_url,
                        error="cross-origin redirect blocked for credentialed request",
                        error_kind="security",
                    )
            current_url = next_url
            params = None
            if result.status == 303:
                method, json_body, data, files = "GET", None, None, None
        return HttpResult(url=current_url, error="redirect limit exceeded", error_kind="security")

    @staticmethod
    def _credential_origin(
        url: str, credential: OutboundCredential | None
    ) -> tuple[str, str, int | None] | None:
        """Bind a secret-bearing credential to the exact origin it was acquired for."""
        if credential is None or not credential.headers:
            return None
        bound = _origin(credential.resource or url)
        if bound[0] != "https":
            raise UnsafeUrlError("credentials are only sent to HTTPS origins")
        if _origin(url) != bound:
            raise UnsafeUrlError("credential is not bound to the request origin")
        return bound

    def _response_result(self, response: httpx.Response, started: float) -> HttpResult:
        content = response.content
        if len(content) > self._settings.max_response_bytes:
            return HttpResult(
                url=str(response.url),
                status=response.status_code,
                latency_ms=int((time.monotonic() - started) * 1000),
                error="response exceeds configured size limit",
                error_kind="invalid_response",
            )
        parsed: Any = None
        try:
            parsed = response.json()
        except Exception:
            pass
        return HttpResult(
            url=str(response.url),
            status=response.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            headers={key.lower(): value for key, value in response.headers.items()},
            json=parsed,
            text=None if parsed is not None else response.text,
        )

    @staticmethod
    def _transport_error(url: str, exc: Exception, started: float) -> HttpResult:
        kind = "connection"
        if isinstance(exc, UnsafeUrlError):
            kind = "security"
        elif isinstance(exc, httpx.TimeoutException):
            kind = "timeout"
        elif isinstance(exc, ssl.SSLError) or "ssl" in str(exc).lower():
            kind = "tls"
        elif isinstance(exc, socket.gaierror) or "getaddrinfo" in str(exc).lower():
            kind = "dns"
        return HttpResult(
            url=url,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
            error_kind=kind,
        )
