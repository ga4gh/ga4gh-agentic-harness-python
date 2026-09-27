"""GA4GH Implementation Registry discovery and normalization."""

from __future__ import annotations

import time
from typing import Any

from .http import SafeHttpClient
from .models import ServiceDescriptor
from .settings import Settings


def normalize_service(item: dict[str, Any]) -> ServiceDescriptor:
    standard = item.get("standardVersion") or {}
    service_type = item.get("type") or {}
    organization = item.get("organisation") or item.get("organization") or {}
    url = item.get("url")
    if not url:
        service_info_url = item.get("serviceInfoUrl") or ""
        url = service_info_url.removesuffix("/service-info")
    if not url:
        raise ValueError("registry service has no URL")
    return ServiceDescriptor(
        id=str(item.get("id") or item.get("implementationId")),
        implementation_id=item.get("implementationId") or item.get("id"),
        name=item.get("name"),
        product=(
            standard.get("ga4ghProduct")
            or item.get("product")
            or (service_type.get("artifact") if isinstance(service_type, dict) else None)
        ),
        standard_version=(
            standard.get("version")
            or (service_type.get("version") if isinstance(service_type, dict) else None)
            or item.get("version")
        ),
        url=url,
        service_info_url=item.get("serviceInfoUrl"),
        organization=organization.get("name") if isinstance(organization, dict) else None,
        environment=item.get("environment"),
        source=item.get("source") or "ga4gh-implementation-registry",
        raw=item,
    )


class RegistryError(RuntimeError):
    pass


class ServiceRegistry:
    def __init__(
        self,
        http: SafeHttpClient,
        settings: Settings,
        *,
        static_services: list[ServiceDescriptor] | None = None,
    ) -> None:
        self._http = http
        self._settings = settings
        self._cache: tuple[float, list[ServiceDescriptor]] | None = None
        self._static = {service.id: service for service in static_services or []}

    async def all(self, *, refresh: bool = False) -> list[ServiceDescriptor]:
        if not refresh and self._cache:
            age = time.monotonic() - self._cache[0]
            if age < self._settings.registry_cache_ttl_seconds:
                return list(self._cache[1])
        services: list[ServiceDescriptor] = []
        failures: list[str] = []
        for source in self._settings.registries:
            result = await self._http.request("GET", source.url.rstrip("/") + "/services")
            if not result.ok or not isinstance(result.json, list):
                failures.append(
                    f"{source.url}: {result.error or f'registry returned HTTP {result.status}'}"
                )
                continue
            for item in result.json:
                if not isinstance(item, dict):
                    continue
                # Implementation Registry records keep their default source label; other
                # registries are named by URL so a caller can tell where a service came from.
                if source.api == "service-registry":
                    item = {"source": source.url, **item}
                try:
                    services.append(normalize_service(item))
                except ValueError:
                    continue
        # One unreachable registry does not hide the others; all of them failing is an error.
        if len(failures) == len(self._settings.registries):
            raise RegistryError("; ".join(failures))
        merged = {service.id: service for service in services}
        merged.update(self._static)
        combined = list(merged.values())
        self._cache = (time.monotonic(), combined)
        return list(combined)

    async def search(
        self,
        *,
        query: str | None = None,
        product: str | None = None,
        organization: str | None = None,
        environment: str | None = None,
        limit: int = 50,
        cursor: int = 0,
    ) -> tuple[list[ServiceDescriptor], int | None, int]:
        services = await self.all()

        def matches(service: ServiceDescriptor) -> bool:
            if product and (service.product or "").lower() != product.lower():
                return False
            if organization and organization.lower() not in (service.organization or "").lower():
                return False
            if environment and (service.environment or "").lower() != environment.lower():
                return False
            if query:
                text = " ".join(
                    value or ""
                    for value in (
                        service.id,
                        service.implementation_id,
                        service.name,
                        service.product,
                        service.organization,
                        str(service.url),
                    )
                ).lower()
                if query.lower() not in text:
                    return False
            return True

        filtered = [service for service in services if matches(service)]
        end = cursor + max(1, min(limit, 200))
        return filtered[cursor:end], end if end < len(filtered) else None, len(filtered)

    async def get(self, service_id: str) -> ServiceDescriptor:
        direct = self._static.get(service_id)
        if direct:
            return direct
        for service in await self.all():
            if service.id == service_id or service.implementation_id == service_id:
                return service
        raise KeyError(service_id)

    def invalidate(self) -> None:
        self._cache = None
