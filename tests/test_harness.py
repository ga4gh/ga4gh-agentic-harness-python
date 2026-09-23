from __future__ import annotations

import httpx
import respx

from ga4gh_agentic_harness.auth import AuthorityContext, EnvironmentBearerCredentialProvider
from ga4gh_agentic_harness.harness import Harness
from ga4gh_agentic_harness.http import SafeHttpClient
from ga4gh_agentic_harness.models import ErrorCode, Operation, ResultStatus
from ga4gh_agentic_harness.policy import PolicyDecision, PolicyRequest
from ga4gh_agentic_harness.registry import ServiceRegistry
from ga4gh_agentic_harness.tracing import MemoryTraceSink


def _harness(settings, traces=None) -> Harness:
    http = SafeHttpClient(settings)
    return Harness(
        settings=settings,
        http=http,
        registry=ServiceRegistry(http, settings),
        traces=traces,
    )


@respx.mock
async def test_describe_and_search_envelopes(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    async with _harness(settings) as harness:
        described = await harness.harness_describe()
        searched = await harness.service_search(product="DRS", limit=1)
    assert described.status == ResultStatus.SUCCESS
    operations = {item["operation"] for item in described.data["capabilities"]}
    assert Operation.WES_RUN_SUBMIT.value in operations
    assert searched.page and searched.page.total == 1
    assert searched.data[0].id == "drs-1"


@respx.mock
async def test_service_probe_and_conformance(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    respx.get("https://drs.test/ga4gh/drs/v1/service-info").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "drs-1",
                "name": "DRS",
                "type": {"artifact": "drs", "version": "1.4.0"},
                "organization": {"name": "Example"},
                "version": "1",
            },
        )
    )
    traces = MemoryTraceSink()
    async with _harness(settings, traces) as harness:
        probe = await harness.service_probe("drs-1")
        report = await harness.conformance_assess("drs-1")
    assert probe.data.status == "live"
    assert report.data["certification"] is False
    assert len(traces.events) == 4


@respx.mock
async def test_service_probe_uses_explicit_metadata_url(settings, registry_items) -> None:
    items = list(registry_items)
    items[2] = dict(items[2]) | {"serviceInfoUrl": "https://beacon.test/api/info"}
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=items)
    )
    route = respx.get("https://beacon.test/api/info").mock(
        return_value=httpx.Response(200, json={"meta": {"apiVersion": "v2.0"}})
    )
    async with _harness(settings) as harness:
        result = await harness.service_probe("beacon-1")
    assert result.status == ResultStatus.SUCCESS
    assert route.called


@respx.mock
async def test_drs_error_is_structured(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    respx.get("https://drs.test/ga4gh/drs/v1/objects/missing").mock(
        return_value=httpx.Response(404, json={"msg": "missing"})
    )
    async with _harness(settings) as harness:
        result = await harness.drs_object_resolve("drs-1", "missing")
    assert result.status == ResultStatus.FAILURE
    assert result.errors[0].code == ErrorCode.NOT_FOUND


@respx.mock
async def test_wes_submit_policy_ledger_and_idempotency(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    route = respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "remote-1"})
    )
    async with _harness(settings) as harness:
        denied = await harness.wes_run_submit(
            "wes-1",
            workflow_url="trs://workflow/1",
            workflow_type="CWL",
            workflow_type_version="v1.2",
            workflow_params={},
            idempotency_key="same",
        )
        authority = AuthorityContext(
            software_actor="test", inbound_scopes=["ga4gh:workflow:submit"]
        )
        accepted = await harness.wes_run_submit(
            "wes-1",
            workflow_url="trs://workflow/1",
            workflow_type="CWL",
            workflow_type_version="v1.2",
            workflow_params={},
            idempotency_key="same",
            authority=authority,
        )
        replay = await harness.wes_run_submit(
            "wes-1",
            workflow_url="trs://workflow/1",
            workflow_type="CWL",
            workflow_type_version="v1.2",
            workflow_params={},
            idempotency_key="same",
            authority=authority,
        )
    assert denied.errors[0].code == ErrorCode.POLICY_DENIED
    assert accepted.data["ledger"]["remote_run_id"] == "remote-1"
    assert replay.warnings[0].code == "IDEMPOTENT_REPLAY"
    assert route.call_count == 1


@respx.mock
async def test_wes_unknown_submission_is_not_retried(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    route = respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        side_effect=httpx.ReadTimeout("outcome unknown")
    )
    authority = AuthorityContext(
        software_actor="test", inbound_scopes=["ga4gh:workflow:submit"]
    )
    async with _harness(settings) as harness:
        first = await harness.wes_run_submit(
            "wes-1",
            workflow_url="trs://workflow/1",
            workflow_type="CWL",
            workflow_type_version="v1.2",
            workflow_params={},
            idempotency_key="unknown",
            authority=authority,
        )
        second = await harness.wes_run_submit(
            "wes-1",
            workflow_url="trs://workflow/1",
            workflow_type="CWL",
            workflow_type_version="v1.2",
            workflow_params={},
            idempotency_key="unknown",
            authority=authority,
        )
    assert first.status == ResultStatus.FAILURE
    assert second.status == ResultStatus.FAILURE
    assert "reconcile" in second.errors[0].message
    assert route.call_count == 1


async def test_dispatch_rejects_unknown_operation(settings) -> None:
    async with _harness(settings) as harness:
        try:
            await harness.dispatch("ga4gh.unknown", {})
        except ValueError as exc:
            assert "not a valid Operation" in str(exc)
        else:
            raise AssertionError("unknown operation was accepted")


@respx.mock
async def test_credential_is_not_sent_to_cross_origin_registry_metadata_url(
    settings, registry_items, monkeypatch
) -> None:
    items = list(registry_items)
    items[0] = dict(items[0]) | {"serviceInfoUrl": "https://evil.test/ga4gh/drs/v1/service-info"}
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=items)
    )
    evil = respx.get("https://evil.test/ga4gh/drs/v1/objects/object-1").mock(
        return_value=httpx.Response(200, json={"id": "object-1"})
    )
    monkeypatch.setenv("TEST_DRS_TOKEN", "top-secret")
    provider = EnvironmentBearerCredentialProvider(
        variable="TEST_DRS_TOKEN", service_id="drs-1", audience="https://drs.test/ga4gh/drs/v1"
    )
    http = SafeHttpClient(settings)
    harness = Harness(
        settings=settings,
        http=http,
        registry=ServiceRegistry(http, settings),
        credentials=provider,
    )
    async with harness:
        result = await harness.drs_object_resolve("drs-1", "object-1")
    assert result.status == ResultStatus.FAILURE
    assert result.errors[0].code == ErrorCode.SECURITY
    assert not any("authorization" in call.request.headers for call in evil.calls)


class _ApprovalPolicy:
    async def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        if request.side_effects:
            return PolicyDecision(
                allowed=True, reason="scope present, approval pending", approval_required=True
            )
        return PolicyDecision(allowed=True, reason="read-only operation")


_SUBMIT = {
    "workflow_url": "trs://workflow/1",
    "workflow_type": "CWL",
    "workflow_type_version": "v1.2",
    "workflow_params": {},
}


@respx.mock
async def test_policy_approval_requirement_blocks_side_effects(settings, registry_items) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    submit = respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "remote-1"})
    )
    cancel = respx.post("https://wes.test/ga4gh/wes/v1/runs/remote-1/cancel").mock(
        return_value=httpx.Response(200, json={"run_id": "remote-1"})
    )
    http = SafeHttpClient(settings)
    harness = Harness(
        settings=settings,
        http=http,
        registry=ServiceRegistry(http, settings),
        policy=_ApprovalPolicy(),
    )
    authority = AuthorityContext(software_actor="test", inbound_scopes=["ga4gh:*"])
    async with harness:
        submitted = await harness.wes_run_submit("wes-1", **_SUBMIT, authority=authority)
        cancelled = await harness.wes_run_cancel("wes-1", "remote-1", authority=authority)
    assert submitted.errors[0].code == ErrorCode.APPROVAL_REQUIRED
    assert cancelled.errors[0].code == ErrorCode.APPROVAL_REQUIRED
    assert not submit.called and not cancel.called


@respx.mock
async def test_denied_submission_does_not_consume_idempotency_key(
    settings, registry_items
) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    route = respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "remote-1"})
    )
    async with _harness(settings) as harness:
        denied = await harness.wes_run_submit(
            "wes-1", **_SUBMIT, idempotency_key="k", authority=AuthorityContext(software_actor="a")
        )
        accepted = await harness.wes_run_submit(
            "wes-1",
            **_SUBMIT,
            idempotency_key="k",
            authority=AuthorityContext(
                software_actor="a", inbound_scopes=["ga4gh:workflow:submit"]
            ),
        )
    assert denied.errors[0].code == ErrorCode.POLICY_DENIED
    assert accepted.status == ResultStatus.SUCCESS, accepted.errors
    assert route.call_count == 1


@respx.mock
async def test_ledger_record_cannot_be_updated_by_another_caller(
    settings, registry_items
) -> None:
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=registry_items)
    )
    respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "remote-1"})
    )
    other_run = respx.get("https://wes.test/ga4gh/wes/v1/runs/other-run").mock(
        return_value=httpx.Response(200, json={"run_id": "other-run", "state": "CANCELED"})
    )
    owner = AuthorityContext(software_actor="owner", inbound_scopes=["ga4gh:workflow:submit"])
    async with _harness(settings) as harness:
        submitted = await harness.wes_run_submit("wes-1", **_SUBMIT, authority=owner)
        local_run_id = submitted.data["ledger"]["local_run_id"]
        foreign = await harness.wes_run_get(
            "wes-1",
            "other-run",
            local_run_id=local_run_id,
            authority=AuthorityContext(software_actor="intruder"),
        )
        record = await harness.ledger.get(local_run_id)
    assert foreign.errors[0].code == ErrorCode.NOT_AUTHORIZED
    assert not other_run.called
    assert (record.state, record.remote_run_id) == ("submitted", "remote-1")


@respx.mock
async def test_beacon_unsupported_version_maps_to_not_supported(settings, registry_items) -> None:
    items = [dict(i) for i in registry_items]
    items[2] = items[2] | {"standardVersion": {"ga4ghProduct": "Beacon", "version": "0.2.0"}}
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=items)
    )
    async with _harness(settings) as harness:
        result = await harness.beacon_variant_query("beacon-1", {"referenceName": "1"})
    assert result.status == "failure"
    assert result.errors[0].code == "NOT_SUPPORTED"


@respx.mock
async def test_beacon_query_shape_warns_on_position_match_and_plain_http(
    settings, registry_items
) -> None:
    items = [dict(i) for i in registry_items]
    items[2] = items[2] | {
        "url": "http://beacon.test/api",
        "standardVersion": {"ga4ghProduct": "Beacon", "version": "0.0.0"},
        "queryShape": {"parameters": {"chrom": "{referenceName}", "pos": "{start}"},
                       "positions": "1-based", "matchesOn": "position",
                       "answer": {"format": "json", "exists": "response.exists"}},
    }
    respx.get("https://registry.test/api/services").mock(
        return_value=httpx.Response(200, json=items)
    )
    respx.get("http://beacon.test/api").mock(
        return_value=httpx.Response(200, json={"response": {"exists": "true"}})
    )
    settings.allow_http = True
    async with _harness(settings) as harness:
        result = await harness.beacon_variant_query(
            "beacon-1", {"referenceName": "12", "start": 25398283}
        )
    assert result.status == "success"
    assert result.data["exists"] is True
    assert result.data["request"]["parameters"] == {"chrom": "12", "pos": "25398284"}
    assert [w.code for w in result.warnings] == [
        "NOT_CLINICAL_INTERPRETATION", "POSITION_MATCH_ONLY", "PLAIN_HTTP"]
