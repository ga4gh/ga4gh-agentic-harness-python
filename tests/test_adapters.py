from __future__ import annotations

import httpx
import pytest
import respx

from ga4gh_agentic_harness.adapters import BeaconAdapter, DrsAdapter, TrsAdapter, WesAdapter
from ga4gh_agentic_harness.adapters.base import AdapterError
from ga4gh_agentic_harness.adapters.beacon import BeaconVersionError
from ga4gh_agentic_harness.auth import OutboundCredential
from ga4gh_agentic_harness.http import SafeHttpClient
from ga4gh_agentic_harness.models import ServiceDescriptor


@respx.mock
async def test_drs_object_and_access_redaction(settings, drs_service) -> None:
    respx.get("https://drs.test/ga4gh/drs/v1/objects/object%2F1").mock(
        return_value=httpx.Response(200, json={"id": "object/1", "size": 1})
    )
    respx.get("https://drs.test/ga4gh/drs/v1/objects/object%2F1/access/s3").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": "https://bucket.test/object?X-Amz-Signature=secret",
                "headers": {"Authorization": "Bearer secret"},
            },
        )
    )
    http = SafeHttpClient(settings)
    adapter = DrsAdapter(http)
    credential = OutboundCredential()
    obj = await adapter.resolve_object(drs_service, "object/1", credential)
    access = await adapter.resolve_access(drs_service, "object/1", "s3", credential)
    assert obj["id"] == "object/1"
    assert access["url"] == "https://bucket.test/object"
    assert access["headers"] == {"redacted": True}
    assert "secret" not in str(access)
    await http.aclose()


@respx.mock
async def test_trs_resolves_version_and_descriptor(settings) -> None:
    service = ServiceDescriptor(
        id="trs-1", product="TRS", url="https://trs.test/ga4gh/trs/v2"
    )
    root = "https://trs.test/ga4gh/trs/v2/tools/github.com%2Facme%2Fworkflow"
    respx.get(root + "/versions/1.0").mock(
        return_value=httpx.Response(200, json={"id": "1.0"})
    )
    respx.get(root + "/versions/1.0/CWL/descriptor").mock(
        return_value=httpx.Response(200, json={"content": "cwlVersion: v1.2"})
    )
    http = SafeHttpClient(settings)
    result = await TrsAdapter(http).resolve_workflow(
        service,
        "github.com/acme/workflow",
        OutboundCredential(),
        version="1.0",
        descriptor_type="CWL",
    )
    assert result["resolved_version"] == "1.0"
    assert "content" in result["descriptor"]
    await http.aclose()


@respx.mock
async def test_beacon_query(settings) -> None:
    service = ServiceDescriptor(id="b", product="Beacon", url="https://beacon.test/api")
    route = respx.post("https://beacon.test/api/g_variants").mock(
        return_value=httpx.Response(200, json={"response": {"exists": True}})
    )
    http = SafeHttpClient(settings)
    entity = {"query": {"requestParameters": {"geneId": "EIF4A1"},
                        "filters": [{"id": "HP:0100526"}]}}
    result = await BeaconAdapter(http).query_variant(service, entity, OutboundCredential())
    assert result["response"]["exists"] is True
    assert route.calls[0].request.method == "POST"
    assert route.calls[0].request.content and b"HP:0100526" in route.calls[0].request.content
    await http.aclose()


@respx.mock
async def test_beacon_v2_entity_with_a_get_form_is_sent_as_get(settings) -> None:
    service = ServiceDescriptor(id="b", product="Beacon", url="https://beacon.test/api")
    route = respx.get("https://beacon.test/api/g_variants").mock(
        return_value=httpx.Response(200, json={"responseSummary": {"exists": True}})
    )
    entity = {"meta": {"apiVersion": "2.0"}, "query": {
        "requestParameters": {"assemblyId": "GRCh38", "referenceName": "11",
                              "start": [5227001], "end": [5227002, 5227010],
                              "referenceBases": "T", "alternateBases": "A"},
        "requestedGranularity": "boolean", "pagination": {"skip": 0, "limit": 10}}}
    http = SafeHttpClient(settings)
    await BeaconAdapter(http).query_variant(service, entity, OutboundCredential())
    await http.aclose()
    assert dict(route.calls[0].request.url.params) == {
        "assemblyId": "GRCh38", "referenceName": "11", "start": "5227001",
        "end": "5227002,5227010", "referenceBases": "T", "alternateBases": "A",
        "requestedGranularity": "boolean", "skip": "0", "limit": "10"}


@respx.mock
async def test_beacon_flat_query_uses_get(settings) -> None:
    service = ServiceDescriptor(id="b", product="Beacon", url="https://beacon.test/api")
    route = respx.get("https://beacon.test/api/g_variants").mock(
        return_value=httpx.Response(200, json={"responseSummary": {"exists": False}})
    )
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(
        service,
        {"assemblyId": "GRCh38", "referenceName": "1", "start": 100000},
        OutboundCredential(),
    )
    assert result["responseSummary"]["exists"] is False
    assert route.calls[0].request.method == "GET"
    await http.aclose()


@respx.mock
async def test_wes_submit_get_cancel(settings, wes_service) -> None:
    submit = respx.post("https://wes.test/ga4gh/wes/v1/runs").mock(
        return_value=httpx.Response(200, json={"run_id": "run-1"})
    )
    respx.get("https://wes.test/ga4gh/wes/v1/runs/run-1").mock(
        return_value=httpx.Response(200, json={"run_id": "run-1", "state": "COMPLETE"})
    )
    respx.post("https://wes.test/ga4gh/wes/v1/runs/run-1/cancel").mock(
        return_value=httpx.Response(200, json={"run_id": "run-1"})
    )
    http = SafeHttpClient(settings)
    adapter = WesAdapter(http)
    credential = OutboundCredential()
    created = await adapter.submit(
        wes_service,
        credential,
        workflow_url="trs://workflow/1",
        workflow_type="CWL",
        workflow_type_version="v1.2",
        workflow_params={"message": "hello"},
        idempotency_key="idem-1",
    )
    assert created["run_id"] == "run-1"
    assert submit.calls[0].request.headers["Idempotency-Key"] == "idem-1"
    assert (await adapter.get_run(wes_service, "run-1", credential))["state"] == "COMPLETE"
    assert (await adapter.cancel(wes_service, "run-1", credential))["run_id"] == "run-1"
    await http.aclose()


@pytest.mark.parametrize("run_id", ["..", "."])
@respx.mock
async def test_dot_segment_identifiers_cannot_escape_the_resource_path(
    settings, wes_service, run_id
) -> None:
    escaped = respx.post(url__regex=r"https://wes\.test/ga4gh/wes/v1(/runs)?/cancel").mock(
        return_value=httpx.Response(200, json={"run_id": "other"})
    )
    http = SafeHttpClient(settings)
    with pytest.raises(ValueError):
        await WesAdapter(http).cancel(wes_service, run_id, OutboundCredential())
    assert not escaped.called
    await http.aclose()


@respx.mock
async def test_drs_object_inline_access_urls_are_redacted(settings, drs_service) -> None:
    respx.get("https://drs.test/ga4gh/drs/v1/objects/object-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "object-1",
                "access_methods": [
                    {"type": "s3", "access_id": "s3"},
                    {
                        "type": "https",
                        "access_url": {
                            "url": "https://bucket.test/object?X-Amz-Signature=secret",
                            "headers": ["Authorization: Bearer secret"],
                        },
                    },
                ],
            },
        )
    )
    http = SafeHttpClient(settings)
    obj = await DrsAdapter(http).resolve_object(drs_service, "object-1", OutboundCredential())
    assert "secret" not in str(obj)
    assert obj["access_methods"][0] == {"type": "s3", "access_id": "s3"}
    inline = obj["access_methods"][1]["access_url"]
    assert inline["url"] == "https://bucket.test/object"
    assert inline["access_url_query_redacted"] is True
    assert inline["headers"] == {"redacted": True}
    await http.aclose()


# ---- Beacon v1: selected by the version the service declares, never by probing it

def _beacon(version: str | None) -> ServiceDescriptor:
    return ServiceDescriptor(id="b1", product="Beacon", standard_version=version,
                             url="https://beacon1.test/api")


V1_QUERY = {"referenceName": "1", "start": 100000, "referenceBases": "A",
            "alternateBases": "T", "assemblyId": "GRCh37"}


@pytest.mark.parametrize("version", ["v1.0", "1.0.1", "1.1.0", "v1", "0.3.0", "0.4"])
@respx.mock
async def test_beacon_v1_declared_version_uses_query_endpoint(settings, version) -> None:
    route = respx.get("https://beacon1.test/api/query").mock(return_value=httpx.Response(
        200, json={"beaconId": "org.b1", "apiVersion": "v1.0.1", "exists": True}))
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(_beacon(version), dict(V1_QUERY),
                                                     OutboundCredential())
    await http.aclose()
    assert result["exists"] is True
    sent = dict(route.calls[0].request.url.params)
    assert sent == {"referenceName": "1", "start": "100000", "referenceBases": "A",
                    "alternateBases": "T", "assemblyId": "GRCh37"}


@respx.mock
async def test_beacon_v1_translates_v2_request_entity(settings) -> None:
    route = respx.get("https://beacon1.test/api/query").mock(
        return_value=httpx.Response(200, json={"exists": False}))
    entity = {"meta": {"apiVersion": "2.0"}, "query": {
        "requestParameters": {"referenceName": "X", "start": [100, 200], "end": [300, 400],
                              "referenceBases": "N", "variantType": "DEL",
                              "assemblyId": "GRCh38"},
        "includeResultsetResponses": "HIT"}}
    http = SafeHttpClient(settings)
    await BeaconAdapter(http).query_variant(_beacon("v1.0"), entity, OutboundCredential())
    await http.aclose()
    assert dict(route.calls[0].request.url.params) == {
        "referenceName": "X", "startMin": "100", "startMax": "200", "endMin": "300",
        "endMax": "400", "referenceBases": "N", "variantType": "DEL", "assemblyId": "GRCh38",
        "includeDatasetResponses": "HIT"}


@respx.mock
async def test_beacon_v1_renames_resultset_flag_in_flat_query(settings) -> None:
    route = respx.get("https://beacon1.test/api/query").mock(
        return_value=httpx.Response(200, json={"exists": False}))
    http = SafeHttpClient(settings)
    await BeaconAdapter(http).query_variant(
        _beacon("1.0"), V1_QUERY | {"includeResultsetResponses": "ALL"}, OutboundCredential())
    await http.aclose()
    params = dict(route.calls[0].request.url.params)
    assert params["includeDatasetResponses"] == "ALL"
    assert "includeResultsetResponses" not in params


async def test_beacon_v1_missing_required_fields_is_invalid_request(settings) -> None:
    http = SafeHttpClient(settings)
    with pytest.raises(ValueError, match=r"referenceBases.*assemblyId"):
        await BeaconAdapter(http).query_variant(
            _beacon("v1.0"), {"referenceName": "1", "start": 5}, OutboundCredential())
    await http.aclose()


async def test_beacon_v1_has_no_other_entry_types(settings) -> None:
    http = SafeHttpClient(settings)
    with pytest.raises(BeaconVersionError, match="g_variants"):
        await BeaconAdapter(http).query_variant(_beacon("v1.0"), dict(V1_QUERY),
                                                OutboundCredential(), entry_type="individuals")
    await http.aclose()


@pytest.mark.parametrize("version", ["0.2", "0.1.0", "v3.0", "latest"])
async def test_beacon_unsupported_declared_version_is_refused(settings, version) -> None:
    http = SafeHttpClient(settings)
    with pytest.raises(BeaconVersionError, match="Beacon"):
        await BeaconAdapter(http).query_variant(_beacon(version), dict(V1_QUERY),
                                                OutboundCredential())
    await http.aclose()


@pytest.mark.parametrize("version", [None, "v2.0.0", "2.0.0"])
@respx.mock
async def test_beacon_v2_or_undeclared_keeps_g_variants(settings, version) -> None:
    route = respx.get("https://beacon1.test/api/g_variants").mock(
        return_value=httpx.Response(200, json={"responseSummary": {"exists": True}}))
    http = SafeHttpClient(settings)
    await BeaconAdapter(http).query_variant(_beacon(version), dict(V1_QUERY),
                                            OutboundCredential())
    await http.aclose()
    assert route.called


@respx.mock
async def test_beacon_v1_single_position_array_is_start(settings) -> None:
    route = respx.get("https://beacon1.test/api/query").mock(
        return_value=httpx.Response(200, json={"exists": True}))
    entity = {"meta": {}, "query": {"requestParameters": V1_QUERY | {"start": [100000]}}}
    http = SafeHttpClient(settings)
    await BeaconAdapter(http).query_variant(_beacon("1.0"), entity, OutboundCredential())
    await http.aclose()
    assert dict(route.calls[0].request.url.params)["start"] == "100000"


# ---- queryShape: pre-1.0 and pre-standard Beacons, queried as their registry record declares

def _shaped(
    shape: dict, version: str = "0.2", url: str = "https://old.test/beacon"
) -> ServiceDescriptor:
    return ServiceDescriptor(id="old", product="Beacon", standard_version=version, url=url,
                             raw={"queryShape": shape})


UCSC_SHAPE = {
    "path": "/query",
    "parameters": {"dataset": "lovd", "chromosome": "{referenceName}", "position": "{start}",
                   "alternateBases": "{alternateBases}"},
    "positions": "1-based", "chromosome": "bare", "assemblies": {"GRCh37": "GRCh37"},
    "answer": {"format": "json", "exists": "response.exists"},
}


@respx.mock
async def test_query_shape_converts_coordinates_and_reads_string_answer(settings) -> None:
    route = respx.get("https://old.test/beacon/query").mock(return_value=httpx.Response(
        200, json={"beacon": {"api": "0.2"}, "response": {"exists": "true"}}))
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(
        _shaped(UCSC_SHAPE),
        {"referenceName": "chr11", "start": 5248231, "alternateBases": "A",
         "assemblyId": "grch37"},
        OutboundCredential())
    await http.aclose()
    assert dict(route.calls[0].request.url.params) == {
        "dataset": "lovd", "chromosome": "11", "position": "5248232", "alternateBases": "A"}
    assert result["exists"] is True
    assert result["apiVersion"] == "0.2"
    assert result["matchesOn"] == "allele"
    assert result["response"]["response"]["exists"] == "true"


@pytest.mark.parametrize(("value", "expected"), [
    ("false", False), ("null", None), (None, None), (False, False), ([], False), ([{}], True)])
@respx.mock
async def test_query_shape_json_answers(settings, value, expected) -> None:
    shape = UCSC_SHAPE | {"answer": {"format": "json", "exists": "response.exists"}}
    respx.get("https://old.test/beacon/query").mock(
        return_value=httpx.Response(200, json={"response": {"exists": value}}))
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(
        _shaped(shape), dict(V1_QUERY), OutboundCredential())
    await http.aclose()
    assert result["exists"] is expected


@respx.mock
async def test_query_shape_whole_body_list_and_chr_prefix(settings) -> None:
    shape = {"parameters": {"chrom": "{referenceName}", "spos": "{start}",
                            "ref": "{referenceBases}", "alt": "{alternateBases}"},
             "positions": "1-based", "chromosome": "chr",
             "answer": {"format": "json", "exists": ""}}
    route = respx.get("https://old.test/beacon").mock(
        return_value=httpx.Response(200, json=[{"gene": "GABRB3"}]))
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(
        _shaped(shape, version="0.0.0"),
        {"referenceName": "15", "start": 27018840, "referenceBases": "G", "alternateBases": "A"},
        OutboundCredential())
    await http.aclose()
    assert dict(route.calls[0].request.url.params)["chrom"] == "chr15"
    assert dict(route.calls[0].request.url.params)["spos"] == "27018841"
    assert result["exists"] is True


@respx.mock
async def test_query_shape_text_answer_and_form_post(settings) -> None:
    shape = {"method": "POST", "parameters": {"genome": "{assemblyId}", "chr": "{referenceName}",
                                              "coord": "{start}", "allele": "{alternateBases}"},
             "chromosome": "chr", "assemblies": {"GRCh37": "hg19", "hg19": "hg19"},
             "answer": {"format": "text", "found": "Beacon found allele",
                        "notFound": "Beacon cannot find allele"}}
    route = respx.post("http://old.test/beacon.php").mock(return_value=httpx.Response(
        200, text="<html>Beacon found allele G at coordinate chr1:69510</html>"))
    settings.allow_http = True
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(
        _shaped(shape, version="0.0.0", url="http://old.test/beacon.php"),
        {"referenceName": "1", "start": 69510, "alternateBases": "G", "assemblyId": "GRCh37"},
        OutboundCredential())
    await http.aclose()
    body = route.calls[0].request.content.decode()
    assert "genome=hg19" in body and "chr=chr1" in body and "coord=69510" in body
    assert result["exists"] is True
    assert result["response"].startswith("<html>Beacon found")


@respx.mock
async def test_query_shape_unrecognised_text_is_invalid_response(settings) -> None:
    shape = {"parameters": {"pos": "{start}"}, "answer": {
        "format": "text", "found": "Yes", "notFound": "No"}}
    respx.get("https://old.test/beacon").mock(return_value=httpx.Response(200, text=""))
    http = SafeHttpClient(settings)
    with pytest.raises(AdapterError, match="neither") as caught:
        await BeaconAdapter(http).query_variant(
            _shaped(shape), {"start": 5}, OutboundCredential())
    await http.aclose()
    assert caught.value.result.error_kind == "invalid_response"


@pytest.mark.parametrize(("query", "message"), [
    ({"referenceName": "1", "start": 5, "alternateBases": "A"}, "assemblyId is required"),
    (V1_QUERY | {"assemblyId": "GRCh38"}, "does not hold assembly"),
    ({"start": 5, "alternateBases": "A", "assemblyId": "GRCh37"}, "requires referenceName"),
    (V1_QUERY | {"start": [5, 9]}, "exact positions"),
])
async def test_query_shape_rejects_queries_it_cannot_answer(settings, query, message) -> None:
    http = SafeHttpClient(settings)
    with pytest.raises(ValueError, match=message):
        await BeaconAdapter(http).query_variant(_shaped(UCSC_SHAPE), query, OutboundCredential())
    await http.aclose()


@pytest.mark.parametrize("path", ["//evil.test/x", "/../admin", "/q?x=1", "https://evil.test",
                                  "/a/%2e%2e/b", "query"])
async def test_query_shape_path_cannot_leave_the_service_url(settings, path) -> None:
    http = SafeHttpClient(settings)
    with pytest.raises(BeaconVersionError, match="relative path"):
        await BeaconAdapter(http).query_variant(
            _shaped(UCSC_SHAPE | {"path": path}), dict(V1_QUERY), OutboundCredential())
    await http.aclose()


@pytest.mark.parametrize("shape", [
    UCSC_SHAPE | {"parameters": {"x": "{secret}"}},
    UCSC_SHAPE | {"method": "DELETE"},
    UCSC_SHAPE | {"positions": "2-based"},
    UCSC_SHAPE | {"answer": {"format": "xml"}},
    UCSC_SHAPE | {"matchesOn": "gene"},
])
async def test_query_shape_unrecognised_declarations_are_refused(settings, shape) -> None:
    http = SafeHttpClient(settings)
    with pytest.raises(BeaconVersionError):
        await BeaconAdapter(http).query_variant(_shaped(shape), dict(V1_QUERY),
                                                OutboundCredential())
    await http.aclose()


@respx.mock
async def test_query_shape_takes_precedence_over_declared_v1(settings) -> None:
    # VICC declares 0.4 and speaks v1 parameters, but counts positions from 1.
    shape = {"path": "/query", "parameters": {
        "assemblyId": "{assemblyId}", "referenceName": "{referenceName}", "start": "{start}",
        "referenceBases": "{referenceBases}", "alternateBases": "{alternateBases}"},
        "positions": "1-based", "answer": {"format": "json", "exists": "exists"}}
    route = respx.get("https://old.test/beacon/query").mock(
        return_value=httpx.Response(200, json={"apiVersion": "0.4.0", "exists": True}))
    http = SafeHttpClient(settings)
    result = await BeaconAdapter(http).query_variant(
        _shaped(shape, version="0.4.0"), dict(V1_QUERY), OutboundCredential())
    await http.aclose()
    assert dict(route.calls[0].request.url.params)["start"] == "100001"
    assert result["exists"] is True
