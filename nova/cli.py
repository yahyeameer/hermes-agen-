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
from nova.control.auth import PRINCIPALS_FILENAME, ROLES as AUTH_ROLES
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

    knowledge = sub.add_parser("knowledge", help="manage the tenant's knowledge corpora")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)

    ingest_cmd = knowledge_sub.add_parser("ingest", help="index declared sources")
    ingest_cmd.add_argument("bundle", type=Path)
    ingest_cmd.add_argument(
        "--source",
        action="append",
        default=[],
        dest="sources",
        metavar="ID",
        help="only this source (repeatable); default: every declared source",
    )
    ingest_cmd.add_argument(
        "--force",
        action="store_true",
        help="re-chunk every document, including those whose content is unchanged",
    )
    ingest_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="walk, extract and chunk without writing the index",
    )

    knowledge_status = knowledge_sub.add_parser("status", help="what the index currently holds")
    knowledge_status.add_argument("bundle", type=Path)
    knowledge_status.add_argument("--json", action="store_true")

    knowledge_search = knowledge_sub.add_parser(
        "search", help="run a search exactly as an agent would"
    )
    knowledge_search.add_argument("bundle", type=Path)
    knowledge_search.add_argument("query")
    knowledge_search.add_argument(
        "--as-agent",
        default="",
        metavar="ID",
        help="scope to this agent's granted corpora — the check that matters before a rollout",
    )
    knowledge_search.add_argument("--source", action="append", default=[], dest="sources")
    knowledge_search.add_argument("--limit", type=int, default=5)

    doctor = sub.add_parser(
        "doctor", help="what each agent still needs before it can run"
    )
    doctor.add_argument("bundle", type=Path)
    doctor.add_argument("--json", action="store_true")

    objective = sub.add_parser("objective", help="plan, submit and follow business objectives")
    objective_sub = objective.add_subparsers(dest="objective_command", required=True)

    obj_list = objective_sub.add_parser("list", help="declared objectives and how they route")
    obj_list.add_argument("bundle", type=Path)

    obj_plan = objective_sub.add_parser(
        "plan", help="route an objective and show the plan — writes nothing"
    )
    obj_plan.add_argument("bundle", type=Path)
    obj_plan.add_argument("objective")

    obj_submit = objective_sub.add_parser("submit", help="place an objective's steps on the board")
    obj_submit.add_argument("bundle", type=Path)
    obj_submit.add_argument("objective")
    obj_submit.add_argument(
        "--dry-run", action="store_true", help="show what would be created, write nothing"
    )

    obj_status = objective_sub.add_parser("status", help="an objective's state, read from the runtime")
    obj_status.add_argument("bundle", type=Path)
    obj_status.add_argument("objective", nargs="?", default="")
    obj_status.add_argument("--json", action="store_true")

    serve_cmd = sub.add_parser("serve", help="run the read-only Control API and dashboard")
    serve_cmd.add_argument("bundle", type=Path)
    serve_cmd.add_argument("--host", default="127.0.0.1", help="default: loopback only")
    serve_cmd.add_argument("--port", type=int, default=8787)
    serve_cmd.add_argument(
        "--principals",
        type=Path,
        default=None,
        metavar="FILE",
        help="who may call the control plane (default: <home>/control-principals.yaml)",
    )
    serve_cmd.add_argument("--tls-cert", default="", metavar="FILE", help="TLS certificate")
    serve_cmd.add_argument("--tls-key", default="", metavar="FILE", help="TLS private key")
    serve_cmd.add_argument(
        "--behind-tls-proxy",
        action="store_true",
        help="TLS terminates at a proxy in front of this process; required to bind a "
             "non-loopback interface without --tls-cert",
    )

    token = sub.add_parser("token", help="manage control-plane access")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_new = token_sub.add_parser("new", help="mint a token and print the entry to add")
    token_new.add_argument("name", help="who this token is for, as it will appear in logs")
    token_new.add_argument(
        "--role", choices=list(AUTH_ROLES), default="viewer",
        help="viewer: operational state. admin: also policy, decisions and cost",
    )
    token_list = token_sub.add_parser("list", help="who may call the control plane")
    token_list.add_argument("--principals", type=Path, default=None, metavar="FILE")

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


def _token(args) -> int:
    """``nova token ...`` — mint and inspect control-plane credentials.

    The token is printed **once** and never stored: NOVA writes the digest into the
    principals file and keeps nothing it could later leak. That is the same rule the
    deployment seam follows for model credentials, applied to NOVA's own front door — and
    it means a stolen principals file is an inconvenience rather than an incident.
    """
    from nova.control.auth import PrincipalStore, hash_token, new_token

    runtime = get_runtime(args.runtime, home=args.home)

    if args.token_command == "list":
        path = args.principals or (runtime.state_location / PRINCIPALS_FILENAME)
        store = PrincipalStore.load(path)
        if not store.configured:
            print(f"no principals configured at {path}")
            return 0
        print(f"{path}")
        for entry in store.describe():
            print(f"  {entry['name']:24} {entry['role']}")
        return 0

    secret = new_token()
    path = runtime.state_location / PRINCIPALS_FILENAME
    print(f"token for {args.name} ({args.role}) — shown once, not stored by NOVA:\n")
    print(f"  {secret}\n")
    print(f"Add this entry to {path}:\n")
    print("principals:")
    print(f"  - name: {args.name}")
    print(f"    role: {args.role}")
    print(f"    token_sha256: {hash_token(secret)}")
    print(
        "\nCallers send it as:  Authorization: Bearer <token>\n"
        "Revoke by deleting the entry; no other principal is affected."
    )
    return 0


def _doctor(args) -> int:
    """``nova doctor`` — deployment readiness, reported and never fixed.

    NOVA writes the name of every credential and none of the values, so "materialized" and
    "able to run" are different states. Exits non-zero when any agent cannot run, so a
    deployment pipeline fails here rather than at the first task.
    """
    bundle = load_bundle(args.bundle)
    runtime = get_runtime(args.runtime, home=args.home, tenant_id=bundle.tenant_id)
    rows = [runtime.deployment_readiness(spec, bundle.deployment) for spec in bundle.agents]

    if args.json:
        print(json.dumps({"tenant_id": bundle.tenant_id, "agents": rows}, indent=2))
        return 0 if all(row.get("ready", True) for row in rows) else 1

    print(f"{bundle.tenant_id}: {len(rows)} agent(s)")
    for spec, row in zip(bundle.agents, rows):
        resolved = row.get("provider") or {}
        mark = "ok " if row.get("ready", True) else "NOT READY"
        print(f"\n  [{mark}] {spec.id}")
        print(f"           provider: {resolved.get('provider') or '(runtime default)'}"
              f"   model: {resolved.get('model') or '(runtime default)'}")
        for name in row.get("required", []):
            where = row.get("resolved_from", {}).get(name)
            print(f"           {name}: {where or 'MISSING'}")
        if not row.get("ready", True):
            print(f"           -> add the missing name(s) to {row.get('env_file')}")
        for warning in row.get("warnings", []):
            print(f"           warning: {warning}")

    unready = [row for row in rows if not row.get("ready", True)]
    if unready:
        print(
            f"\n{len(unready)} agent(s) cannot run yet. NOVA never writes credentials — "
            "those files are yours and survive every apply."
        )
        return 1
    print("\nevery agent can resolve its credentials")
    return 0


def _objective(args) -> int:
    """``nova objective ...`` — route, submit and follow declared business processes."""
    from nova.supervisor import collect, plan_order, route_objective, submit_objective

    bundle = load_bundle(args.bundle)
    runtime = get_runtime(args.runtime, home=args.home, tenant_id=bundle.tenant_id)

    if not bundle.objectives:
        print(f"{bundle.tenant_id}: no objectives declared (objectives/ is absent or empty)")
        return 0

    def find(objective_id: str):
        for spec in bundle.objectives:
            if spec.id == objective_id:
                return spec
        known = ", ".join(spec.id for spec in bundle.objectives)
        raise NovaError(f"no objective {objective_id!r}; declared: {known}")

    if args.objective_command == "list":
        for spec in bundle.objectives:
            decision = route_objective(spec, bundle.agents)
            state = "ok" if decision.allowed else f"REFUSED ({len(decision.refusals)})"
            flag = "" if spec.enabled else "  (disabled)"
            print(f"  {spec.id:26} {len(spec.steps)} step(s)  owner={spec.owner:18} {state}{flag}")
        return 0

    if args.objective_command == "plan":
        spec = find(args.objective)
        decision = route_objective(spec, bundle.agents)
        print(f"{spec.title}  ({spec.id})")
        print(f"owner: {spec.owner}")
        if spec.acceptance:
            print(f"done when: {spec.acceptance}")
        print()
        # Printed in execution order rather than declaration order: the question a reader
        # has in front of a plan is what runs when, and steps with no dependencies at the
        # top of the list is the answer.
        for step in plan_order(spec):
            routing = next(r for r in decision.steps if r.step_id == step.id)
            mark = "ok " if routing.allowed else "REFUSED"
            waits = f"  after {', '.join(step.depends_on)}" if step.depends_on else "  (starts immediately)"
            print(f"  [{mark}] {step.id:22} -> {step.assignee:18}{waits}")
            if not routing.allowed:
                print(f"           {routing.detail}")
        for warning in decision.warnings:
            print(f"\n  warning: {warning}")
        print()
        print(decision.explain().splitlines()[0])
        return 0 if decision.allowed else 1

    if args.objective_command == "submit":
        spec = find(args.objective)
        audit = (
            NullAuditLog(tenant_id=bundle.tenant_id)
            if args.dry_run
            else AuditLog.for_home(
                runtime.state_location, tenant_id=bundle.tenant_id, actor="nova-cli"
            )
        )
        report = submit_objective(
            spec,
            bundle.agents,
            runtime,
            audit=audit,
            tenant_id=bundle.tenant_id,
            dry_run=args.dry_run,
        )
        print(report.summary())
        for warning in report.warnings:
            print(f"  warning: {warning}")
        if report.refused:
            print("\nNothing was submitted. Fix the routing above, or widen the owner's "
                  "delegation.may_assign_to, then re-run.")
            return 1
        if report.result is not None:
            for item in report.result.items:
                # A dry run has created nothing, and a row that says "created" next to a
                # missing task id invites exactly the wrong reading.
                if args.dry_run:
                    mark = "would add"
                else:
                    mark = "created" if item.created else "existing"
                print(f"  {mark:9} {item.key:44} {item.task_id or '-'}  {item.state or '-'}")
        if args.dry_run:
            print("\nNothing was written. Re-run without --dry-run to submit.")
        return 0

    # status
    selected = [find(args.objective)] if args.objective else list(bundle.objectives)
    tasks = runtime.list_tasks(limit=1000)
    reports = [collect(spec, runtime, tasks=tasks) for spec in selected]

    if args.json:
        print(json.dumps([report.to_dict() for report in reports], indent=2))
        return 0

    for report in reports:
        print(report.summary())
        for step in report.steps:
            state = step.state if step.submitted else "not submitted"
            flag = "  !" if step.needs_attention else ""
            print(f"    {step.step_id:22} {state:14} {step.assignee:18}{flag}")
            if step.last_error:
                print(f"      last error: {step.last_error}")
        for warning in report.warnings:
            print(f"    warning: {warning}")
    return 0


def _knowledge(args) -> int:
    """``nova knowledge ...`` — ingest, inspect and rehearse retrieval."""
    from nova.knowledge import KnowledgeIndex, ingest

    bundle = load_bundle(args.bundle)
    runtime = get_runtime(args.runtime, home=args.home, tenant_id=bundle.tenant_id)
    index_path = runtime.knowledge_index_path

    if args.knowledge_command == "ingest":
        if not bundle.knowledge.sources:
            print(f"{bundle.tenant_id}: no knowledge sources declared (knowledge.yaml is absent)")
            return 0
        audit = (
            NullAuditLog(tenant_id=bundle.tenant_id)
            if args.dry_run
            else AuditLog.for_home(
                runtime.state_location, tenant_id=bundle.tenant_id, actor="nova-cli"
            )
        )
        report = ingest(
            bundle.knowledge,
            index_path,
            extractor=runtime,
            source_ids=args.sources or None,
            audit=audit,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(report.summary())
        for source in report.sources:
            for path, reason in source.skipped:
                print(f"  skipped  {source.source_id}/{path}: {reason}")
            for path in source.removed:
                print(f"  removed  {source.source_id}/{path} (no longer on disk)")
        if args.dry_run:
            print("\nNothing was written. Re-run without --dry-run to index.")
        else:
            print(f"\nindex: {index_path}")
        return 0

    if not index_path.is_file():
        print(
            f"no knowledge index at {index_path}. Run `nova knowledge ingest "
            f"{args.bundle}` first.",
            file=sys.stderr,
        )
        return 1

    with KnowledgeIndex.open(index_path, create=False) as index:
        if args.knowledge_command == "status":
            stats = index.stats()
            if args.json:
                print(json.dumps({"index": str(index_path), "sources": stats}, indent=2))
                return 0
            print(f"index: {index_path}")
            declared = {source.id for source in bundle.knowledge.sources}
            for source_id in sorted(set(stats) | declared):
                row = stats.get(source_id)
                if row is None:
                    print(f"  {source_id:22} declared, never ingested")
                    continue
                extra = "" if source_id in declared else "   (no longer declared)"
                print(
                    f"  {source_id:22} {row['documents']:5} docs  {row['chunks']:6} chunks  "
                    f"{row['bytes'] / 1024:8.1f} KiB{extra}"
                )
            return 0

        # search
        scope = list(args.sources)
        if args.as_agent:
            spec = bundle.agent(args.as_agent)
            granted = list(spec.knowledge.sources)
            # Intersect rather than union: --as-agent is a rehearsal of that agent's real
            # scope, so it must never be able to reach past it.
            scope = [s for s in scope if s in granted] if scope else granted
            if not scope:
                print(f"{args.as_agent} is granted no knowledge sources; nothing to search.")
                return 0
        elif not scope:
            scope = [source.id for source in bundle.knowledge.sources]

        hits = index.search(args.query, source_ids=scope, limit=args.limit)
        print(f"searching {', '.join(scope)} for {args.query!r}")
        if not hits:
            print("no passages matched")
            return 0
        for position, hit in enumerate(hits, start=1):
            print(f"\n[{position}] {hit.doc_title or hit.doc_path} — {hit.citation}")
            print(f"    score {hit.score:.4g}   source {hit.source_id}")
            for line in hit.snippet.splitlines():
                print(f"    {line}")
    return 0


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

        if args.command == "knowledge":
            return _knowledge(args)

        if args.command == "objective":
            return _objective(args)

        if args.command == "doctor":
            return _doctor(args)

        if args.command == "token":
            return _token(args)

        if args.command == "serve":
            from nova.control import ControlAPI, serve

            bundle = load_bundle(args.bundle)
            runtime = get_runtime(args.runtime, home=args.home, tenant_id=bundle.tenant_id)
            api = ControlAPI(
                bundle,
                runtime,
                audit=AuditLog.for_home(
                    runtime.state_location, tenant_id=bundle.tenant_id, actor="nova-control"
                ),
            )
            from nova.control.auth import PrincipalStore

            principals_path = args.principals or (
                runtime.state_location / PRINCIPALS_FILENAME
            )
            principals = PrincipalStore.load(principals_path)
            if principals.configured:
                who = ", ".join(
                    f"{entry['name']}({entry['role']})" for entry in principals.describe()
                )
                print(f"principals: {who}")
            else:
                print(
                    f"no principals file at {principals_path} — serving loopback only, "
                    "every local caller is admin. Add one with `nova token new`."
                )
            serve(
                api,
                host=args.host,
                port=args.port,
                principals=principals,
                tls_certfile=args.tls_cert,
                tls_keyfile=args.tls_key,
                behind_tls_proxy=args.behind_tls_proxy,
                ready=lambda url: print(f"{bundle.identity.product_name} control plane: {url}"),
            )
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
