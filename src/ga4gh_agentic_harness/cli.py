"""JSON-in/JSON-out local CLI for canonical Harness operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .auth import AuthorityContext
from .harness import Harness
from .models import Operation, ResultStatus
from .settings import RegistrySource, Settings


def _load_payload(value: str) -> dict[str, Any]:
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("input must be a JSON object")
    return parsed


def _registry(value: str) -> RegistrySource:
    api, sep, url = value.partition("=")
    if not sep or not url:
        raise ValueError(f"--registry must be API=URL, got {value!r}")
    return RegistrySource.model_validate({"url": url, "api": api})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ga4gh-harness",
        description="Invoke protocol-neutral GA4GH Agentic Harness operations.",
    )
    parser.add_argument("operation", choices=[operation.value for operation in Operation])
    parser.add_argument("--input", default="{}", help="JSON object or @path/to/input.json")
    parser.add_argument("--actor", default="local-cli", help="Authenticated software actor ID")
    parser.add_argument("--principal", help="Authenticated human principal ID")
    parser.add_argument("--purpose", help="Declared purpose of use")
    parser.add_argument("--scope", action="append", default=[], help="Validated inbound scope")
    parser.add_argument(
        "--registry",
        action="append",
        default=[],
        metavar="API=URL",
        help=(
            "Registry to discover services from, repeatable; replaces the default GA4GH "
            "Implementation Registry. API is implementation-registry or service-registry."
        ),
    )
    parser.add_argument(
        "--allow-http",
        action="store_true",
        help="Allow plain HTTP targets for explicit local development",
    )
    parser.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Allow private and loopback targets for explicit local development",
    )
    parser.add_argument("--ledger-path", type=Path, help="Override the local WES ledger path")
    parser.add_argument("--pretty", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    payload = _load_payload(args.input)
    authority = AuthorityContext(
        human_principal=args.principal,
        software_actor=args.actor,
        purpose=args.purpose,
        inbound_scopes=args.scope,
    )
    overrides: dict[str, Any] = {
        "allow_http": args.allow_http,
        "allow_private_hosts": args.allow_private_hosts,
    }
    if args.registry:
        overrides["registries"] = [_registry(value) for value in args.registry]
    if args.ledger_path:
        overrides["ledger_path"] = args.ledger_path
    async with Harness(settings=Settings(**overrides)) as harness:
        result = await harness.dispatch(args.operation, payload, authority=authority)
    output = result.model_dump(mode="json", exclude_none=True)
    output.setdefault("data", None)
    print(json.dumps(output, indent=2 if args.pretty else None))
    return 0 if result.status != ResultStatus.FAILURE else 1


def main() -> None:
    try:
        code = asyncio.run(_run(build_parser().parse_args()))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
