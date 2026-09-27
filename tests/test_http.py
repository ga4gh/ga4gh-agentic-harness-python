from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest
import respx

from ga4gh_agentic_harness.auth import OutboundCredential
from ga4gh_agentic_harness.http import SafeHttpClient, UnsafeUrlError
from ga4gh_agentic_harness.settings import Settings


@respx.mock
async def test_json_response_and_retry(settings: Settings) -> None:
    route = respx.get("https://service.test/value").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    client = SafeHttpClient(settings)
    result = await client.request("GET", "https://service.test/value")
    assert result.ok and result.json == {"ok": True}
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_post_is_not_retried(settings: Settings) -> None:
    route = respx.post("https://service.test/value").mock(return_value=httpx.Response(503))
    client = SafeHttpClient(settings)
    result = await client.request("POST", "https://service.test/value", json_body={})
    assert result.status == 503
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_credentialed_cross_origin_redirect_is_blocked(settings: Settings) -> None:
    respx.get("https://service.test/value").mock(
        return_value=httpx.Response(302, headers={"Location": "https://other.test/value"})
    )
    client = SafeHttpClient(settings)
    result = await client.request(
        "GET",
        "https://service.test/value",
        credential=OutboundCredential(headers={"Authorization": "Bearer secret"}),
    )
    assert result.error_kind == "security"
    assert "redirect" in (result.error or "")
    await client.aclose()


async def test_http_and_private_ip_are_blocked() -> None:
    settings = Settings(max_retries=0)
    client = SafeHttpClient(settings)
    with pytest.raises(UnsafeUrlError):
        await client.request("GET", "http://example.com")
    with pytest.raises(UnsafeUrlError):
        await client.request("GET", "https://127.0.0.1/value")


@respx.mock
async def test_response_size_limit(settings: Settings) -> None:
    settings.max_response_bytes = 4
    respx.get("https://service.test/value").mock(return_value=httpx.Response(200, text="large"))
    client = SafeHttpClient(settings)
    result = await client.request("GET", "https://service.test/value")
    assert result.error_kind == "invalid_response"
    await client.aclose()



@respx.mock
async def test_cross_origin_redirect_is_blocked_for_any_credential_header(
    settings: Settings,
) -> None:
    respx.get("https://service.test/value").mock(
        return_value=httpx.Response(302, headers={"Location": "https://other.test/value"})
    )
    other = respx.get("https://other.test/value").mock(return_value=httpx.Response(200, json={}))
    client = SafeHttpClient(settings)
    result = await client.request(
        "GET",
        "https://service.test/value",
        credential=OutboundCredential(headers={"X-Api-Key": "secret"}),
    )
    assert result.error_kind == "security"
    assert not other.called
    await client.aclose()


@respx.mock
async def test_credentialed_redirect_cannot_downgrade_to_http(settings: Settings) -> None:
    settings.allow_http = True
    respx.get("https://service.test/value").mock(
        return_value=httpx.Response(302, headers={"Location": "http://service.test/value"})
    )
    downgraded = respx.get("http://service.test/value").mock(
        return_value=httpx.Response(200, json={})
    )
    client = SafeHttpClient(settings)
    result = await client.request(
        "GET",
        "https://service.test/value",
        credential=OutboundCredential(headers={"Authorization": "Bearer secret"}),
    )
    assert result.error_kind == "security"
    assert not downgraded.called
    await client.aclose()


@respx.mock
async def test_credential_is_not_sent_outside_its_resource_origin(settings: Settings) -> None:
    other = respx.get("https://other.test/value").mock(return_value=httpx.Response(200, json={}))
    client = SafeHttpClient(settings)
    credential = OutboundCredential(
        headers={"Authorization": "Bearer secret"}, resource="https://service.test/api"
    )
    with pytest.raises(UnsafeUrlError):
        await client.request("GET", "https://other.test/value", credential=credential)
    assert not other.called
    await client.aclose()


@respx.mock
async def test_credential_origin_match_ignores_default_port(settings: Settings) -> None:
    route = respx.get("https://service.test/api/value").mock(
        return_value=httpx.Response(200, json={})
    )
    client = SafeHttpClient(settings)
    credential = OutboundCredential(
        headers={"Authorization": "Bearer secret"}, resource="https://SERVICE.test:443/api"
    )
    result = await client.request("GET", "https://service.test/api/value", credential=credential)
    assert result.ok and route.called
    await client.aclose()


async def test_credential_is_not_sent_over_plain_http(settings: Settings) -> None:
    settings.allow_http = True
    client = SafeHttpClient(settings)
    credential = OutboundCredential(
        headers={"Authorization": "Bearer secret"}, resource="http://service.test/api"
    )
    with pytest.raises(UnsafeUrlError):
        await client.request("GET", "http://service.test/api/value", credential=credential)
    await client.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "https://100.100.100.200/latest/meta-data/",  # shared address space (RFC 6598)
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://[64:ff9b::a9fe:a9fe]/latest/meta-data/",  # NAT64 of 169.254.169.254
        "https://[2002:a9fe:a9fe::]/",  # 6to4 of 169.254.169.254
    ],
)
async def test_non_global_address_forms_are_blocked(url: str) -> None:
    client = SafeHttpClient(Settings(max_retries=0))
    with pytest.raises(UnsafeUrlError):
        await client.request("GET", url)
    await client.aclose()


async def test_dns_rebinding_to_loopback_is_blocked_at_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_getaddrinfo = socket.getaddrinfo
    answers = iter(["93.184.216.34", "127.0.0.1", "127.0.0.1", "127.0.0.1"])

    def rebinding_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host in ("rebind.test", b"rebind.test"):
            address = next(answers)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_getaddrinfo)
    client = SafeHttpClient(Settings(allow_http=True, max_retries=0))
    try:
        result = await client.request("GET", f"http://rebind.test:{port}/value")
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
    assert result.status is None
    assert result.error_kind == "security"
    assert requests == []


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:18090/x", "http://localhost:18090/x", "http://[::1]:18090/x"]
)
async def test_plain_http_to_loopback_follows_the_private_host_setting(url: str) -> None:
    allowed = SafeHttpClient(Settings(allow_private_hosts=True, max_retries=0))
    refused = SafeHttpClient(Settings(max_retries=0))
    try:
        await allowed._validate_url(url)
        with pytest.raises(UnsafeUrlError, match="only HTTPS"):
            await refused._validate_url(url)
    finally:
        await allowed.aclose()
        await refused.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/x",
        "http://10.0.0.5/x",
        # A name that resolves to loopback is not treated as loopback: names are not resolved.
        "http://localtest.me/x",
    ],
)
async def test_plain_http_elsewhere_still_needs_allow_http(url: str) -> None:
    client = SafeHttpClient(Settings(allow_private_hosts=True, max_retries=0))
    try:
        with pytest.raises(UnsafeUrlError, match="only HTTPS"):
            await client._validate_url(url)
    finally:
        await client.aclose()


async def test_credentials_are_not_sent_over_plain_http_to_loopback() -> None:
    client = SafeHttpClient(Settings(allow_private_hosts=True, max_retries=0))
    credential = OutboundCredential(
        headers={"Authorization": "Bearer secret"}, resource="http://127.0.0.1:18090/api"
    )
    try:
        with pytest.raises(UnsafeUrlError):
            await client.request("GET", "http://127.0.0.1:18090/api/value", credential=credential)
    finally:
        await client.aclose()
