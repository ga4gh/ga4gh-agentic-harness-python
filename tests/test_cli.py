from __future__ import annotations

from ga4gh_agentic_harness.cli import build_parser


def test_local_transport_options_are_explicit() -> None:
    args = build_parser().parse_args(
        [
            "ga4gh.service.search",
            "--registry",
            "service-registry=http://127.0.0.1:18080/ga4gh/registry",
            "--allow-http",
            "--allow-private-hosts",
        ]
    )
    assert args.registry == ["service-registry=http://127.0.0.1:18080/ga4gh/registry"]
    assert args.allow_http is True
    assert args.allow_private_hosts is True


def test_registry_flags_become_the_registry_list() -> None:
    from ga4gh_agentic_harness.cli import _registry
    from ga4gh_agentic_harness.settings import RegistrySource

    parsed = _registry("service-registry=http://127.0.0.1:18090/ga4gh/registry")
    assert parsed == RegistrySource(
        url="http://127.0.0.1:18090/ga4gh/registry", api="service-registry"
    )
    for bad in ("http://no-api.test", "unknown-api=http://x.test"):
        try:
            _registry(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} was accepted")
