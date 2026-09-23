"""Variant evidence assembly workflow realization."""

from __future__ import annotations

from typing import Any

from ..auth import AuthorityContext
from ..harness import Harness
from ._common import data_as_dict, first_error_code, succeeded


def _source_failure(service_id: str, service_type: str, code: str) -> dict[str, Any]:
    status = "auth-blocked" if code in {
        "not_authorized",
        "policy_denied",
        "audience_mismatch",
        "insufficient_scope",
    } else "failed"
    return {
        "service_id": service_id,
        "service_type": service_type,
        "status": status,
        "evidence_count": 0,
        "error_code": code,
    }


def _beacon_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("matches"), list):
        return [item for item in data["matches"] if isinstance(item, dict)]
    if "exists" in data:
        # Beacon v1 (BeaconAlleleResponse) and pre-1.0 Beacons answer with a top-level exists.
        return [{"id": "beacon-existence-response", "kind": "observation", "content": data}]
    response = data.get("response")
    records: list[dict[str, Any]] = []
    if isinstance(response, dict):
        for result_set in response.get("resultSets") or []:
            if isinstance(result_set, dict):
                records.extend(
                    item for item in (result_set.get("results") or []) if isinstance(item, dict)
                )
    if records:
        return records
    if isinstance(response, dict) and "exists" in response:
        return [{"id": "beacon-existence-response", "kind": "observation", "content": response}]
    summary = data.get("responseSummary")
    if isinstance(summary, dict) and "exists" in summary:
        # A Beacon v2 boolean-granularity answer carries only the summary.
        return [{"id": "beacon-existence-response", "kind": "observation", "content": summary}]
    return []


async def run(
    harness: Harness,
    inputs: dict[str, Any],
    *,
    authority: AuthorityContext | None = None,
) -> dict[str, Any]:
    """Gather attributable Beacon and DRS evidence without interpreting it clinically."""
    variant = inputs.get("variant") or {}
    if not variant.get("id") or not variant.get("assembly"):
        raise ValueError("variant.id and variant.assembly are required")
    beacon_targets = inputs.get("beacon_targets") or []
    drs_references = inputs.get("drs_references") or []
    sources: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    query: dict[str, Any] = {
        "assemblyId": variant["assembly"],
        "variantInternalId": variant["id"],
    }
    filters = inputs.get("filters") or []
    if filters:
        query["filters"] = filters

    for service_id in beacon_targets:
        probe = await harness.service_probe(str(service_id), authority=authority)
        if not succeeded(probe):
            sources.append(_source_failure(str(service_id), "beacon", first_error_code(probe)))
            continue
        described = await harness.service_describe(str(service_id), authority=authority)
        if not succeeded(described):
            sources.append(
                _source_failure(str(service_id), "beacon", first_error_code(described))
            )
            continue
        result = await harness.beacon_variant_query(
            str(service_id), query, authority=authority
        )
        if not succeeded(result):
            sources.append(_source_failure(str(service_id), "beacon", first_error_code(result)))
            continue
        records = _beacon_records(data_as_dict(result))
        for index, record in enumerate(records):
            record_id = str(record.get("id") or f"record-{index + 1}")
            kind = str(record.get("kind") or "observation")
            if kind not in {"observation", "frequency", "clinical-assertion"}:
                kind = "observation"
            content = record.get("content")
            if not isinstance(content, dict):
                content = record
            evidence.append(
                {
                    "source_service": str(service_id),
                    "source_record_id": record_id,
                    "kind": kind,
                    "content": content,
                    "provenance": {
                        "operation": "ga4gh.beacon.variant.query",
                        "trace_id": result.trace_id,
                    },
                }
            )
        sources.append(
            {
                "service_id": str(service_id),
                "service_type": "beacon",
                "status": "succeeded",
                "evidence_count": len(records),
            }
        )

    for reference in drs_references:
        service_id = str(reference["service_id"])
        object_id = str(reference["object_id"])
        result = await harness.drs_object_resolve(
            service_id, object_id, authority=authority
        )
        if not succeeded(result):
            sources.append(_source_failure(service_id, "drs", first_error_code(result)))
            continue
        evidence.append(
            {
                "source_service": service_id,
                "source_record_id": object_id,
                "kind": "object-metadata",
                "content": data_as_dict(result),
                "provenance": {
                    "operation": "ga4gh.drs.object.resolve",
                    "trace_id": result.trace_id,
                },
            }
        )
        sources.append(
            {
                "service_id": service_id,
                "service_type": "drs",
                "status": "succeeded",
                "evidence_count": 1,
            }
        )

    succeeded_count = sum(item["status"] == "succeeded" for item in sources)
    return {
        "variant": {"id": variant["id"], "assembly": variant["assembly"]},
        "summary": {
            "queried": len(sources),
            "succeeded": succeeded_count,
            "failed": len(sources) - succeeded_count,
            "evidence_items": len(evidence),
        },
        "sources": sources,
        "evidence": evidence,
        "clinical_conclusion": None,
    }
