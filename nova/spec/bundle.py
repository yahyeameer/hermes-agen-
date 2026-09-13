"""TenantBundle — a directory of declarations loaded and cross-checked as one unit.

Layout::

    <bundle>/
      organization.yaml     required — who this deployment serves
      identity.yaml         optional — white-label surface; defaults apply when absent
      policy.yaml           optional — business actions, permissions, enforcement defaults
      knowledge.yaml        optional — the corpora agents may be granted
      agents/*.yaml         one AgentSpec per file
      prompts/*.md          persona files referenced by agents

The whole bundle is validated before anything is written anywhere. A bundle that names
a missing teammate or a duplicate agent id fails at load, not halfway through
materialising a runtime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from nova.errors import SpecError
from nova.knowledge.sources import KnowledgeCatalog, load_catalog
from nova.spec.agent import AgentSpec
from nova.spec.identity import IdentitySpec
from nova.policy.model import PolicySpec
from nova.spec.organization import OrganizationSpec

ORGANIZATION_FILE = "organization.yaml"
IDENTITY_FILE = "identity.yaml"
POLICY_FILE = "policy.yaml"
KNOWLEDGE_FILE = "knowledge.yaml"
AGENTS_DIR = "agents"


def _load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"could not be read: {exc.strerror or exc}", source=path) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"is not valid YAML: {exc}", source=path) from exc


@dataclass(frozen=True)
class TenantBundle:
    """One tenant's complete declared configuration."""

    root: Path
    organization: OrganizationSpec
    identity: IdentitySpec
    agents: tuple[AgentSpec, ...]
    #: Absent from a bundle means "no policy declared", which is different from an empty
    #: one: no policy means no enforcement plugin is installed and agents behave exactly
    #: as they did before governance existed.
    policy: Optional[PolicySpec] = None
    #: Declared corpora. An empty catalog means no agent gets a knowledge tool — the same
    #: "absent is a valid state" rule the policy follows.
    knowledge: KnowledgeCatalog = field(default_factory=KnowledgeCatalog)

    @property
    def tenant_id(self) -> str:
        return self.organization.tenant_id

    def agent(self, agent_id: str) -> AgentSpec:
        for spec in self.agents:
            if spec.id == agent_id:
                return spec
        known = ", ".join(sorted(spec.id for spec in self.agents)) or "(none)"
        raise SpecError(f"no agent {agent_id!r} in this bundle; known agents: {known}")

    def enabled_agents(self) -> tuple[AgentSpec, ...]:
        return tuple(spec for spec in self.agents if spec.enabled)

    def digest(self) -> str:
        """Content hash of the whole bundle — every agent plus identity and org."""
        canonical = json.dumps(
            {
                "organization": self.organization.to_dict(),
                "identity": self.identity.to_dict(),
                "policy": self.policy.to_dict() if self.policy else None,
                "knowledge": self.knowledge.to_dict(),
                "agents": [spec.to_dict() for spec in sorted(self.agents, key=lambda s: s.id)],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "digest": self.digest(),
            "organization": self.organization.to_dict(),
            "identity": self.identity.to_dict(),
            "policy": self.policy.to_dict() if self.policy else None,
            "knowledge": self.knowledge.to_dict(),
            "agents": [spec.to_dict() for spec in self.agents],
        }


def load_bundle(root: Path | str, *, env: Optional[Mapping[str, str]] = None) -> TenantBundle:
    """Load and fully validate a tenant bundle.

    Raises :class:`~nova.errors.SpecError` on the first problem, naming the file and
    field. Nothing outside the bundle directory is read or written.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        raise SpecError(f"tenant bundle directory not found: {root}")

    org_path = root / ORGANIZATION_FILE
    if not org_path.is_file():
        raise SpecError(f"{ORGANIZATION_FILE} is required", source=root)
    organization = OrganizationSpec.parse(_load_yaml(org_path) or {}, source=org_path, env=env)

    identity_path = root / IDENTITY_FILE
    if identity_path.is_file():
        identity = IdentitySpec.parse(_load_yaml(identity_path) or {}, source=identity_path, env=env)
    else:
        identity = IdentitySpec()

    policy_path = root / POLICY_FILE
    policy = (
        PolicySpec.parse(_load_yaml(policy_path) or {}, source=policy_path, env=env)
        if policy_path.is_file()
        else None
    )

    knowledge = load_catalog(root, env=env)

    agents = _load_agents(root, env=env)
    _check_cross_references(agents, identity)
    _check_knowledge_references(agents, knowledge)
    if policy is not None:
        _check_policy_references(agents, policy)

    return TenantBundle(
        root=root,
        organization=organization,
        identity=identity,
        agents=agents,
        policy=policy,
        knowledge=knowledge,
    )


def _check_knowledge_references(
    agents: tuple[AgentSpec, ...], knowledge: KnowledgeCatalog
) -> None:
    """Fail on an agent granted a corpus the tenant never declared.

    Failing here rather than at materialization is the point. A typo in a source id would
    otherwise produce an agent whose knowledge tool searches one corpus instead of two, with
    nothing anywhere saying so — it would simply answer worse, and look like a retrieval
    quality problem for as long as it took someone to re-read the YAML.
    """
    for spec in agents:
        unknown = [name for name in spec.knowledge.sources if name not in knowledge.ids]
        if unknown:
            raise SpecError(
                f"names knowledge source(s) the tenant has not declared: "
                f"{', '.join(sorted(unknown))}; declared in "
                f"{KNOWLEDGE_FILE}: {', '.join(sorted(knowledge.ids)) or '(none)'}",
                field="knowledge.sources",
                source=spec.source,
            )


def _load_agents(root: Path, *, env: Optional[Mapping[str, str]]) -> tuple[AgentSpec, ...]:
    agents_dir = root / AGENTS_DIR
    if not agents_dir.is_dir():
        raise SpecError(f"{AGENTS_DIR}/ is required and must contain at least one agent", source=root)

    paths = sorted(
        path
        for path in agents_dir.iterdir()
        if path.is_file() and path.suffix in (".yaml", ".yml")
    )
    if not paths:
        raise SpecError(f"{AGENTS_DIR}/ contains no .yaml files", source=agents_dir)

    agents: list[AgentSpec] = []
    seen: dict[str, Path] = {}
    for path in paths:
        spec = AgentSpec.parse(_load_yaml(path) or {}, source=path, base_dir=root, env=env)
        if spec.id in seen:
            raise SpecError(
                f"duplicate agent id {spec.id!r} — already declared in {seen[spec.id].name}",
                field="id",
                source=path,
            )
        seen[spec.id] = path
        agents.append(spec)
    return tuple(agents)


def _check_cross_references(agents: tuple[AgentSpec, ...], identity: IdentitySpec) -> None:
    """Fail on references between documents that cannot resolve."""
    known = {spec.id for spec in agents}

    for spec in agents:
        unknown = [target for target in spec.delegation.may_assign_to if target not in known]
        if unknown:
            raise SpecError(
                f"names unknown agent(s): {', '.join(sorted(unknown))}; "
                f"known agents: {', '.join(sorted(known))}",
                field="delegation.may_assign_to",
                source=spec.source,
            )

    branded_unknown = sorted(set(identity.agent_display_names) - known)
    if branded_unknown:
        raise SpecError(
            f"names unknown agent(s): {', '.join(branded_unknown)}; "
            f"known agents: {', '.join(sorted(known))}",
            field="agents",
            source=identity.source,
        )


def _check_policy_references(agents: tuple[AgentSpec, ...], policy: PolicySpec) -> None:
    """Fail on policy declarations that cannot resolve.

    A permission that grants nothing, or an approval requirement nothing can trigger, is a
    governance control that silently does not exist — the worst kind, because it looks
    present in a review.
    """
    seen_tools: dict[str, str] = {}
    for action in policy.actions.values():
        for tool in action.tools:
            if tool in seen_tools:
                raise SpecError(
                    f"tool {tool!r} is claimed by both {seen_tools[tool]!r} and "
                    f"{action.name!r}; one tool performs one business action",
                    field=f"actions.{action.name}.tools",
                    source=policy.source,
                )
            seen_tools[tool] = action.name

    for spec in agents:
        unknown = [name for name in spec.permissions if name not in policy.permissions]
        if unknown:
            raise SpecError(
                f"names permission(s) the tenant policy does not define: "
                f"{', '.join(sorted(unknown))}; defined: "
                f"{', '.join(sorted(policy.permissions)) or '(none)'}",
                field="permissions",
                source=spec.source,
            )
        unknown_actions = [
            name for name in spec.approval.required_for if name not in policy.actions
        ]
        if unknown_actions:
            raise SpecError(
                f"requires approval for action(s) the tenant policy does not define: "
                f"{', '.join(sorted(unknown_actions))}; defined: "
                f"{', '.join(sorted(policy.actions)) or '(none)'}",
                field="approval.required_for",
                source=spec.source,
            )
