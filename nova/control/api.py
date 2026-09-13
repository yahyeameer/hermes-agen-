"""Route handlers for the Control API.

Pure functions of (path, query) -> :class:`Response`. No HTTP framework, no sockets, no
globals — the transport is a thin adapter in :mod:`nova.control.server`, so the API's
behaviour is tested directly and the transport can be replaced without touching any of
this.

Phase 1 serves five read-only routes. Every response is JSON-serialisable data assembled
from the runtime adapter and the tenant bundle; nothing here knows which runtime is
underneath.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from nova import __version__
from nova.audit import AuditLog
from nova.errors import NovaError
from nova.policy import agent_digest, compile_policy, decide
from nova.policy.limits import ENFORCING_CLASSES
from nova.runtime.base import AgentRuntime
from nova.spec import TenantBundle

API_PREFIX = "/platform/v1"


@dataclass(frozen=True)
class Response:
    """One API response: an HTTP status and a JSON-serialisable body."""

    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _error(status: int, message: str, **extra: Any) -> Response:
    """A problem the caller can act on, in one consistent shape."""
    return Response(status, {"error": {"status": status, "message": message, **extra}})


class ControlAPI:
    """Read-only control plane over one tenant's deployment.

    Constructed with the declared bundle and a runtime adapter. The bundle is the
    declared truth and the runtime is the applied truth; where they differ the API says
    so rather than presenting one as the other.
    """

    def __init__(
        self,
        bundle: TenantBundle,
        runtime: AgentRuntime,
        *,
        audit: Optional[AuditLog] = None,
    ) -> None:
        self.bundle = bundle
        self.runtime = runtime
        self.audit = audit

    def _compiled(self, agent_id: str):
        """The compiled policy for one agent, or None when no policy is declared."""
        if self.bundle.policy is None:
            return None
        return compile_policy(self.bundle.agent(agent_id), self.bundle.policy)

    # -- routing --------------------------------------------------------------

    def handle(self, path: str, query: Optional[Mapping[str, str]] = None) -> Response:
        """Dispatch one GET. Unknown paths are 404, write methods never reach here."""
        query = query or {}
        if not path.startswith(API_PREFIX):
            return _error(404, f"no such route: {path}")
        tail = path[len(API_PREFIX) :].rstrip("/") or "/"

        if tail == "/health":
            return self.health()
        if tail == "/identity":
            return self.identity()
        if tail == "/agents":
            return self.agents()
        if tail == "/tasks":
            return self.tasks(query)
        if tail.startswith("/tasks/"):
            task_id = tail[len("/tasks/") :]
            return self.task(task_id) if task_id else _error(404, "no task id given")
        if tail == "/policy":
            return self.policy()
        if tail == "/policy/simulate":
            return self.simulate(query)
        if tail == "/decisions":
            return self.decisions(query)
        if tail == "/budget":
            return self.budget()
        if tail == "/knowledge":
            return self.knowledge()
        return _error(404, f"no such route: {path}")

    # -- routes ---------------------------------------------------------------

    def health(self) -> Response:
        """Platform and runtime health. Never 503 for an absent work store — that is normal."""
        runtime_health = self.runtime.health()
        return Response(
            200,
            {
                "platform": {"version": __version__, "tenant_id": self.bundle.tenant_id},
                "runtime": {**self.runtime.describe(), **runtime_health.to_dict()},
                "bundle": {"digest": self.bundle.digest(), "agents": len(self.bundle.agents)},
            },
        )

    def identity(self) -> Response:
        """Resolved branding for display surfaces.

        The dashboard themes itself from this at boot, which is what makes one build
        serve differently-branded deployments.
        """
        identity = self.bundle.identity
        return Response(
            200,
            {
                "product_name": identity.product_name,
                "company_name": identity.company_name,
                "logo": identity.logo,
                "favicon": identity.favicon,
                "theme": identity.theme.to_dict(),
                "support": identity.support.to_dict(),
                "welcome": identity.welcome,
                "tenant_id": self.bundle.tenant_id,
            },
        )

    def agents(self) -> Response:
        """Declared agents, each with its applied state and whether the two agree."""
        applied = {agent.agent_id: agent for agent in self.runtime.list_agents()}
        identity = self.bundle.identity

        rows: list[dict[str, Any]] = []
        for spec in self.bundle.agents:
            live = applied.get(spec.id)
            declared_digest = self.runtime.expected_digest(
                spec, policy=self._compiled(spec.id), knowledge=self.bundle.knowledge
            )
            rows.append(
                {
                    "id": spec.id,
                    "display_name": identity.display_name_for(spec.id, spec.name),
                    "role": spec.role,
                    "description": spec.description,
                    "enabled": spec.enabled,
                    "model": spec.model.to_dict(),
                    "materialized": live is not None,
                    "in_sync": bool(live and live.digest == declared_digest),
                    "declared_digest": declared_digest,
                    "applied_digest": live.digest if live else "",
                    "limits": spec.limits.to_dict(),
                    "approval_required_for": list(spec.approval.required_for),
                    "knowledge_sources": list(spec.knowledge.sources),
                    "may_assign_to": list(spec.delegation.may_assign_to),
                }
            )

        # Agents present in the runtime but absent from the bundle are surfaced rather
        # than hidden: an operator needs to see everything that can run.
        declared = {spec.id for spec in self.bundle.agents}
        unmanaged = [
            {
                "id": agent.agent_id,
                "display_name": agent.agent_id,
                "declared": False,
                "managed_by_nova": agent.managed_by_nova,
            }
            for agent in applied.values()
            if agent.agent_id not in declared
        ]

        return Response(200, {"agents": rows, "undeclared": unmanaged})

    def tasks(self, query: Mapping[str, str]) -> Response:
        """Work the runtime holds, newest first, with a per-state tally."""
        agent_id = (query.get("agent") or "").strip()
        try:
            limit = int(query.get("limit") or 100)
        except ValueError:
            return _error(400, "limit must be a whole number")
        if limit < 1:
            return _error(400, "limit must be at least 1")

        views = self.runtime.list_tasks(agent_id=agent_id, limit=limit)
        counts: dict[str, int] = {}
        for view in views:
            counts[view.state] = counts.get(view.state, 0) + 1

        return Response(
            200,
            {
                "tasks": [view.to_dict() for view in views],
                "counts": counts,
                "needs_attention": sum(1 for view in views if view.needs_attention),
                "filtered_by_agent": agent_id,
            },
        )

    def knowledge(self) -> Response:
        """Declared corpora, what is actually indexed, and which agents can read each.

        Three facts that live in three places — the bundle, the index, and each agent's
        grant — and are only useful together. "Who can read the handbook" and "is the
        handbook actually indexed" are the two questions asked about a knowledge
        deployment, and neither is answerable from one source alone.
        """
        declared = self.bundle.knowledge.sources
        readers: dict[str, list[str]] = {}
        for spec in self.bundle.agents:
            for source_id in spec.knowledge.sources:
                readers.setdefault(source_id, []).append(spec.id)

        indexed: dict[str, dict[str, int]] = {}
        index_detail = ""
        index_path = self.runtime.knowledge_index_path
        if index_path.is_file():
            try:
                from nova.knowledge import KnowledgeIndex

                with KnowledgeIndex.open(index_path, create=False) as index:
                    indexed = index.stats()
            except NovaError as exc:
                index_detail = str(exc)
        else:
            index_detail = "no index yet — run `nova knowledge ingest`"

        sources = [
            {
                "id": source.id,
                "title": source.display_title,
                "description": source.description,
                "classification": source.classification,
                "root": str(source.root),
                "readable_by": sorted(readers.get(source.id, [])),
                "indexed": source.id in indexed,
                "documents": indexed.get(source.id, {}).get("documents", 0),
                "chunks": indexed.get(source.id, {}).get("chunks", 0),
                "bytes": indexed.get(source.id, {}).get("bytes", 0),
            }
            for source in declared
        ]

        # A corpus in the index that the bundle no longer declares is still searchable by
        # any agent whose grant was not re-applied. Surfaced rather than filtered out.
        undeclared = sorted(set(indexed) - {source.id for source in declared})

        return Response(
            200,
            {
                "retrieval_enabled": self.runtime.capabilities.knowledge_retrieval,
                "document_extraction": self.runtime.capabilities.document_extraction,
                "index_path": str(index_path),
                "index_detail": index_detail,
                "sources": sources,
                "undeclared_in_index": undeclared,
                "agents": [
                    {
                        "id": spec.id,
                        "display_name": self.bundle.identity.display_name_for(spec.id, spec.name),
                        "sources": list(spec.knowledge.sources),
                    }
                    for spec in self.bundle.agents
                ],
            },
        )

    def task(self, task_id: str) -> Response:
        view = self.runtime.get_task(task_id)
        if view is None:
            return _error(404, f"no task {task_id!r}")
        return Response(200, {"task": view.to_dict()})

    # -- governance -----------------------------------------------------------

    def policy(self) -> Response:
        """The tenant policy and what it compiles to per agent.

        A security reviewer reads this to answer "what can this agent actually do?"
        without reading YAML across several files or trusting a summary.
        """
        if self.bundle.policy is None:
            return Response(
                200,
                {
                    "declared": False,
                    "enforced": False,
                    "detail": (
                        "no policy.yaml in this tenant bundle; agents run with no "
                        "platform-level restrictions"
                    ),
                    "actions": {},
                    "permissions": {},
                    "agents": [],
                },
            )

        enforced = self.runtime.capabilities.policy_enforcement
        agents = []
        for spec in self.bundle.agents:
            compiled = compile_policy(spec, self.bundle.policy)
            agents.append(
                {
                    "id": spec.id,
                    "display_name": self.bundle.identity.display_name_for(spec.id, spec.name),
                    "allow": compiled.document["allow"],
                    "deny": compiled.document["deny"],
                    "approval_actions": sorted(compiled.document["approval_actions"]),
                    "unlisted_tool": compiled.document["unlisted_tool"],
                    "has_allowlist": compiled.has_allowlist,
                    "warnings": list(compiled.warnings),
                }
            )

        return Response(
            200,
            {
                "declared": True,
                "enforced": enforced,
                "detail": (
                    ""
                    if enforced
                    else f"runtime {self.runtime.name!r} cannot enforce policy; these rules are inert"
                ),
                "actions": {
                    name: action.to_dict()
                    for name, action in sorted(self.bundle.policy.actions.items())
                },
                "permissions": {
                    name: permission.to_dict()
                    for name, permission in sorted(self.bundle.policy.permissions.items())
                },
                "baseline_tools": list(self.bundle.policy.baseline_tools),
                "agents": agents,
            },
        )

    def simulate(self, query: Mapping[str, str]) -> Response:
        """Explain what policy would do for one agent and one tool.

        Answers the question a customer's security team actually asks — "what happens if
        this agent calls that?" — using the same decision function the runtime enforces
        with, so the answer cannot drift from the behaviour.
        """
        agent_id = (query.get("agent") or "").strip()
        tool = (query.get("tool") or "").strip()
        if not agent_id or not tool:
            return _error(400, "both 'agent' and 'tool' are required")

        known = {spec.id for spec in self.bundle.agents}
        if agent_id not in known:
            return _error(404, f"no agent {agent_id!r}", known_agents=sorted(known))

        compiled = self._compiled(agent_id)
        if compiled is None:
            return Response(
                200,
                {
                    "agent": agent_id,
                    "tool": tool,
                    "decision": {
                        "effect": "allow",
                        "reason": "no policy is declared for this tenant",
                        "tool": tool,
                        "action": "",
                        "rule": "no-policy",
                    },
                    "enforced": False,
                },
            )

        decision = decide(compiled.document, tool)
        return Response(
            200,
            {
                "agent": agent_id,
                "tool": tool,
                "decision": decision.to_dict(),
                "enforced": self.runtime.capabilities.policy_enforcement,
            },
        )

    def decisions(self, query: Mapping[str, str]) -> Response:
        """Refusals and escalations the runtime has recorded.

        Permitted calls are deliberately absent: they are the overwhelming majority, and
        including them would bury what a reviewer is looking for.
        """
        try:
            limit = int(query.get("limit") or 100)
        except ValueError:
            return _error(400, "limit must be a whole number")
        if limit < 1:
            return _error(400, "limit must be at least 1")

        if self.audit is None:
            return Response(
                200,
                {"decisions": [], "detail": "no audit log is attached to this control plane"},
            )

        agent_id = (query.get("agent") or "").strip()
        rows = [
            {
                "ts": event.ts,
                "agent_id": event.subject,
                "tool": event.detail.get("tool", ""),
                "effect": event.detail.get("effect", ""),
                "reason": event.detail.get("reason", ""),
                "rule": event.detail.get("rule", ""),
                "action": event.detail.get("action", ""),
                "correlation_id": event.correlation_id,
            }
            for event in self.audit.read()
            if event.kind == "policy.decision"
            and (not agent_id or event.subject == agent_id)
        ]
        rows.reverse()  # newest first
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["effect"]] = counts.get(row["effect"], 0) + 1
        return Response(200, {"decisions": rows[:limit], "counts": counts, "total": len(rows)})

    # -- budget ---------------------------------------------------------------

    def budget(self) -> Response:
        """Limits and reported usage, with the two kept structurally apart.

        ``controls`` are things that stop an agent. ``observed`` are measurements that do
        not. They are separate keys rather than one list with a flag, because a flag is
        easy to drop in a UI and a missing key is not — and presenting a measurement as a
        ceiling is the specific failure this route is shaped to prevent.
        """
        identity = self.bundle.identity
        # Enforcement is the runtime's claim, not the platform's assumption.
        facts = {fact.key: fact for fact in self.runtime.limit_facts()}

        controls: list[dict[str, Any]] = []
        advisory: list[dict[str, Any]] = []
        recorded: list[dict[str, Any]] = []

        for spec in self.bundle.agents:
            limits = spec.limits.to_dict()
            display = identity.display_name_for(spec.id, spec.name)

            def _row(key: str, value: Any) -> dict[str, Any]:
                fact = facts.get(key)
                return {
                    "agent_id": spec.id,
                    "display_name": display,
                    "key": key,
                    "value": value,
                    "enforcement": fact.enforcement if fact else "observed_only",
                    "summary": fact.summary if fact else "",
                    "compiles_to": fact.compiles_to if fact else "",
                }

            for key, value in limits.items():
                if key == "delegation":
                    for sub_key, sub_value in value.items():
                        row = _row(f"delegation.{sub_key}", sub_value)
                        (controls if row["enforcement"] in ENFORCING_CLASSES else recorded).append(row)
                    continue
                row = _row(key, value)
                if row["enforcement"] in ENFORCING_CLASSES:
                    controls.append(row)
                elif row["enforcement"] == "soft_advisory":
                    advisory.append(row)
                else:
                    recorded.append(row)

        observed = [self.runtime.usage(spec.id).to_dict() for spec in self.bundle.agents]

        return Response(
            200,
            {
                # Things that actually stop an agent.
                "controls": controls,
                # Asks the agent to finish. Not a limit.
                "advisory": advisory,
                # Carried to the runtime but not enforced by anything NOVA controls.
                "recorded": recorded,
                # Measurements. Never a ceiling.
                "observed": observed,
                "observed_caveat": (
                    "Reported usage is observation, not a limit. No plugin can veto a model "
                    "call in this runtime, figures lag a background writer, and costs are "
                    "the runtime's estimate rather than an invoice."
                ),
                "enforcement_classes": [fact.to_dict() for fact in self.runtime.limit_facts()],
            },
        )
