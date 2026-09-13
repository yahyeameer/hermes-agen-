"""Compile an AgentSpec into a Hermes profile directory.

A NOVA agent becomes a Hermes profile: a directory the runtime already understands,
containing configuration, a persona, and a NOVA provenance file.

Three safety properties, each tested:

**NOVA writes only what it owns.** Every profile NOVA creates carries
``nova-agent.json``. A profile without one was created by someone else, and
materializing over it is refused rather than silently overwriting a hand-built agent.

**Customer data is never touched.** Credentials, session history, memories and
databases live in the same directory and are on :data:`NEVER_WRITE`. The writer refuses
to touch those names even if a future change tries to.

**Writes are atomic per file.** Each file is written to a temporary sibling and renamed,
so a crash leaves either the old file or the new one, never half of either.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from nova.errors import RuntimeAdapterError
from nova.policy import CompiledPolicy, agent_digest
from nova.runtime.base import MaterializeResult
from nova.runtime.hermes.paths import HermesPaths
from nova.spec import AgentSpec, IdentitySpec

#: Filenames inside a profile that belong to the customer or the runtime. NOVA never
#: creates, modifies or deletes these. Mirrors the runtime's own user-owned exclusions.
NEVER_WRITE: frozenset[str] = frozenset(
    {
        "auth.json",
        ".env",
        "state.db",
        "state.db-shm",
        "state.db-wal",
        "hermes_state.db",
        "response_store.db",
        "memories",
        "sessions",
        "logs",
        "workspace",
        "checkpoints",
        "backups",
        "local",
    }
)

#: Written into every NOVA-managed profile. Its presence is the ownership claim.
PROVENANCE_FILENAME = "nova-agent.json"
PROVENANCE_VERSION = 1

#: The plugin's own manifest, shipped beside this module.
PLUGIN_MANIFEST = Path(__file__).parent / "plugin_manifest.yaml"
#: The hook entry point, copied verbatim into each agent's plugin directory.
PLUGIN_ENTRY = Path(__file__).parent / "enforcement.py"
#: The pure decision module, copied beside it so the same code decides in both places.
PLUGIN_DECIDE = Path(__file__).parents[2] / "policy" / "decide.py"


@dataclass(frozen=True)
class Provenance:
    """NOVA's record of what it wrote and from which spec."""

    version: int
    agent_id: str
    digest: str
    nova_version: str

    @classmethod
    def read(cls, path: Path) -> Optional["Provenance"]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt marker still proves NOVA created the profile; treat it as owned
            # but with an unknown digest so the next materialize rewrites it.
            return cls(version=0, agent_id="", digest="", nova_version="")
        return cls(
            version=int(data.get("version", 0)),
            agent_id=str(data.get("agent_id", "")),
            digest=str(data.get("digest", "")),
            nova_version=str(data.get("nova_version", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "agent_id": self.agent_id,
            "digest": self.digest,
            "nova_version": self.nova_version,
            "_comment": (
                "Written by NOVA. This file marks the profile as NOVA-managed; "
                "deleting it makes NOVA refuse to update the profile."
            ),
        }


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, refusing protected names."""
    if path.name in NEVER_WRITE:
        raise RuntimeAdapterError(
            f"refusing to write {path.name!r}: it holds customer or runtime state that "
            "NOVA does not own"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def build_config(spec: AgentSpec) -> dict[str, Any]:
    """The runtime ``config.yaml`` body for one agent.

    Only keys the agent's spec actually sets are emitted, so the runtime's own defaults
    continue to apply everywhere the customer did not express an opinion. An empty
    section is omitted rather than written as ``{}``, which the runtime would treat as
    an explicit empty value.
    """
    config: dict[str, Any] = {}

    model: dict[str, Any] = {}
    if spec.model.name:
        model["model"] = spec.model.name
    if spec.model.provider:
        model["provider"] = spec.model.provider
    if model:
        config["model"] = model

    # -- HARD, PRE-EMPTIVE limits ------------------------------------------
    # Each key below was verified at the runtime call site that reads it; see
    # docs/platform/BUDGET_ENFORCEMENT_AUDIT.md and nova/policy/limits.py.
    agent_section: dict[str, Any] = {}
    if spec.limits.max_turns is not None:
        # cli.py::_init_turn_limits -> conversation_loop.py:1514 loop condition.
        agent_section["max_turns"] = spec.limits.max_turns
    if spec.limits.soft_wrapup_after_seconds is not None:
        # SOFT: conversation_loop.py:119 injects a wrap-up message. Nothing terminates.
        agent_section["run_budget_seconds"] = spec.limits.soft_wrapup_after_seconds
    if spec.model.reasoning_effort:
        agent_section["reasoning_effort"] = spec.model.reasoning_effort
    if agent_section:
        config["agent"] = agent_section

    # tools/delegate_tool_config.py::_load_config reads this block through
    # load_config_readonly(), which follows HERMES_HOME and therefore the profile.
    delegation = spec.limits.delegation
    delegation_section: dict[str, Any] = {}
    if delegation.max_concurrent_children is not None:
        delegation_section["max_concurrent_children"] = delegation.max_concurrent_children
    if delegation.max_depth is not None:
        delegation_section["max_spawn_depth"] = delegation.max_depth
    if delegation.max_child_turns is not None:
        delegation_section["max_iterations"] = delegation.max_child_turns
    if delegation.child_timeout_seconds is not None:
        delegation_section["child_timeout_seconds"] = delegation.child_timeout_seconds
    if delegation.orchestrator_enabled is not None:
        delegation_section["orchestrator_enabled"] = delegation.orchestrator_enabled
    if delegation_section:
        config["delegation"] = delegation_section

    # Tool DENIALS compile to the runtime's own unconditional deny list, which is
    # evaluated before any bypass mode. That is the strongest expression available
    # without touching runtime code, and it is genuinely enforced.
    if spec.tools.deny:
        config["approvals"] = {"deny": [f"{name}*" for name in spec.tools.deny]}

    # Positive tool scoping (toolsets / allow) is deliberately NOT compiled yet.
    #
    # The runtime resolves an agent's toolset from ``platform_toolsets[<surface>]``,
    # not from a top-level key. Writing a narrowed list there would strip the kanban
    # tools a dispatched worker needs to report completion, leaving tasks that run and
    # then never close. A restriction that silently breaks task reporting is worse than
    # one that is honestly reported as not yet enforced, so the declaration is preserved
    # under ``nova:`` and the materializer warns. See ``warnings_for`` below.

    kanban: dict[str, Any] = {}
    if spec.limits.max_concurrent_tasks is not None:
        kanban["max_in_progress_per_profile"] = spec.limits.max_concurrent_tasks
    if kanban:
        config["kanban"] = kanban

    # Declared-but-not-yet-enforced sections are recorded under a NOVA-owned key so the
    # runtime ignores them while the information survives for the policy and knowledge
    # phases. Writing them nowhere would lose the customer's stated intent.
    declared: dict[str, Any] = {}
    if spec.tools.toolsets:
        declared["toolsets"] = list(spec.tools.toolsets)
    if spec.tools.allow:
        declared["tools_allow"] = list(spec.tools.allow)
    if spec.permissions:
        declared["permissions"] = list(spec.permissions)
    if spec.approval.required_for:
        declared["approval_required_for"] = list(spec.approval.required_for)
    if spec.knowledge.sources:
        declared["knowledge_sources"] = list(spec.knowledge.sources)
    if spec.delegation.may_assign_to:
        declared["may_assign_to"] = list(spec.delegation.may_assign_to)
    # RECORDED ONLY: the dispatcher enforces these as task columns, which NOVA cannot set
    # while it does not create tasks. Preserved so the intent survives to the phase that
    # can act on it — never presented as enforced.
    if spec.limits.max_task_runtime_seconds is not None:
        declared["max_task_runtime_seconds"] = spec.limits.max_task_runtime_seconds
    if spec.limits.max_retries is not None:
        declared["max_retries"] = spec.limits.max_retries
    if declared:
        config["nova"] = declared

    return config


def warnings_for(spec: AgentSpec) -> list[str]:
    """Honest reporting of what this adapter records but does not yet enforce."""
    notes: list[str] = []
    if spec.tools.toolsets or spec.tools.allow:
        notes.append(
            "positive tool scoping (toolsets/allow) is recorded but not yet enforced by the "
            "hermes adapter; tool denials in tools.deny ARE enforced"
        )
    if not spec.enabled:
        notes.append(
            "agent is disabled in its spec; its profile is written but nothing should dispatch to it"
        )
    return notes


def build_persona(spec: AgentSpec, identity: Optional[IdentitySpec]) -> str:
    """The agent's ``SOUL.md`` — its voice, with tenant branding applied.

    The display name comes from identity when branded, so the agent introduces itself as
    the customer's product rather than as its internal id.
    """
    display = spec.name
    if identity is not None:
        display = identity.display_name_for(spec.id, spec.name)

    lines: list[str] = []
    if spec.instructions:
        lines.append(spec.instructions)
    else:
        product = identity.product_name if identity else ""
        opening = f"You are {display}"
        if product:
            opening += f", part of {product}"
        lines.append(opening + ".")
        if spec.description:
            lines.append("")
            lines.append(spec.description)
    return "\n".join(lines).strip() + "\n"


def plan_writes(
    spec: AgentSpec,
    paths: HermesPaths,
    identity: Optional[IdentitySpec],
    policy: Optional[CompiledPolicy] = None,
) -> dict[Path, str]:
    """Every file this materialization would write, as path -> content.

    Separated from the writing so a dry run reports exactly what a real run would do,
    rather than approximating it.
    """
    config_text = yaml.safe_dump(build_config(spec), sort_keys=True, default_flow_style=False)
    provenance = Provenance(
        version=PROVENANCE_VERSION,
        agent_id=spec.id,
        # The recorded digest must cover everything materialization depends on, policy
        # included, or a re-apply compares against a digest it can never match and
        # reports every agent as changed forever.
        digest=_combined_digest(spec, policy),
        nova_version=_nova_version(),
    )
    writes = {
        paths.config_path(spec.id): config_text,
        paths.persona_path(spec.id): build_persona(spec, identity),
        paths.provenance_path(spec.id): json.dumps(provenance.to_dict(), indent=2) + "\n",
    }

    # The enforcement plugin is installed ONLY when the tenant declares a policy. Without
    # one there is nothing to enforce, and installing a plugin that would deny everything
    # on a missing document would break every agent that predates governance.
    if policy is not None:
        plugin_dir = paths.plugin_dir(spec.id)
        writes[paths.policy_path(spec.id)] = json.dumps(policy.document, indent=2, sort_keys=True) + "\n"
        writes[plugin_dir / "plugin.yaml"] = PLUGIN_MANIFEST.read_text(encoding="utf-8")
        writes[plugin_dir / "__init__.py"] = PLUGIN_ENTRY.read_text(encoding="utf-8")
        writes[plugin_dir / "_decide.py"] = PLUGIN_DECIDE.read_text(encoding="utf-8")

    return writes


def _nova_version() -> str:
    from nova import __version__

    return __version__


def materialize(
    spec: AgentSpec,
    paths: HermesPaths,
    *,
    identity: Optional[IdentitySpec] = None,
    policy: Optional[CompiledPolicy] = None,
    dry_run: bool = False,
) -> MaterializeResult:
    """Create or update one agent's profile. Idempotent.

    Refuses to write into a profile directory that exists without NOVA provenance.
    """
    profile_dir = paths.profile_dir(spec.id)
    provenance_path = paths.provenance_path(spec.id)
    existing = Provenance.read(provenance_path)
    warnings: list[str] = []

    if profile_dir.exists() and existing is None:
        raise RuntimeAdapterError(
            f"profile directory {profile_dir} already exists but was not created by NOVA "
            f"(no {PROVENANCE_FILENAME}). Refusing to overwrite it — remove or rename the "
            "directory if you want NOVA to manage this agent."
        )

    created = not profile_dir.exists()
    # The policy is part of what an agent IS, so it belongs in the identity that decides
    # whether a re-apply is a change. A policy edit with an unchanged spec must rewrite.
    digest = _combined_digest(spec, policy)
    writes = plan_writes(spec, paths, identity, policy)

    if existing is not None and existing.digest == digest and not created:
        # Still verify the files are actually present: a deleted SOUL.md with a stale
        # marker would otherwise be reported as up to date.
        if all(path.is_file() for path in writes):
            return MaterializeResult(
                agent_id=spec.id,
                created=False,
                changed=False,
                digest=digest,
                location=profile_dir,
            )
        warnings.append("provenance was current but files were missing; rewriting")

    warnings.extend(warnings_for(spec))
    if policy is not None:
        warnings.extend(policy.warnings)

    if dry_run:
        return MaterializeResult(
            agent_id=spec.id,
            created=created,
            changed=not created,
            digest=digest,
            paths_written=tuple(sorted(writes)),
            warnings=tuple(warnings),
            location=profile_dir,
        )

    for path, text in sorted(writes.items()):
        atomic_write(path, text)

    return MaterializeResult(
        agent_id=spec.id,
        created=created,
        changed=not created,
        digest=digest,
        paths_written=tuple(sorted(writes)),
        warnings=tuple(warnings),
        location=profile_dir,
    )


def _combined_digest(spec: AgentSpec, policy: Optional[CompiledPolicy]) -> str:
    """Delegates to the one shared definition; see :func:`nova.policy.agent_digest`."""
    return agent_digest(spec, policy)
