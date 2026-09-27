"""Canonical, protocol-neutral GA4GH Agentic Harness operations."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

from .adapters import BeaconAdapter, DrsAdapter, TrsAdapter, WesAdapter
from .adapters.base import AdapterError
from .adapters.beacon import BeaconVersionError
from .auth import (
    AuthorityContext,
    CredentialProvider,
    CredentialRequest,
    OutboundCredential,
    PublicCredentialProvider,
)
from .conformance import TEST_PACK, ConformanceAssessor
from .http import HttpResult, SafeHttpClient, UnsafeUrlError
from .ledger import WorkflowRunLedger
from .models import (
    AuthRequirement,
    Capability,
    CapabilityDescriptor,
    ErrorCode,
    HarnessError,
    Operation,
    Page,
    ProbeResult,
    ProbeStatus,
    ResultEnvelope,
    ResultStatus,
    ServiceDescriptor,
    Warning,
)
from .policy import DefaultPolicyEvaluator, PolicyEvaluator, PolicyRequest
from .registry import RegistryError, ServiceRegistry
from .settings import Settings
from .tracing import JsonLinesTraceSink, NullTraceSink, TraceEvent, TraceSink

R = TypeVar("R")


_CAPABILITIES: dict[str, list[tuple[Operation, str, bool]]] = {
    "DRS": [
        (Operation.DRS_OBJECT_RESOLVE, "Resolve a DRS object by identifier.", False),
        (Operation.DRS_ACCESS_RESOLVE, "Resolve an access method for a DRS object.", False),
    ],
    "TRS": [
        (Operation.TRS_WORKFLOW_RESOLVE, "Resolve a versioned workflow from TRS.", False),
    ],
    "BEACON": [
        (
            Operation.BEACON_VARIANT_QUERY,
            "Query a Beacon service (pre-1.0, v1 or v2) for a genomic variant.",
            False,
        ),
    ],
    "WES": [
        (Operation.WES_SERVICE_DESCRIBE, "Describe WES capabilities and limits.", False),
        (Operation.WES_RUN_SUBMIT, "Submit a workflow run to WES.", True),
        (Operation.WES_RUN_GET, "Retrieve a WES workflow run.", False),
        (Operation.WES_RUN_CANCEL, "Cancel a WES workflow run.", True),
    ],
}

_OPERATION_METADATA: dict[Operation, tuple[str, str, str, str]] = {
    Operation.HARNESS_DESCRIBE: (
        "Describe this Harness and its capabilities.", "none", "public", "ga4gh:discover"
    ),
    Operation.SERVICE_SEARCH: (
        "Search declarations of GA4GH services.", "none", "public", "ga4gh:discover"
    ),
    Operation.SERVICE_DESCRIBE: (
        "Describe a declared service using registry and native metadata.",
        "none", "public", "ga4gh:discover",
    ),
    Operation.SERVICE_PROBE: (
        "Perform bounded reachability and compatibility observations.",
        "observation", "target-defined", "ga4gh:discover",
    ),
    Operation.TRS_WORKFLOW_RESOLVE: (
        "Resolve a workflow and descriptor through TRS.",
        "none", "target-defined", "ga4gh:data:read",
    ),
    Operation.DRS_OBJECT_RESOLVE: (
        "Resolve a DRS object by identifier or DRS URI.",
        "none", "target-defined", "ga4gh:data:read",
    ),
    Operation.DRS_ACCESS_RESOLVE: (
        "Resolve a DRS access method as a protected capability.",
        "capability-read", "capability-sensitive", "ga4gh:data:read",
    ),
    Operation.BEACON_VARIANT_QUERY: (
        "Perform a structured Beacon variant discovery query.",
        "none", "target-defined", "ga4gh:data:read",
    ),
    Operation.WES_SERVICE_DESCRIBE: (
        "Describe WES workflow execution capabilities.",
        "none", "target-defined", "ga4gh:workflow:read",
    ),
    Operation.WES_RUN_SUBMIT: (
        "Submit a workflow run to WES.",
        "creates-resource", "controlled", "ga4gh:workflow:submit",
    ),
    Operation.WES_RUN_GET: (
        "Retrieve WES run status or logs.",
        "none", "controlled", "ga4gh:workflow:read",
    ),
    Operation.WES_RUN_CANCEL: (
        "Request cancellation of a WES run.",
        "changes-resource", "controlled", "ga4gh:workflow:cancel",
    ),
    Operation.CONFORMANCE_ASSESS: (
        "Run a named deterministic conformance test pack.",
        "test-pack-defined", "target-defined", "ga4gh:conformance:assess",
    ),
}


class _OperationFailure(Exception):
    """Carry a fully formed Harness error out of an operation action."""

    def __init__(self, error: HarnessError) -> None:
        super().__init__(error.message)
        self.error = error


def _default_authority() -> AuthorityContext:
    return AuthorityContext(software_actor="local-sdk")


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _authority_key(authority: AuthorityContext) -> str:
    """Build a non-secret ledger partition key for the effective caller."""
    return "|".join(
        (
            authority.tenant or "-",
            authority.human_principal or "-",
            authority.software_actor,
        )
    )


def _service_info_url(service: ServiceDescriptor) -> str:
    if service.service_info_url:
        return str(service.service_info_url)
    return str(service.url).rstrip("/") + "/service-info"


class Harness:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http: SafeHttpClient | None = None,
        registry: ServiceRegistry | None = None,
        credentials: CredentialProvider | None = None,
        policy: PolicyEvaluator | None = None,
        traces: TraceSink | None = None,
        ledger: WorkflowRunLedger | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.http = http or SafeHttpClient(self.settings)
        self.registry = registry or ServiceRegistry(self.http, self.settings)
        self.credentials = credentials or PublicCredentialProvider()
        self.policy = policy or DefaultPolicyEvaluator()
        self.traces = traces or (
            JsonLinesTraceSink(self.settings.trace_path)
            if self.settings.trace_path
            else NullTraceSink()
        )
        self.ledger = ledger or WorkflowRunLedger(self.settings.ledger_path)
        self.drs = DrsAdapter(self.http)
        self.trs = TrsAdapter(self.http)
        self.beacon = BeaconAdapter(self.http)
        self.wes = WesAdapter(self.http)
        self.conformance = ConformanceAssessor()

    async def __aenter__(self) -> Harness:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _execute(
        self,
        operation: Operation,
        action: Callable[[OutboundCredential], Awaitable[R]],
        *,
        authority: AuthorityContext | None = None,
        service: ServiceDescriptor | None = None,
        side_effects: bool = False,
        request_id: str | None = None,
        trace_id: str | None = None,
        warnings: list[Warning] | None = None,
        page: Page | None = None,
    ) -> ResultEnvelope[R]:
        authority = authority or _default_authority()
        request_id = request_id or str(uuid.uuid4())
        trace_id = trace_id or str(uuid.uuid4())
        target = _origin(str(service.url)) if service else None
        await self.traces.emit(
            TraceEvent(
                trace_id=trace_id,
                request_id=request_id,
                phase="started",
                operation=operation,
                authority=authority,
                service_id=service.id if service else None,
                target_origin=target,
            )
        )
        decision = await self.policy.evaluate(
            PolicyRequest(
                authority=authority,
                operation=operation,
                service=service,
                side_effects=side_effects,
            )
        )
        if not decision.allowed:
            return await self._failure(
                operation,
                request_id,
                trace_id,
                authority,
                HarnessError(code=ErrorCode.POLICY_DENIED, message=decision.reason),
                service=service,
                policy_decision=decision.reason,
            )
        if decision.approval_required:
            # A permit that still needs approval is not permission to act. The evaluator
            # clears approval_required only once it holds an approval bound to this request.
            return await self._failure(
                operation,
                request_id,
                trace_id,
                authority,
                HarnessError(code=ErrorCode.APPROVAL_REQUIRED, message=decision.reason),
                service=service,
                policy_decision=decision.reason,
            )
        try:
            credential = OutboundCredential()
            credential_metadata: dict[str, str] = {}
            if service:
                resource = str(service.url).rstrip("/")
                credential = await self.credentials.acquire(
                    CredentialRequest(
                        service=service,
                        operation=operation,
                        resource=resource,
                        scopes=[operation.value],
                        authority=authority,
                    )
                )
                # A provider that does not state its resource is bound to the one requested,
                # so registry-supplied URLs on other origins never receive the credential.
                if credential.resource is None:
                    credential.resource = resource
                credential_metadata = credential.audit_metadata()
            data = await action(credential)
        except Exception as exc:  # operation errors are always converted to envelopes
            return await self._failure(
                operation,
                request_id,
                trace_id,
                authority,
                self._error_from_exception(exc),
                service=service,
                policy_decision=decision.reason,
            )
        await self.traces.emit(
            TraceEvent(
                trace_id=trace_id,
                request_id=request_id,
                phase="completed",
                operation=operation,
                authority=authority,
                service_id=service.id if service else None,
                target_origin=target,
                policy_decision=decision.reason,
                status=ResultStatus.SUCCESS,
                metadata={"credential": credential_metadata},
            )
        )
        return ResultEnvelope[R](
            profile_version=self.settings.profile_version,
            operation=operation,
            request_id=request_id,
            status=ResultStatus.SUCCESS,
            data=data,
            warnings=warnings or [],
            trace_id=trace_id,
            page=page,
        )

    async def _failure(
        self,
        operation: Operation,
        request_id: str,
        trace_id: str,
        authority: AuthorityContext,
        error: HarnessError,
        *,
        service: ServiceDescriptor | None = None,
        policy_decision: str | None = None,
    ) -> ResultEnvelope[Any]:
        await self.traces.emit(
            TraceEvent(
                trace_id=trace_id,
                request_id=request_id,
                phase="completed",
                operation=operation,
                authority=authority,
                service_id=service.id if service else None,
                target_origin=_origin(str(service.url)) if service else None,
                policy_decision=policy_decision,
                status=ResultStatus.FAILURE,
                metadata={"error_code": error.code.value},
            )
        )
        return ResultEnvelope[Any](
            profile_version=self.settings.profile_version,
            operation=operation,
            request_id=request_id,
            status=ResultStatus.FAILURE,
            errors=[error],
            trace_id=trace_id,
        )

    @staticmethod
    def _error_from_exception(exc: Exception) -> HarnessError:
        if isinstance(exc, _OperationFailure):
            return exc.error
        if isinstance(exc, AdapterError):
            result = exc.result
            status = result.status if result else None
            if status in {401, 403}:
                code = ErrorCode.NOT_AUTHORIZED
            elif status == 404:
                code = ErrorCode.NOT_FOUND
            elif result and result.error_kind == "timeout":
                code = ErrorCode.TIMEOUT
            elif result and result.error_kind == "security":
                code = ErrorCode.SECURITY
            elif result and result.error_kind == "invalid_response":
                code = ErrorCode.INVALID_RESPONSE
            else:
                code = ErrorCode.UPSTREAM
            return HarnessError(
                code=code,
                message=str(exc),
                retryable=bool(status in {429, 500, 502, 503, 504}),
                native_status=status,
            )
        if isinstance(exc, BeaconVersionError):
            return HarnessError(code=ErrorCode.UNSUPPORTED, message=str(exc))
        if isinstance(exc, (UnsafeUrlError, PermissionError)):
            code = (
                ErrorCode.SECURITY
                if isinstance(exc, UnsafeUrlError)
                else ErrorCode.NOT_AUTHORIZED
            )
            return HarnessError(code=code, message=str(exc))
        if isinstance(exc, KeyError):
            return HarnessError(
                code=ErrorCode.NOT_FOUND,
                message=f"resource not found: {exc.args[0]}",
            )
        if isinstance(exc, RegistryError):
            return HarnessError(code=ErrorCode.UPSTREAM, message=str(exc), retryable=True)
        if isinstance(exc, ValueError):
            return HarnessError(code=ErrorCode.INVALID_REQUEST, message=str(exc))
        return HarnessError(code=ErrorCode.INTERNAL, message=f"{type(exc).__name__}: {exc}")

    async def harness_describe(
        self, *, authority: AuthorityContext | None = None
    ) -> ResultEnvelope[dict[str, Any]]:
        async def action(_: OutboundCredential) -> dict[str, Any]:
            capabilities = []
            schema_base = (
                "https://w3id.org/ga4gh/agentic-harness/0.1/schemas/operations/"
            )
            for operation, (purpose, side_effect, sensitivity, scope) in (
                _OPERATION_METADATA.items()
            ):
                schema_name = operation.value.removeprefix("ga4gh.").replace(".", "-")
                capabilities.append(
                    {
                        "operation": operation.value,
                        "purpose": purpose,
                        "input_schema": f"{schema_base}{schema_name}.schema.json",
                        "side_effect": side_effect,
                        "data_sensitivity": sensitivity,
                        "authentication": {
                            "required": operation
                            in {Operation.WES_RUN_SUBMIT, Operation.WES_RUN_CANCEL},
                            "scopes": [scope],
                            "delegation_supported": False,
                        },
                        "bindings": [
                            {
                                "name": "python",
                                "operation": operation.value.replace(".", "_"),
                            }
                        ],
                        "evidence": {"state": "declared"},
                    }
                )
            return {
                "profile_version": self.settings.profile_version,
                "harness": {
                    "id": "org.ga4gh.agentic-harness.python",
                    "name": "GA4GH Agentic Harness Python",
                    "version": "0.1.0.dev0",
                },
                "bindings": [{"name": "python", "version": "0.1.0"}],
                "capabilities": capabilities,
                "conformance": [
                    {"class": name, "status": "not-assessed"}
                    for name in (
                        "level-0-describe",
                        "level-1-discover-access",
                        "level-2-execute",
                        "assessment-extension",
                        "protected-resource-extension",
                    )
                ],
            }

        return await self._execute(Operation.HARNESS_DESCRIBE, action, authority=authority)

    async def service_search(
        self,
        *,
        query: str | None = None,
        product: str | None = None,
        organization: str | None = None,
        environment: str | None = None,
        limit: int = 50,
        cursor: int = 0,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[list[ServiceDescriptor]]:
        page = Page(returned=0)

        async def action(_: OutboundCredential) -> list[ServiceDescriptor]:
            services, next_cursor, total = await self.registry.search(
                query=query,
                product=product,
                organization=organization,
                environment=environment,
                limit=limit,
                cursor=cursor,
            )
            page.returned = len(services)
            page.next_page_token = str(next_cursor) if next_cursor is not None else None
            page.total = total
            return services

        return await self._execute(
            Operation.SERVICE_SEARCH, action, authority=authority, page=page
        )

    async def service_describe(
        self, service_id: str, *, authority: AuthorityContext | None = None
    ) -> ResultEnvelope[dict[str, Any]]:
        try:
            service = await self.registry.get(service_id)
        except Exception as exc:
            return await self._preflight_failure(Operation.SERVICE_DESCRIBE, authority, exc)

        async def action(_: OutboundCredential) -> dict[str, Any]:
            return {
                "service": service.model_dump(mode="json", exclude_none=True),
                "capability_descriptor": self._capability_descriptor(service).model_dump(
                    mode="json", exclude_none=True
                ),
            }

        return await self._execute(
            Operation.SERVICE_DESCRIBE, action, authority=authority, service=service
        )

    def _capability_descriptor(self, service: ServiceDescriptor) -> CapabilityDescriptor:
        capabilities = []
        for operation, purpose, side_effects in _CAPABILITIES.get(
            (service.product or "").upper(), []
        ):
            capabilities.append(
                Capability(
                    operation=operation,
                    purpose=purpose,
                    side_effects=side_effects,
                    underlying_standard=service.product or "unknown",
                    standard_version=service.standard_version,
                )
            )
        return CapabilityDescriptor(
            profile_version=self.settings.profile_version,
            service=service,
            capabilities=capabilities,
        )

    async def service_probe(
        self, service_id: str, *, authority: AuthorityContext | None = None
    ) -> ResultEnvelope[ProbeResult]:
        try:
            service = await self.registry.get(service_id)
        except Exception as exc:
            return await self._preflight_failure(Operation.SERVICE_PROBE, authority, exc)

        async def action(credential: OutboundCredential) -> ProbeResult:
            url = _service_info_url(service)
            result = await self.http.request("GET", url, credential=credential)
            return self._probe_result(service, result)

        return await self._execute(
            Operation.SERVICE_PROBE, action, authority=authority, service=service
        )

    @staticmethod
    def _probe_result(service: ServiceDescriptor, result: HttpResult) -> ProbeResult:
        if result.status in {401, 403}:
            status = ProbeStatus.AUTH_REQUIRED
        elif result.error_kind == "timeout":
            status = ProbeStatus.TIMEOUT
        elif result.status is None:
            status = ProbeStatus.UNREACHABLE
        elif result.status >= 400:
            status = ProbeStatus.HTTP_ERROR
        elif not isinstance(result.json, dict):
            status = ProbeStatus.INVALID_RESPONSE
        else:
            status = ProbeStatus.LIVE
        challenge = result.headers.get("www-authenticate", "")
        scheme = challenge.split(maxsplit=1)[0] if challenge else None
        return ProbeResult(
            service=service,
            status=status,
            probed_url=result.url,
            http_status=result.status,
            latency_ms=result.latency_ms,
            service_info=result.json if isinstance(result.json, dict) else None,
            auth=AuthRequirement(
                required=status == ProbeStatus.AUTH_REQUIRED,
                schemes=[scheme] if scheme else [],
            ),
        )

    async def trs_workflow_resolve(
        self,
        service_id: str,
        tool_id: str,
        *,
        version: str | None = None,
        descriptor_type: str | None = None,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        return await self._service_operation(
            Operation.TRS_WORKFLOW_RESOLVE,
            service_id,
            lambda service, credential: self.trs.resolve_workflow(
                service,
                tool_id,
                credential,
                version=version,
                descriptor_type=descriptor_type,
            ),
            authority=authority,
        )

    async def drs_object_resolve(
        self,
        service_id: str,
        object_id: str,
        *,
        expand: bool = False,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        return await self._service_operation(
            Operation.DRS_OBJECT_RESOLVE,
            service_id,
            lambda service, credential: self.drs.resolve_object(
                service, object_id, credential, expand=expand
            ),
            authority=authority,
        )

    async def drs_access_resolve(
        self,
        service_id: str,
        object_id: str,
        access_id: str,
        *,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        return await self._service_operation(
            Operation.DRS_ACCESS_RESOLVE,
            service_id,
            lambda service, credential: self.drs.resolve_access(
                service, object_id, access_id, credential
            ),
            authority=authority,
        )

    async def beacon_variant_query(
        self,
        service_id: str,
        query: dict[str, Any],
        *,
        entry_type: str = "g_variants",
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        warning = Warning(
            code="NOT_CLINICAL_INTERPRETATION",
            message=(
                "Results are evidence retrieval and must not be treated as a clinical conclusion."
            ),
        )
        envelope = await self._service_operation(
            Operation.BEACON_VARIANT_QUERY,
            service_id,
            lambda service, credential: self.beacon.query_variant(
                service, query, credential, entry_type=entry_type
            ),
            authority=authority,
            warnings=[warning],
        )
        data = envelope.data if isinstance(envelope.data, dict) else {}
        if data.get("matchesOn") == "position":
            envelope.warnings.append(
                Warning(
                    code="POSITION_MATCH_ONLY",
                    message=(
                        "This Beacon matches on position, not allele: exists means it holds a "
                        "variant at the position, not necessarily the queried allele."
                    ),
                )
            )
        request = data.get("request")
        if isinstance(request, dict) and str(request.get("url", "")).startswith("http://"):
            envelope.warnings.append(
                Warning(
                    code="PLAIN_HTTP",
                    message=(
                        "This Beacon is served over plain HTTP only, so its answer was not "
                        "protected in transit. No credential was sent."
                    ),
                )
            )
        return envelope

    async def wes_service_describe(
        self, service_id: str, *, authority: AuthorityContext | None = None
    ) -> ResultEnvelope[dict[str, Any]]:
        return await self._service_operation(
            Operation.WES_SERVICE_DESCRIBE,
            service_id,
            self.wes.describe_service,
            authority=authority,
        )

    async def wes_run_submit(
        self,
        service_id: str,
        *,
        workflow_url: str,
        workflow_type: str,
        workflow_type_version: str,
        workflow_params: dict[str, Any],
        tags: dict[str, Any] | None = None,
        workflow_engine_parameters: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        authority = authority or _default_authority()
        request_id, trace_id = str(uuid.uuid4()), str(uuid.uuid4())
        key = idempotency_key or request_id
        try:
            service = await self.registry.get(service_id)
        except Exception as exc:
            return await self._preflight_failure(
                Operation.WES_RUN_SUBMIT, authority, exc, request_id=request_id, trace_id=trace_id
            )
        replay_warnings: list[Warning] = []

        # The ledger record is created inside the action, after policy has permitted the
        # submission, so a denied request cannot consume the idempotency key.
        async def action(credential: OutboundCredential) -> dict[str, Any]:
            record, created = await self.ledger.create_submission(
                service_id=service.id,
                authority_key=_authority_key(authority),
                request_id=request_id,
                idempotency_key=key,
                details={"workflow_url": workflow_url, "workflow_type": workflow_type},
            )
            if not created and record.remote_run_id:
                replay_warnings.append(
                    Warning(
                        code="IDEMPOTENT_REPLAY",
                        message="Returned the prior submission for this idempotency key.",
                    )
                )
                return record.model_dump()
            if not created:
                raise _OperationFailure(
                    HarnessError(
                        code=ErrorCode.UPSTREAM,
                        message=(
                            "a prior submission with this idempotency key has no confirmed "
                            "run_id; reconcile it before attempting another submission"
                        ),
                        retryable=False,
                        details={"local_run_id": record.local_run_id, "state": record.state},
                    )
                )
            try:
                result = await self.wes.submit(
                    service,
                    credential,
                    workflow_url=workflow_url,
                    workflow_type=workflow_type,
                    workflow_type_version=workflow_type_version,
                    workflow_params=workflow_params,
                    tags=tags,
                    workflow_engine_parameters=workflow_engine_parameters,
                    idempotency_key=key,
                )
            except AdapterError as exc:
                unknown = exc.result is not None and exc.result.status is None
                await self.ledger.update(
                    record.local_run_id,
                    state="submission_unknown" if unknown else "submission_failed",
                )
                raise
            updated = await self.ledger.update(
                record.local_run_id,
                state="submitted",
                remote_run_id=str(result["run_id"]),
            )
            return {"run": result, "ledger": updated.model_dump()}

        return await self._execute(
            Operation.WES_RUN_SUBMIT,
            action,
            authority=authority,
            service=service,
            side_effects=True,
            request_id=request_id,
            trace_id=trace_id,
            warnings=replay_warnings,
        )

    async def wes_run_get(
        self,
        service_id: str,
        run_id: str,
        *,
        local_run_id: str | None = None,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        async def call(
            service: ServiceDescriptor, credential: OutboundCredential
        ) -> dict[str, Any]:
            if local_run_id:
                await self._owned_ledger_record(local_run_id, service, authority, run_id)
            result = await self.wes.get_run(service, run_id, credential)
            if local_run_id:
                state = str(result.get("state") or "unknown").lower()
                await self.ledger.update(local_run_id, state=state, remote_run_id=run_id)
            return result

        return await self._service_operation(
            Operation.WES_RUN_GET, service_id, call, authority=authority
        )

    async def wes_run_cancel(
        self,
        service_id: str,
        run_id: str,
        *,
        local_run_id: str | None = None,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        async def call(
            service: ServiceDescriptor, credential: OutboundCredential
        ) -> dict[str, Any]:
            if local_run_id:
                await self._owned_ledger_record(local_run_id, service, authority, run_id)
            result = await self.wes.cancel(service, run_id, credential)
            if local_run_id:
                await self.ledger.update(local_run_id, state="cancelled", remote_run_id=run_id)
            return result

        return await self._service_operation(
            Operation.WES_RUN_CANCEL,
            service_id,
            call,
            authority=authority,
            side_effects=True,
        )

    async def _owned_ledger_record(
        self,
        local_run_id: str,
        service: ServiceDescriptor,
        authority: AuthorityContext | None,
        run_id: str,
    ) -> None:
        """Refuse ledger updates for records another caller, service, or run owns."""
        record = await self.ledger.get(local_run_id)
        if (
            record.service_id != service.id
            or record.authority_key != _authority_key(authority or _default_authority())
            or record.remote_run_id not in {None, run_id}
        ):
            raise PermissionError("local_run_id is not bound to this caller, service, and run")

    async def conformance_assess(
        self,
        service_id: str,
        *,
        test_pack: str = TEST_PACK,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        authority = authority or _default_authority()
        try:
            service = await self.registry.get(service_id)
        except Exception as exc:
            return await self._preflight_failure(
                Operation.CONFORMANCE_ASSESS, authority, exc
            )

        async def action(credential: OutboundCredential) -> dict[str, Any]:
            url = _service_info_url(service)
            probe = self._probe_result(
                service, await self.http.request("GET", url, credential=credential)
            )
            report = self.conformance.assess(service, probe, test_pack=test_pack)
            return report.model_dump(mode="json")

        return await self._execute(
            Operation.CONFORMANCE_ASSESS, action, authority=authority, service=service
        )

    async def _service_operation(
        self,
        operation: Operation,
        service_id: str,
        call: Callable[[ServiceDescriptor, OutboundCredential], Awaitable[R]],
        *,
        authority: AuthorityContext | None = None,
        side_effects: bool = False,
        warnings: list[Warning] | None = None,
    ) -> ResultEnvelope[R]:
        try:
            service = await self.registry.get(service_id)
        except Exception as exc:
            return await self._preflight_failure(operation, authority, exc)
        return await self._execute(
            operation,
            lambda credential: call(service, credential),
            authority=authority,
            service=service,
            side_effects=side_effects,
            warnings=warnings,
        )

    async def _preflight_failure(
        self,
        operation: Operation,
        authority: AuthorityContext | None,
        exc: Exception,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> ResultEnvelope[Any]:
        return await self._failure(
            operation,
            request_id or str(uuid.uuid4()),
            trace_id or str(uuid.uuid4()),
            authority or _default_authority(),
            self._error_from_exception(exc),
        )

    async def dispatch(
        self,
        operation: str | Operation,
        payload: dict[str, Any] | None = None,
        *,
        authority: AuthorityContext | None = None,
    ) -> ResultEnvelope[Any]:
        op = Operation(operation)
        values = dict(payload or {})
        methods: dict[Operation, Callable[..., Awaitable[ResultEnvelope[Any]]]] = {
            Operation.HARNESS_DESCRIBE: self.harness_describe,
            Operation.SERVICE_SEARCH: self.service_search,
            Operation.SERVICE_DESCRIBE: self.service_describe,
            Operation.SERVICE_PROBE: self.service_probe,
            Operation.TRS_WORKFLOW_RESOLVE: self.trs_workflow_resolve,
            Operation.DRS_OBJECT_RESOLVE: self.drs_object_resolve,
            Operation.DRS_ACCESS_RESOLVE: self.drs_access_resolve,
            Operation.BEACON_VARIANT_QUERY: self.beacon_variant_query,
            Operation.WES_SERVICE_DESCRIBE: self.wes_service_describe,
            Operation.WES_RUN_SUBMIT: self.wes_run_submit,
            Operation.WES_RUN_GET: self.wes_run_get,
            Operation.WES_RUN_CANCEL: self.wes_run_cancel,
            Operation.CONFORMANCE_ASSESS: self.conformance_assess,
        }
        return await methods[op](**values, authority=authority)
