"""Beacon variant query adapter for every Beacon version: pre-1.0, v1 and v2.

The protocol is chosen from what the service's registry record declares, never by probing the
service:

- A record with a ``queryShape`` is queried exactly as that shape describes, whatever version it
  declares. This is how pre-1.0 Beacons (0.1, 0.2), and Beacons whose API predates any Beacon
  version, are reached: they differ in path, parameter names, coordinate base, chromosome form
  and answer format, and nothing in the version number says which.
- Otherwise the declared version (``ServiceDescriptor.standard_version``) decides. v1, and the
  0.3 and 0.4 APIs whose BeaconAlleleRequest v1 kept, answer allele queries at ``/query``; v2 at
  ``/{entry_type}``. A service that declares no version keeps the v2 behavior this adapter has
  always had. Earlier 0.x versions need a ``queryShape``, and any other major version is refused.
"""

from __future__ import annotations

import re
from typing import Any

from ..auth import OutboundCredential
from ..http import HttpResult
from ..models import ServiceDescriptor
from .base import AdapterError, BaseAdapter, require_json, segment


class BeaconVersionError(Exception):
    """The declared Beacon version, or the requested entry type, has no supported protocol."""


_VERSION = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.\d+)*\s*$", re.IGNORECASE)

# Beacon v1 GA4GH API: required BeaconAlleleRequest fields.
_V1_REQUIRED = ("referenceName", "referenceBases", "assemblyId")

# Pre-1.0 Beacon APIs from 0.3 on use the BeaconAlleleRequest that v1 kept.
_V1_COMPATIBLE_MINOR = 3


def beacon_version(service: ServiceDescriptor) -> tuple[int, int] | None:
    """(major, minor) Beacon version the service declares, or None when it declares none."""
    declared = service.standard_version
    if declared is None or not str(declared).strip():
        return None
    match = _VERSION.match(str(declared))
    if not match:
        raise BeaconVersionError(f"Beacon version {declared!r} is not a recognised version")
    return int(match.group(1)), int(match.group(2) or 0)


def beacon_major_version(service: ServiceDescriptor) -> int | None:
    """Major Beacon version the service declares, or None when it declares none."""
    version = beacon_version(service)
    return version[0] if version else None


def _request_parameters(query: dict[str, Any]) -> dict[str, Any]:
    """The flat parameters of a flat query or of a v2 request entity."""
    if "query" in query or "meta" in query:
        body = query.get("query") or {}
        params = dict(body.get("requestParameters") or {})
        if "includeResultsetResponses" in body:
            params.setdefault("includeResultsetResponses", body["includeResultsetResponses"])
        return params
    return dict(query)


def _v1_params(query: dict[str, Any]) -> dict[str, Any]:
    """Map a flat query or a v2 request entity onto v1 BeaconAlleleRequest parameters.

    The coordinate fields share names and 0-based semantics across v1 and v2. What differs:
    v2 expresses a position as an array, one element for an exact position and two for a fuzzy
    range, which v1 spells ``start`` and ``startMin``/``startMax`` (likewise ``end``); and v2's
    ``includeResultsetResponses`` is v1's ``includeDatasetResponses`` (same HIT/MISS/ALL/NONE
    values).
    """
    params = _request_parameters(query)
    for name in ("start", "end"):
        value = params.get(name)
        if isinstance(value, list):
            if len(value) == 1:
                params[name] = value[0]
            elif len(value) == 2:
                del params[name]
                params[f"{name}Min"], params[f"{name}Max"] = value
            else:
                raise ValueError(f"Beacon v1 {name} needs one position or a two-position range")
    if "includeResultsetResponses" in params:
        params.setdefault("includeDatasetResponses", params.pop("includeResultsetResponses"))
    missing = [field for field in _V1_REQUIRED if params.get(field) in (None, "")]
    if missing:
        raise ValueError(f"Beacon v1 query requires {', '.join(missing)}")
    return params


# ---- queryShape: a registry record's declaration of how its Beacon is asked and answers.
#
# {
#   "method": "GET" | "POST",            POST sends the parameters as a form-encoded body
#   "path": "/query",                     appended to the service URL; "" queries the URL itself
#   "parameters": {"chromosome": "{referenceName}", "position": "{start}", "dataset": "lovd"},
#   "positions": "0-based" | "1-based",   the service's coordinate base
#   "chromosome": "bare" | "chr",         11 or chr11
#   "assemblies": {"GRCh37": "hg19"},     assemblyId values accepted, and the value sent for each
#   "answer": {"format": "json", "exists": "response.exists"}
#           | {"format": "text", "found": "Yes", "notFound": "No"},
#   "matchesOn": "allele" | "position"    what a positive answer establishes
# }
#
# Parameter values are literals or one of the placeholders below, which carry v1/v2 names and
# v1/v2 meaning: callers always send 0-based positions and either chromosome form, and the
# adapter converts to what the shape declares.

_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")
_PLACEHOLDERS = {"referenceName", "start", "referenceBases", "alternateBases", "assemblyId"}
_SAFE_PATH = re.compile(r"^(/[A-Za-z0-9._~!$&'()*+,;=:@-]+)*/?$")
_RESPONSE_EXCERPT_CHARS = 2000


def query_shape(service: ServiceDescriptor) -> dict[str, Any] | None:
    shape = service.raw.get("queryShape")
    if shape is None:
        return None
    if not isinstance(shape, dict):
        raise BeaconVersionError("the registry record's queryShape is not an object")
    return shape


def _shape_url(service: ServiceDescriptor, shape: dict[str, Any]) -> str:
    path = shape.get("path") or ""
    # The shape comes from the registry record, like the URL itself. It may extend the service
    # URL's path but never leave it: no host, query string, fragment, or dot segment.
    if not isinstance(path, str) or not _SAFE_PATH.match(path) or any(
        part in {".", ".."} for part in path.split("/")
    ):
        raise BeaconVersionError(f"queryShape path {path!r} is not a relative path")
    return str(service.url).rstrip("/") + path


def _one_position(value: Any) -> int:
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError("this Beacon answers exact positions only, not a range")
        value = value[0]
    if isinstance(value, bool):
        raise ValueError("start must be an integer position")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("start must be an integer position") from None


def _shape_values(shape: dict[str, Any], params: dict[str, Any]) -> dict[str, str]:
    """Placeholder values for one query, converted to the service's conventions."""
    values: dict[str, str] = {}
    chromosome = params.get("referenceName")
    if chromosome not in (None, ""):
        bare = re.sub(r"^chr", "", str(chromosome), flags=re.IGNORECASE)
        values["referenceName"] = "chr" + bare if shape.get("chromosome") == "chr" else bare
    if params.get("start") not in (None, ""):
        start = _one_position(params["start"])
        positions = shape.get("positions", "0-based")
        if positions not in {"0-based", "1-based"}:
            raise BeaconVersionError(f"queryShape positions {positions!r} is not recognised")
        values["start"] = str(start + 1 if positions == "1-based" else start)
    for name in ("referenceBases", "alternateBases"):
        if params.get(name) not in (None, ""):
            values[name] = str(params[name])
    assemblies = shape.get("assemblies")
    assembly = params.get("assemblyId")
    if isinstance(assemblies, dict) and assemblies:
        if assembly in (None, ""):
            raise ValueError(f"assemblyId is required; this Beacon holds {', '.join(assemblies)}")
        match = next(
            (value for key, value in assemblies.items() if key.lower() == str(assembly).lower()),
            None,
        )
        if match is None:
            raise ValueError(
                f"this Beacon does not hold assembly {assembly!r}; it holds {', '.join(assemblies)}"
            )
        values["assemblyId"] = str(match)
    elif assembly not in (None, ""):
        values["assemblyId"] = str(assembly)
    return values


def _shape_parameters(shape: dict[str, Any], values: dict[str, str]) -> dict[str, str]:
    template = shape.get("parameters") or {}
    if not isinstance(template, dict):
        raise BeaconVersionError("queryShape parameters is not an object")
    sent: dict[str, str] = {}
    missing: list[str] = []
    for key, value in template.items():
        placeholder = _PLACEHOLDER.match(str(value))
        if not placeholder:
            sent[str(key)] = str(value)
            continue
        name = placeholder.group(1)
        if name not in _PLACEHOLDERS:
            raise BeaconVersionError(f"queryShape placeholder {{{name}}} is not recognised")
        if name not in values:
            missing.append(name)
        else:
            sent[str(key)] = values[name]
    if missing:
        raise ValueError(f"this Beacon's query requires {', '.join(missing)}")
    return sent


def _field(document: Any, path: str) -> Any:
    value = document
    for part in [part for part in path.split(".") if part]:
        if not isinstance(value, dict) or part not in value:
            raise KeyError(part)
        value = value[part]
    return value


def _invalid(result: HttpResult, message: str) -> AdapterError:
    return AdapterError(
        message,
        HttpResult(url=result.url, status=result.status, error_kind="invalid_response"),
    )


def _answer(shape: dict[str, Any]) -> dict[str, Any]:
    """The shape's answer declaration, checked before any request is sent."""
    answer = dict(shape.get("answer") or {})
    answer.setdefault("format", "json")
    if answer["format"] == "json":
        if not isinstance(answer.get("exists", ""), str):
            raise BeaconVersionError("queryShape answer exists must be a field path")
        return answer
    if answer["format"] == "text":
        found, not_found = answer.get("found"), answer.get("notFound")
        if not (isinstance(found, str) and found and isinstance(not_found, str) and not_found):
            raise BeaconVersionError("queryShape text answers need found and notFound strings")
        return answer
    raise BeaconVersionError(f"queryShape answer format {answer['format']!r} is not recognised")


def _shape_answer(shape: dict[str, Any], result: HttpResult) -> tuple[bool | None, Any]:
    """(exists, native response) read as the shape's answer declares. None means no answer."""
    if not result.ok:
        raise AdapterError(result.error or f"upstream returned HTTP {result.status}", result)
    answer = _answer(shape)
    if answer["format"] == "json":
        if result.json is None:
            raise AdapterError("upstream returned a non-JSON response", result)
        try:
            value = _field(result.json, str(answer.get("exists") or ""))
        except KeyError:
            raise _invalid(result, "Beacon answer has no exists field") from None
        if isinstance(value, bool):
            return value, result.json
        if value is None:
            return None, result.json
        if isinstance(value, list):
            return bool(value), result.json
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "null"}:
            token = value.strip().lower()
            return (None if token == "null" else token == "true"), result.json
        raise _invalid(result, "Beacon exists field is not a boolean answer")
    text = result.text if result.text is not None else str(result.json)
    # Literal substrings, not patterns: the strings come from a registry record.
    excerpt = text.strip()[:_RESPONSE_EXCERPT_CHARS]
    if answer["found"] in text:
        return True, excerpt
    if answer["notFound"] in text:
        return False, excerpt
    raise _invalid(result, "Beacon answer matches neither its found nor its notFound text")


class BeaconAdapter(BaseAdapter):
    async def query_variant(
        self,
        service: ServiceDescriptor,
        query: dict[str, Any],
        credential: OutboundCredential,
        *,
        entry_type: str = "g_variants",
    ) -> dict[str, Any]:
        shape = query_shape(service)
        if shape is not None:
            return await self._query_shape(service, shape, query, credential, entry_type)
        version = beacon_version(service)
        major = version[0] if version else None
        if major == 1 or (major == 0 and version and version[1] >= _V1_COMPATIBLE_MINOR):
            return await self._query_v1(service, query, credential, entry_type=entry_type)
        if major == 0:
            raise BeaconVersionError(
                f"Beacon {service.standard_version} has no standard query form; "
                "its registry record must declare a queryShape"
            )
        if major not in (None, 2):
            raise BeaconVersionError(
                f"Beacon {service.standard_version} is not supported; "
                "the Harness speaks Beacon pre-1.0, v1 and v2"
            )
        # Beacon permits both query-parameter GETs and request-entity POSTs. A flat mapping is
        # the portable GET form used by several public Beacons; structured request entities use
        # POST so their nested shape is preserved.
        structured = "query" in query or "meta" in query
        result = await self._http.request(
            "POST" if structured else "GET",
            str(service.url).rstrip("/") + f"/{segment(entry_type)}",
            credential=credential,
            json_body=query if structured else None,
            params=None if structured else query,
        )
        data = require_json(result)
        if not isinstance(data, dict):
            raise ValueError("Beacon response must be an object")
        return data

    async def _query_v1(
        self,
        service: ServiceDescriptor,
        query: dict[str, Any],
        credential: OutboundCredential,
        *,
        entry_type: str,
    ) -> dict[str, Any]:
        if entry_type != "g_variants":
            raise BeaconVersionError(
                f"Beacon v1 answers allele queries only (entry_type g_variants), not {entry_type!r}"
            )
        result = await self._http.request(
            "GET",
            str(service.url).rstrip("/") + "/query",
            credential=credential,
            params=_v1_params(query),
        )
        data = require_json(result)
        if not isinstance(data, dict):
            raise ValueError("Beacon response must be an object")
        # v1 returns a BeaconAlleleResponse: beaconId, apiVersion, exists, datasetAlleleResponses.
        return data

    async def _query_shape(
        self,
        service: ServiceDescriptor,
        shape: dict[str, Any],
        query: dict[str, Any],
        credential: OutboundCredential,
        entry_type: str,
    ) -> dict[str, Any]:
        if entry_type != "g_variants":
            raise BeaconVersionError(
                "this Beacon answers allele queries only (entry_type g_variants), "
                f"not {entry_type!r}"
            )
        method = str(shape.get("method") or "GET").upper()
        if method not in {"GET", "POST"}:
            raise BeaconVersionError(f"queryShape method {method!r} is not GET or POST")
        matches_on = shape.get("matchesOn", "allele")
        if matches_on not in {"allele", "position"}:
            raise BeaconVersionError(f"queryShape matchesOn {matches_on!r} is not recognised")
        _answer(shape)
        url = _shape_url(service, shape)
        parameters = _shape_parameters(shape, _shape_values(shape, _request_parameters(query)))
        result = await self._http.request(
            method,
            url,
            credential=credential,
            headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"},
            params=parameters if method == "GET" else None,
            data=parameters if method == "POST" else None,
        )
        exists, native = _shape_answer(shape, result)
        # The same top-level exists a v1 BeaconAlleleResponse carries, with the exact request
        # sent and the service's own answer kept beside it as evidence.
        return {
            "exists": exists,
            "apiVersion": service.standard_version,
            "matchesOn": matches_on,
            "request": {"method": method, "url": url, "parameters": parameters},
            "response": native,
        }
