"""``python -m nova`` — operator commands for the platform layer.

Deliberately small. This is not a replacement for the runtime's own CLI, which stays
exactly where it is for engineering and debugging; it covers only the platform
operations that have no runtime equivalent: validating a tenant bundle, previewing what
applying it would do, applying it, and reporting what a runtime currently holds.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from nova import __version__
from nova.apply import apply_bundle
from nova.audit import AuditLog, NullAuditLog
from nova.errors import NovaError
from nova.runtime import available_runtimes, get_runtime
from nova.spec import load_bundle


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nova", description="NOVA platform operations for an agent runtime."
    )
    parser.add_argument("--version", action="version", version=f"nova {__version__}")
    runtimes = available_runtimes()
    parser.add_argument(
        "--runtime",
        default=runtimes[0] if runtimes else None,
        choices=runtimes or None,
        help=f"runtime adapter to target (available: {', '.join(runtimes)})",
    )
    parser.add_argument(
        "--home", type=Path, default=None, help="runtime home directory (default: auto-detected)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="load and fully validate a tenant bundle")
    validate.add_argument("bundle", type=Path)
    validate.add_argument("--json", action="store_true", help="print the resolved bundle as JSON")

    plan = sub.add_parser("plan", help="show what applying a bundle would change")
    plan.add_argument("bundle", type=Path)

    apply_cmd = sub.add_parser("apply", help="make the runtime match a bundle")
    apply_cmd.add_argument("bundle", type=Path)
    apply_cmd.add_argument(
        "--include-disabled",
        action="store_true",
        help="also materialize agents whose spec sets enabled: false",
    )

    status = sub.add_parser("status", help="report what the runtime currently holds")
    status.add_argument("--json", action="store_true")

    return parser


def _report(report, *, verb: str) -> None:
    print(report.summary())
    for label, ids in (("created", report.created), ("changed", report.changed)):
        for agent_id in ids:
            print(f"  {label:9} {agent_id}")
    for agent_id in report.skipped:
        print(f"  {'skipped':9} {agent_id} (disabled in its spec)")
    for warning in report.warnings:
        print(f"  warning:  {warning}")
    if report.dry_run:
        print(f"\nNothing was written. Re-run `nova {verb}` without --dry-run to apply.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            bundle = load_bundle(args.bundle)
            if args.json:
                print(json.dumps(bundle.to_dict(), indent=2))
            else:
                print(f"{bundle.tenant_id}: {len(bundle.agents)} agent(s) valid")
                for spec in bundle.agents:
                    state = "" if spec.enabled else "  (disabled)"
                    print(f"  {spec.id:22} {bundle.identity.display_name_for(spec.id, spec.name)}{state}")
                print(f"digest: {bundle.digest()}")
            return 0

        if args.command == "status":
            runtime = get_runtime(args.runtime, home=args.home)
            agents = runtime.list_agents()
            if args.json:
                print(
                    json.dumps(
                        {
                            "runtime": runtime.describe(),
                            "agents": [
                                {
                                    "id": a.agent_id,
                                    "managed_by_nova": a.managed_by_nova,
                                    "digest": a.digest,
                                }
                                for a in agents
                            ],
                        },
                        indent=2,
                    )
                )
                return 0
            print(f"runtime: {runtime.name}   state: {runtime.state_location}")
            if not agents:
                print("no agents materialized")
                return 0
            for agent in agents:
                owner = "nova" if agent.managed_by_nova else "not nova-managed"
                print(f"  {agent.agent_id:22} {owner}")
            return 0

        # plan / apply both load a bundle and drive the same code path.
        bundle = load_bundle(args.bundle)
        runtime = get_runtime(args.runtime, home=args.home, tenant_id=bundle.tenant_id)
        dry_run = args.command == "plan"
        audit = (
            NullAuditLog(tenant_id=bundle.tenant_id)
            if dry_run
            else AuditLog.for_home(
                runtime.state_location, tenant_id=bundle.tenant_id, actor="nova-cli"
            )
        )
        report = apply_bundle(
            bundle,
            runtime,
            audit=audit,
            dry_run=dry_run,
            include_disabled=getattr(args, "include_disabled", False),
        )
        _report(report, verb="apply")
        if not dry_run:
            print(f"\naudit: {audit.path}")
            open_intents = audit.open_intents()
            if open_intents:
                print(
                    f"WARNING: {len(open_intents)} unfinished change(s) in the audit log — "
                    "a previous run was interrupted"
                )
        return 0

    except NovaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
