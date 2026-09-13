"""Apply a tenant bundle to a runtime.

The one orchestration entry point above the adapter: take a fully validated bundle and
make a runtime match it. Everything it does is expressed against
:class:`~nova.runtime.base.AgentRuntime`, so it works unchanged on any future adapter.

Ordering matters and is deliberate. Identity is applied **before** agents, because an
agent's persona embeds its branded display name — projecting identity second would leave
every agent carrying the previous tenant's name until the next materialize.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from nova.audit import AuditLog, new_correlation_id
from nova.policy import compile_policy
from nova.runtime.base import AgentRuntime, MaterializeResult
from nova.spec import TenantBundle


@dataclass(frozen=True)
class ApplyReport:
    """What applying a bundle did, per agent and overall."""

    tenant_id: str
    runtime: str
    correlation_id: str
    bundle_digest: str
    dry_run: bool
    identity: Optional[MaterializeResult] = None
    agents: tuple[MaterializeResult, ...] = ()
    skipped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(r.agent_id for r in self.agents if r.created)

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(r.agent_id for r in self.agents if r.changed and not r.created)

    @property
    def unchanged(self) -> tuple[str, ...]:
        return tuple(r.agent_id for r in self.agents if r.unchanged)

    def summary(self) -> str:
        parts = [
            f"tenant={self.tenant_id}",
            f"runtime={self.runtime}",
            f"created={len(self.created)}",
            f"changed={len(self.changed)}",
            f"unchanged={len(self.unchanged)}",
        ]
        if self.skipped:
            parts.append(f"skipped={len(self.skipped)}")
        if self.dry_run:
            parts.append("dry-run")
        return " ".join(parts)


def apply_bundle(
    bundle: TenantBundle,
    runtime: AgentRuntime,
    *,
    audit: AuditLog,
    dry_run: bool = False,
    include_disabled: bool = False,
    correlation_id: Optional[str] = None,
) -> ApplyReport:
    """Make ``runtime`` match ``bundle``.

    Disabled agents are skipped by default rather than materialized-and-ignored: leaving
    no profile behind is the unambiguous state, and a disabled agent that still has a
    profile is one dispatcher configuration change away from running.

    This function never removes agents. Deletion is destructive and stays an explicit
    operator action through :meth:`AgentRuntime.remove_agent`; a bundle that no longer
    names an agent produces a warning here, not a removal.
    """
    correlation_id = correlation_id or new_correlation_id()
    warnings: list[str] = []

    capability_gaps = runtime.capabilities.missing_for(["durable_tasks", "process_isolation"])
    if capability_gaps:
        warnings.append(
            f"runtime {runtime.name!r} does not provide: {', '.join(capability_gaps)} — "
            "unattended agent work may not survive a restart"
        )

    if bundle.policy is not None and not runtime.capabilities.policy_enforcement:
        warnings.append(
            f"a policy is declared but runtime {runtime.name!r} cannot enforce it — every "
            "rule would be inert. Refusing to present governance that does not exist"
        )

    knowledge_users = [spec.id for spec in bundle.agents if spec.knowledge.sources]
    if knowledge_users and not runtime.capabilities.knowledge_retrieval:
        warnings.append(
            f"knowledge sources are declared by {', '.join(knowledge_users)} but runtime "
            f"{runtime.name!r} cannot retrieve them yet; the declaration is recorded and inert"
        )

    audit.record(
        "bundle.apply_started",
        correlation_id=correlation_id,
        subject=bundle.tenant_id,
        digest=bundle.digest(),
        detail={
            "runtime": runtime.name,
            "dry_run": dry_run,
            "agents": [spec.id for spec in bundle.agents],
            "policy_declared": bundle.policy is not None,
            "warnings": warnings,
        },
    )

    identity_result = runtime.apply_identity(
        bundle.identity, audit=audit, correlation_id=correlation_id, dry_run=dry_run
    )

    results: list[MaterializeResult] = []
    skipped: list[str] = []
    selected = bundle.agents if include_disabled else bundle.enabled_agents()
    for spec in bundle.agents:
        if spec not in selected:
            skipped.append(spec.id)
            continue
        compiled = compile_policy(spec, bundle.policy) if bundle.policy is not None else None
        if compiled is not None:
            warnings.extend(f"{spec.id}: {note}" for note in compiled.warnings)
            # Runtime-specific fields the portable compiler cannot know: where to record a
            # refusal, and which tenant it belongs to. Injected here, where both are in hand.
            compiled.document["tenant_id"] = bundle.tenant_id
            compiled.document["audit_log"] = str(audit.path)
        results.append(
            runtime.materialize_agent(
                spec,
                audit=audit,
                correlation_id=correlation_id,
                identity=bundle.identity,
                policy=compiled,
                dry_run=dry_run,
            )
        )

    orphans = _orphans(bundle, runtime)
    if orphans:
        warnings.append(
            f"runtime still holds NOVA-managed agent(s) no longer in the bundle: "
            f"{', '.join(orphans)} — remove them explicitly if that is intended"
        )

    report = ApplyReport(
        tenant_id=bundle.tenant_id,
        runtime=runtime.name,
        correlation_id=correlation_id,
        bundle_digest=bundle.digest(),
        dry_run=dry_run,
        identity=identity_result,
        agents=tuple(results),
        skipped=tuple(skipped),
        warnings=tuple(warnings),
    )

    audit.record(
        "bundle.apply_finished",
        correlation_id=correlation_id,
        subject=bundle.tenant_id,
        digest=bundle.digest(),
        detail={
            "runtime": runtime.name,
            "dry_run": dry_run,
            "created": list(report.created),
            "changed": list(report.changed),
            "unchanged": list(report.unchanged),
            "skipped": list(report.skipped),
            "warnings": list(report.warnings),
        },
    )
    return report


def _orphans(bundle: TenantBundle, runtime: AgentRuntime) -> list[str]:
    """NOVA-managed agents present in the runtime but absent from the bundle."""
    declared = {spec.id for spec in bundle.agents}
    return sorted(
        agent.agent_id
        for agent in runtime.list_agents()
        if agent.managed_by_nova and agent.agent_id not in declared
    )
