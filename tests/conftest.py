from __future__ import annotations

import os
from pathlib import Path

import pytest

from ga4gh_agentic_harness.models import ServiceDescriptor
from ga4gh_agentic_harness.settings import Settings


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run opt-in read-only tests against public GA4GH services",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    enabled = config.getoption("--run-live") or os.getenv("GA4GH_HARNESS_RUN_LIVE_TESTS") == "1"
    if enabled:
        return
    skip = pytest.mark.skip(reason="use --run-live to enable read-only public service tests")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        registries=[{"url": "https://registry.test/api", "api": "implementation-registry"}],
        allow_private_hosts=True,
        max_retries=1,
        retry_backoff_seconds=0,
        ledger_path=tmp_path / "runs.db",
    )


@pytest.fixture
def drs_service() -> ServiceDescriptor:
    return ServiceDescriptor(
        id="drs-1",
        implementation_id="example.drs",
        name="Example DRS",
        product="DRS",
        standard_version="1.4.0",
        url="https://drs.test/ga4gh/drs/v1",
        service_info_url="https://drs.test/ga4gh/drs/v1/service-info",
    )


@pytest.fixture
def wes_service() -> ServiceDescriptor:
    return ServiceDescriptor(
        id="wes-1",
        name="Example WES",
        product="WES",
        standard_version="1.1.0",
        url="https://wes.test/ga4gh/wes/v1",
        service_info_url="https://wes.test/ga4gh/wes/v1/service-info",
    )


@pytest.fixture
def registry_items() -> list[dict[str, object]]:
    return [
        {
            "id": "drs-1",
            "implementationId": "example.drs",
            "name": "Example DRS",
            "url": "https://drs.test/ga4gh/drs/v1",
            "serviceInfoUrl": "https://drs.test/ga4gh/drs/v1/service-info",
            "standardVersion": {"ga4ghProduct": "DRS", "version": "1.4.0"},
            "organisation": {"name": "Example Org"},
            "environment": "test",
        },
        {
            "id": "wes-1",
            "name": "Example WES",
            "url": "https://wes.test/ga4gh/wes/v1",
            "serviceInfoUrl": "https://wes.test/ga4gh/wes/v1/service-info",
            "standardVersion": {"ga4ghProduct": "WES", "version": "1.1.0"},
            "organisation": {"name": "Workflow Org"},
        },
        {
            "id": "beacon-1",
            "name": "Example Beacon",
            "url": "https://beacon.test/api",
            "standardVersion": {"ga4ghProduct": "Beacon", "version": "2.0.0"},
        },
    ]
