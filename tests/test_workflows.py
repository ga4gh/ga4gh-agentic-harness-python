from __future__ import annotations

import httpx
import pytest
import respx

from ga4gh_agentic_harness.auth import AuthorityContext
from ga4gh_agentic_harness.harness import Harness
from ga4gh_agentic_harness.http import SafeHttpClient
from ga4gh_agentic_harness.registry import ServiceRegistry
from ga4gh_agentic_harness.workflows import (
    run_conformance_assessment,
    run_federated_wes_analysis,
    run_variant_evidence_assembly,
)


def _harness(settings) -> Harness:
    http = SafeHttpClient(settings)
    return Harness(settings=settings, http=http, registry=ServiceRegistry(http, settings))


def _service_info(service_id: str, artifact: str) -> dict[str, object]:
    return {
        "id": service_id,
        "name": service_id,
        "type": {"group": "org.ga4gh", "artifact": artifact, "version": "1.0.0"},
        "organization": {"name": "Example"},
        "version": "1.0.0",
    }


@respx.mock
async def test_conformance_workflow(settings) -> None:
    services = [
        {
            "id": "drs-1",
            "url": "https://drs.test/ga4gh/drs/v1",
            "serviceInfoUrl": "https://drs.test/ga4gh/drs/v1/service-info",
            "standardVersion": {"ga4ghProduct": "DRS", "version": "1.4.0"},
        }
    ]
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=services)
    )
    respx.get("https://drs.test/ga4gh/drs/v1/service-info").mock(
        return_value=httpx.Response(200, json=_service_info("drs-1", "drs"))
    )
    async with _harness(settings) as harness:
        output = await run_conformance_assessment(
            harness,
            {
                "targets": [
                    {
                        "service_id": "drs-1",
                        "url": "https://drs.test/ga4gh/drs/v1",
                        "service_type": "org.ga4gh.drs",
                    }
                ],
                "test_packs": [{"id": "ga4gh.service-info.core", "version": "0.1.0"}],
                "allow_mutating_tests": False,
            },
        )
    assert output["summary"] == {
        "assessed": 1,
        "reachable": 1,
        "conformant": 1,
        "non_conformant": 0,
        "inconclusive": 0,
    }
    assert output["disclaimer"] == "Observed test evidence; not GA4GH certification."


@respx.mock
async def test_variant_evidence_workflow(settings) -> None:
    services = [
        {
            "id": "beacon-1",
            "url": "https://beacon.test/api",
            "standardVersion": {"ga4ghProduct": "Beacon", "version": "2.0.0"},
        },
        {
            "id": "drs-1",
            "url": "https://drs.test/ga4gh/drs/v1",
            "standardVersion": {"ga4ghProduct": "DRS", "version": "1.4.0"},
        },
    ]
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=services)
    )
    respx.get("https://beacon.test/api/service-info").mock(
        return_value=httpx.Response(200, json=_service_info("beacon-1", "beacon"))
    )
    respx.get("https://beacon.test/api/g_variants").mock(
        return_value=httpx.Response(
            200,
            json={
                "matches": [
                    {
                        "id": "record-1",
                        "kind": "frequency",
                        "content": {"allele_count": 4},
                    }
                ]
            },
        )
    )
    respx.get("https://drs.test/ga4gh/drs/v1/objects/object-1").mock(
        return_value=httpx.Response(200, json={"id": "object-1", "size": 10})
    )
    async with _harness(settings) as harness:
        output = await run_variant_evidence_assembly(
            harness,
            {
                "variant": {"id": "ga4gh:VA.example", "assembly": "GRCh38"},
                "beacon_targets": ["beacon-1"],
                "drs_references": [{"service_id": "drs-1", "object_id": "object-1"}],
            },
        )
    assert output["summary"] == {
        "queried": 2,
        "succeeded": 2,
        "failed": 0,
        "evidence_items": 2,
    }
    assert output["clinical_conclusion"] is None
    assert {item["source_service"] for item in output["evidence"]} == {"beacon-1", "drs-1"}


@respx.mock
async def test_federated_wes_workflow_requires_approval_and_completes(settings) -> None:
    services = [
        {
            "id": "trs-1",
            "url": "https://tools.test/ga4gh/trs/v2",
            "standardVersion": {"ga4ghProduct": "TRS", "version": "2.0.1"},
        },
        {
            "id": "wes-1",
            "url": "https://wes.test/ga4gh/wes/v1",
            "serviceInfoUrl": "https://wes.test/ga4gh/wes/v1/service-info",
            "standardVersion": {"ga4ghProduct": "WES", "version": "1.1.0"},
        },
        {
            "id": "wes-unapproved",
            "url": "https://unapproved.test/ga4gh/wes/v1",
            "standardVersion": {"ga4ghProduct": "WES", "version": "1.1.0"},
        },
    ]
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=services)
    )
    respx.get("https://tools.test/ga4gh/trs/v2/tools/org%2Ftool/versions/1.0").mock(
        return_value=httpx.Response(200, json={"id": "1.0"})
    )
    respx.get(
        "https://tools.test/ga4gh/trs/v2/tools/org%2Ftool/versions/1.0/CWL/descriptor"
    ).mock(return_value=httpx.Response(200, json={"content": "class: Workflow"}))
    wes_info = _service_info("wes-1", "wes") | {"workflow_type_versions": {"CWL": ["v1.2"]}}
    respx.get("https://wes.test/ga4gh/wes/v1/service-info").mock(
        return_value=httpx.Response(200, json=wes_info)
    )
    respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "run-1"})
    )
    respx.get("https://wes.test/ga4gh/wes/v1/runs/run-1").mock(
        return_value=httpx.Response(
            200, json={"run_id": "run-1", "state": "COMPLETE", "outputs": {"x": 1}}
        )
    )
    authority = AuthorityContext(
        software_actor="workflow-test", inbound_scopes=["ga4gh:workflow:submit"]
    )
    async with _harness(settings) as harness:
        output = await run_federated_wes_analysis(
            harness,
            {
                "workflow": {
                    "trs_uri": "trs://tools.test/org/tool/1.0",
                    "descriptor_type": "CWL",
                },
                "workflow_inputs": {"message": "hello"},
                "targets": ["wes-1", "wes-unapproved"],
            },
            authority=authority,
            approved_targets={"wes-1"},
            poll_interval_seconds=0,
        )
    assert output["summary"] == {
        "selected": 2,
        "submitted": 1,
        "succeeded": 1,
        "failed": 0,
        "skipped": 1,
        "unknown": 0,
    }
    assert output["targets"][0]["outputs"] == {"x": 1}
    assert output["targets"][1]["error_code"] == "approval_required"


@respx.mock
async def test_federated_wes_workflow_resolves_trs_only_at_the_named_host(settings) -> None:
    services = [
        {
            "id": "trs-lookalike",
            "name": "Mirror of tools.test",
            "url": "https://mirror.test/ga4gh/trs/v2",
            "standardVersion": {"ga4ghProduct": "TRS", "version": "2.0.1"},
        },
        {
            "id": "trs-1",
            "url": "https://tools.test/ga4gh/trs/v2",
            "standardVersion": {"ga4ghProduct": "TRS", "version": "2.0.1"},
        },
    ]
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=services)
    )
    lookalike = respx.get(url__startswith="https://mirror.test/").mock(
        return_value=httpx.Response(200, json={"id": "1.0", "content": "class: Workflow"})
    )
    named = respx.get(url__startswith="https://tools.test/").mock(
        return_value=httpx.Response(200, json={"id": "1.0", "content": "class: Workflow"})
    )
    async with _harness(settings) as harness:
        await run_federated_wes_analysis(
            harness,
            {
                "workflow": {"trs_uri": "trs://tools.test/org/tool/1.0", "descriptor_type": "CWL"},
                "targets": ["wes-1"],
            },
            authority=AuthorityContext(software_actor="workflow-test"),
            approved_targets=set(),
        )
    assert named.called
    assert not lookalike.called


@pytest.mark.parametrize("data", [
    {"beaconId": "b", "apiVersion": "1.0.0", "exists": True, "datasetAlleleResponses": []},
    {"exists": None, "apiVersion": "0.2", "matchesOn": "position", "request": {}, "response": {}},
    {"responseSummary": {"exists": True, "numTotalResults": 1}},
])
def test_beacon_existence_answers_of_every_version_become_evidence(data) -> None:
    from ga4gh_agentic_harness.workflows.variant_evidence_assembly import _beacon_records

    records = _beacon_records(data)
    assert [r["id"] for r in records] == ["beacon-existence-response"]
