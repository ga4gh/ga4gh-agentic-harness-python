from __future__ import annotations

import httpx
import pytest
import respx

from ga4gh_agentic_harness.http import SafeHttpClient
from ga4gh_agentic_harness.models import ServiceDescriptor
from ga4gh_agentic_harness.registry import RegistryError, ServiceRegistry, normalize_service
from ga4gh_agentic_harness.settings import GA4GH_IMPLEMENTATION_REGISTRY, RegistrySource, Settings


def test_normalize_service(registry_items) -> None:
    service = normalize_service(registry_items[0])
    assert service.id == "drs-1"
    assert service.product == "DRS"
    assert service.organization == "Example Org"


def test_normalize_standard_service_registry_record() -> None:
    service = normalize_service(
        {
            "id": "local.drs",
            "name": "Local DRS",
            "type": {"group": "org.ga4gh", "artifact": "drs", "version": "1.4.0"},
            "organization": {"name": "Local"},
            "version": "0.1.0",
            "url": "http://127.0.0.1:8080/ga4gh/drs/v1",
        }
    )
    assert service.product == "drs"
    assert service.standard_version == "1.4.0"
    assert service.implementation_id == "local.drs"


@respx.mock
async def test_search_filter_pagination_and_cache(settings, registry_items) -> None:
    route = respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    http = SafeHttpClient(settings)
    registry = ServiceRegistry(http, settings)
    services, next_cursor, total = await registry.search(product="DRS", limit=1)
    assert [service.id for service in services] == ["drs-1"]
    assert next_cursor is None and total == 1
    assert (await registry.get("example.drs")).id == "drs-1"
    assert route.call_count == 1
    await http.aclose()


@respx.mock
async def test_search_free_text(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    http = SafeHttpClient(settings)
    registry = ServiceRegistry(http, settings)
    services, _, total = await registry.search(query="workflow")
    assert total == 1 and services[0].id == "wes-1"
    await http.aclose()


async def test_static_service_resolves_without_remote_registry(settings) -> None:
    service = ServiceDescriptor(
        id="private-wes",
        product="WES",
        url="https://private.test/ga4gh/wes/v1",
    )
    http = SafeHttpClient(settings)
    registry = ServiceRegistry(http, settings, static_services=[service])
    try:
        resolved = await registry.get(service.id)
    finally:
        await http.aclose()
    assert resolved == service


WES_RECORD = {
    "id": "local.ga4gh-wes",
    "type": {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"},
    "url": "https://local.test/ga4gh/wes/v1",
}


def _with_registries(settings, *registries):
    return settings.model_copy(
        update={"registries": [RegistrySource(url=url, api=api) for api, url in registries]}
    )


@respx.mock
async def test_registries_merge_in_order_and_label_their_source(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    respx.get("https://local.test/ga4gh/registry/services").mock(
        return_value=httpx.Response(200, json=[WES_RECORD])
    )
    configured = _with_registries(
        settings,
        ("implementation-registry", "https://registry.test/api"),
        ("service-registry", "https://local.test/ga4gh/registry"),
    )
    http = SafeHttpClient(configured)
    try:
        services = {
            service.id: service for service in await ServiceRegistry(http, configured).all()
        }
    finally:
        await http.aclose()
    assert services["local.ga4gh-wes"].source == "https://local.test/ga4gh/registry"
    assert services["drs-1"].source == "ga4gh-implementation-registry"


@respx.mock
async def test_an_unreachable_registry_does_not_hide_the_others(settings) -> None:
    respx.get("https://down.test/services").mock(return_value=httpx.Response(503))
    respx.get("https://local.test/ga4gh/registry/services").mock(
        return_value=httpx.Response(200, json=[WES_RECORD])
    )
    configured = _with_registries(
        settings,
        ("implementation-registry", "https://down.test"),
        ("service-registry", "https://local.test/ga4gh/registry"),
    )
    http = SafeHttpClient(configured)
    try:
        services, _, total = await ServiceRegistry(http, configured).search(product="WES")
    finally:
        await http.aclose()
    assert total == 1 and services[0].id == "local.ga4gh-wes"


@respx.mock
async def test_every_registry_failing_is_an_error(settings) -> None:
    respx.get("https://down.test/services").mock(return_value=httpx.Response(503))
    configured = _with_registries(settings, ("service-registry", "https://down.test"))
    http = SafeHttpClient(configured)
    try:
        with pytest.raises(RegistryError, match=r"down\.test"):
            await ServiceRegistry(http, configured).all()
    finally:
        await http.aclose()


def test_default_is_the_ga4gh_implementation_registry(monkeypatch) -> None:
    monkeypatch.delenv("GA4GH_HARNESS_REGISTRIES", raising=False)
    assert Settings().registries == [GA4GH_IMPLEMENTATION_REGISTRY]


def test_registries_come_from_json_and_replace_the_default(monkeypatch) -> None:
    monkeypatch.setenv(
        "GA4GH_HARNESS_REGISTRIES",
        '[{"url": "http://127.0.0.1:18090/ga4gh/registry", "api": "service-registry"}]',
    )
    assert Settings().registries == [
        RegistrySource(url="http://127.0.0.1:18090/ga4gh/registry", api="service-registry")
    ]


def test_registry_list_must_be_non_empty_and_name_a_known_api() -> None:
    with pytest.raises(ValueError, match="at least one registry"):
        Settings(registries=[])
    with pytest.raises(ValueError):
        Settings(registries=[{"url": "https://x.test", "api": "probe-it"}])
