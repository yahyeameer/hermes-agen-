"""Fold an agent's spec and the tenant policy into one compiled document.

Compilation is where declarations become enforceable. It is also where contradictions
surface: an agent denying a tool its own permission grants, or denying a baseline tool it
needs to report work. Those are reported as warnings at build time, because the
alternative is discovering them when a task runs and never closes.

The compiled document is deliberately a plain dictionary of tool names — no NOVA types,
no indirection — so the enforcement point inside the runtime can read it with nothing but
the standard library.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from nova.policy.decide import POLICY_SCHEMA_VERSION
from nova.policy.model import PolicySpec
from nova.spec import AgentSpec

#: Fields a runtime adapter injects into a compiled document at materialization time.
#: They are plumbing, not meaning: moving the audit log does not change what an agent is
#: allowed to do, so they are excluded from the agent's identity.
RUNTIME_INJECTED_KEYS = frozenset({"audit_log", "tenant_id"})


@dataclass(frozen=True)
class CompiledPolicy:
    """One agent's enforceable policy, plus what compiling it revealed."""

    agent_id: str
    document: dict[str, Any]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_allowlist(self) -> bool:
        return bool(self.document.get("allow"))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.document)


def compile_policy(spec: AgentSpec, policy: PolicySpec) -> CompiledPolicy:
    """Compile ``spec`` against ``policy``.

    An agent that declares neither permissions nor an allow-list runs without an
    allow-list, so Phase 1 bundles keep working unchanged: adding governance must not
    silently restrict agents that predate it.
    """
    warnings: list[str] = []

    denied = set(spec.tools.deny)
    baseline = set(policy.baseline_tools)

    # Tools this agent is granted, from its permissions and its explicit allow list.
    granted = policy.tools_for_permissions(spec.permissions) | set(spec.tools.allow)

    unknown_permissions = [name for name in spec.permissions if name not in policy.permissions]
    if unknown_permissions:
        warnings.append(
            f"permission(s) {', '.join(sorted(unknown_permissions))} are not defined in the "
            "tenant policy, so they grant nothing"
        )

    # Actions this agent must escalate: the tenant default, plus its own additions.
    approval_actions: dict[str, list[str]] = {}
    for name, action in policy.actions.items():
        required = action.requires_approval or name in spec.approval.required_for
        if required:
            approval_actions[name] = sorted(action.tools)

    unknown_actions = [
        name for name in spec.approval.required_for if name not in policy.actions
    ]
    if unknown_actions:
        warnings.append(
            f"approval is required for {', '.join(sorted(unknown_actions))}, which the tenant "
            "policy does not define as an action — nothing will trigger it. Define the action "
            "and the tools that perform it"
        )

    # Contradictions worth surfacing before they reach a running agent.
    denied_baseline = sorted(denied & baseline)
    if denied_baseline:
        warnings.append(
            f"denies baseline tool(s) {', '.join(denied_baseline)} — this agent may be unable "
            "to report its own task outcome, leaving work that runs and never closes"
        )

    denied_granted = sorted(denied & granted)
    if denied_granted:
        warnings.append(
            f"denies {', '.join(denied_granted)} which its own permissions grant — deny wins, "
            "so the grant has no effect"
        )

    denied_approval = sorted(
        denied.intersection(*[set(t) for t in approval_actions.values()])
        if approval_actions
        else set()
    )
    if denied_approval:
        warnings.append(
            f"denies {', '.join(denied_approval)}, which is also marked for approval — it will "
            "be denied outright and never reach a human"
        )

    document: dict[str, Any] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "agent_id": spec.id,
        "deny": sorted(denied),
        "baseline": sorted(baseline),
        "approval_actions": {name: approval_actions[name] for name in sorted(approval_actions)},
        "allow": sorted(granted),
        "unlisted_tool": policy.unlisted_tool,
    }
    return CompiledPolicy(agent_id=spec.id, document=document, warnings=tuple(warnings))


def policy_identity(document: dict[str, Any]) -> dict[str, Any]:
    """The part of a compiled document that defines what an agent may do."""
    return {key: value for key, value in document.items() if key not in RUNTIME_INJECTED_KEYS}


def agent_digest(spec: AgentSpec, policy: Optional[CompiledPolicy]) -> str:
    """Stable identity of an agent as materialized: its spec plus its policy.

    The single definition, used by the materializer when it writes provenance and by the
    control plane when it reports drift. Two definitions would disagree, and the
    disagreement would show as an agent permanently "out of sync".
    """
    if policy is None:
        return spec.digest()
    payload = json.dumps(
        {"spec": spec.to_dict(), "policy": policy_identity(policy.document)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
