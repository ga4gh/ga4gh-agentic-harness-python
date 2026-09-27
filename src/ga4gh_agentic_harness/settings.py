"""Runtime settings with conservative transport defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RegistrySource(BaseModel):
    """One registry the Harness discovers services from.

    ``api`` says which API the URL speaks; it is declared, never probed. Both answer
    ``GET {url}/services``: the GA4GH Implementation Registry with its registry records, and
    any GA4GH Service Registry with service-info objects.
    """

    url: str
    api: Literal["implementation-registry", "service-registry"]


GA4GH_IMPLEMENTATION_REGISTRY = RegistrySource(
    url="https://implementation-registry.ga4gh.org/api", api="implementation-registry"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GA4GH_HARNESS_", extra="ignore")

    profile_version: str = "0.1.0"
    # Every registry that feeds discovery, merged in order (later entries win on id clashes).
    # Setting the list replaces the default, so a deployment can omit the public registry.
    # Environment: GA4GH_HARNESS_REGISTRIES='[{"url": "...", "api": "service-registry"}]'.
    registries: list[RegistrySource] = Field(
        default_factory=lambda: [GA4GH_IMPLEMENTATION_REGISTRY.model_copy()]
    )
    registry_cache_ttl_seconds: float = 300.0
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    retry_backoff_seconds: float = 0.25
    max_retries: int = 2
    max_redirects: int = 3
    max_response_bytes: int = 5_000_000
    verify_tls: bool = True
    allow_http: bool = False
    allow_private_hosts: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)
    user_agent: str = "ga4gh-agentic-harness/0.1"
    ledger_path: Path = Path(".ga4gh-harness-runs.db")
    trace_path: Path | None = None

    @field_validator("registries")
    @classmethod
    def _at_least_one_registry(cls, value: list[RegistrySource]) -> list[RegistrySource]:
        if not value:
            raise ValueError("at least one registry is required")
        return value
