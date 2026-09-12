"""HermesRuntime — the AgentRuntime implementation for the Hermes runtime."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping, Optional

import yaml

from nova.audit import AuditLog
from nova.errors import RuntimeAdapterError
from nova.runtime.hermes.skin import build_skin, skin_filename
from nova.runtime.base import (
    AgentRuntime,
    MaterializedAgent,
    MaterializeResult,
    RuntimeCapabilities,
    RuntimeHealth,
    TaskView,
)
from nova.runtime.hermes import materialize as _materialize
from nova.runtime.hermes import work as _work
from nova.runtime.hermes.paths import HermesPaths
from nova.spec import AgentSpec, IdentitySpec

#: What the Hermes runtime provides, as established by the Phase 0 audit.
#:
#: ``tool_scoping`` is True because tool DENIALS compile to the runtime's unconditional
#: deny list and are genuinely enforced ahead of any bypass. Positive scoping
#: (toolsets/allow) is not yet compiled — see ``materialize.warnings_for`` for why — and
#: the materializer warns whenever a spec declares it.
#: ``knowledge_retrieval`` is False because no runtime has it yet; the spec field is
#: carried through, never silently dropped.
HERMES_CAPABILITIES = RuntimeCapabilities(
    durable_tasks=True,
    worktree_isolation=True,
    process_isolation=True,
    credential_isolation=True,
    tool_scoping=True,
    knowledge_retrieval=False,
    brand_projection=True,
)


class HermesRuntime(AgentRuntime):
    """Adapts NOVA onto Hermes.

    A NOVA agent is a Hermes profile; NOVA identity is a Hermes skin. Nothing here
    imports a Hermes module: the adapter works through the runtime's on-disk extension
    points, which is what keeps the core patch budget at zero.
    """

    name = "hermes"

    def __init__(
        self,
        *,
        home: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        tenant_id: str = "",
    ) -> None:
        self.paths = HermesPaths(home=Path(home)) if home is not None else HermesPaths.resolve(env)
        self.tenant_id = tenant_id

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return HERMES_CAPABILITIES

    @property
    def state_location(self) -> Path:
        return self.paths.home

    # -- agents ---------------------------------------------------------------

    def materialize_agent(
        self,
        spec: AgentSpec,
        *,
        audit: AuditLog,
        correlation_id: str,
        identity: Optional[IdentitySpec] = None,
        dry_run: bool = False,
    ) -> MaterializeResult:
        if dry_run:
            # A dry run changes nothing, so it is not a model-visible change. It is
            # still recorded: knowing what an operator previewed is useful during an
            # incident, and a plain record() carries no intent/commit pair.
            result = _materialize.materialize(spec, self.paths, identity=identity, dry_run=True)
            audit.record(
                "agent.materialize_preview",
                correlation_id=correlation_id,
                subject=spec.id,
                digest=result.digest,
                detail=result.to_detail(),
            )
            return result

        with audit.model_visible_change(
            "agent.materialized",
            correlation_id=correlation_id,
            subject=spec.id,
            digest=spec.digest(),
            detail={"runtime": self.name, "agent_name": spec.name},
        ) as outcome:
            result = _materialize.materialize(spec, self.paths, identity=identity, dry_run=False)
            outcome.update(result.to_detail())
        return result

    def list_agents(self) -> list[MaterializedAgent]:
        profiles_dir = self.paths.profiles_dir
        if not profiles_dir.is_dir():
            return []

        agents: list[MaterializedAgent] = []
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir():
                continue
            provenance = _materialize.Provenance.read(
                self.paths.provenance_path(entry.name)
            )
            agents.append(
                MaterializedAgent(
                    agent_id=entry.name,
                    display_name=self._display_name(entry.name),
                    enabled=True,
                    digest=provenance.digest if provenance else "",
                    location=entry,
                    managed_by_nova=provenance is not None,
                    detail={"runtime": self.name},
                )
            )
        return agents

    def _display_name(self, agent_id: str) -> str:
        """Best-effort display name read back from the materialized profile."""
        config_path = self.paths.config_path(agent_id)
        if config_path.is_file():
            try:
                data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                return agent_id
            nova_section = data.get("nova")
            if isinstance(nova_section, dict):
                name = nova_section.get("display_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return agent_id

    def remove_agent(
        self, agent_id: str, *, audit: AuditLog, correlation_id: str, dry_run: bool = False
    ) -> bool:
        profile_dir = self.paths.profile_dir(agent_id)
        if not profile_dir.is_dir():
            return False

        provenance = _materialize.Provenance.read(self.paths.provenance_path(agent_id))
        if provenance is None:
            raise RuntimeAdapterError(
                f"profile {profile_dir} was not created by NOVA; refusing to remove it"
            )

        protected = sorted(
            child.name
            for child in profile_dir.iterdir()
            if child.name in _materialize.NEVER_WRITE
        )
        if protected:
            raise RuntimeAdapterError(
                f"profile {profile_dir} holds customer state ({', '.join(protected)}); "
                "refusing to remove it. Move that data out first if the agent is truly finished."
            )

        if dry_run:
            audit.record(
                "agent.remove_preview",
                correlation_id=correlation_id,
                subject=agent_id,
                detail={"runtime": self.name, "location": str(profile_dir)},
            )
            return True

        with audit.model_visible_change(
            "agent.removed",
            correlation_id=correlation_id,
            subject=agent_id,
            digest=provenance.digest,
            detail={"runtime": self.name, "location": str(profile_dir)},
        ):
            shutil.rmtree(profile_dir)
        return True

    # -- work read models -----------------------------------------------------

    def list_tasks(self, *, agent_id: str = "", limit: int = 200) -> list[TaskView]:
        return _work.list_tasks(self.paths.home, agent_id=agent_id, limit=limit)

    def get_task(self, task_id: str) -> Optional[TaskView]:
        return _work.get_task(self.paths.home, task_id)

    def health(self) -> RuntimeHealth:
        present, detail = _work.store_status(self.paths.home)
        home_exists = self.paths.home.is_dir()
        return RuntimeHealth(
            reachable=home_exists,
            detail=detail if home_exists else f"runtime home {self.paths.home} does not exist",
            work_store_present=present,
            agent_count=len(self.list_agents()),
        )

    # -- identity -------------------------------------------------------------

    def apply_identity(
        self,
        identity: IdentitySpec,
        *,
        audit: AuditLog,
        correlation_id: str,
        dry_run: bool = False,
    ) -> MaterializeResult:
        tenant = self.tenant_id or "nova"
        skin = build_skin(identity, tenant_id=tenant)
        target = self.paths.skins_dir / skin_filename(tenant)
        text = yaml.safe_dump(skin, sort_keys=True, default_flow_style=False, allow_unicode=True)

        existing = target.read_text(encoding="utf-8") if target.is_file() else None
        changed = existing != text

        if dry_run:
            audit.record(
                "identity.apply_preview",
                correlation_id=correlation_id,
                subject=tenant,
                detail={"runtime": self.name, "path": str(target), "changed": changed},
            )
            return MaterializeResult(
                agent_id=tenant,
                created=existing is None,
                changed=changed and existing is not None,
                digest="",
                paths_written=(target,),
                location=target,
            )

        if not changed:
            return MaterializeResult(
                agent_id=tenant, created=False, changed=False, digest="", location=target
            )

        with audit.model_visible_change(
            "identity.applied",
            correlation_id=correlation_id,
            subject=tenant,
            detail={"runtime": self.name, "product_name": identity.product_name},
        ) as outcome:
            _materialize.atomic_write(target, text)
            outcome.update({"path": str(target)})

        return MaterializeResult(
            agent_id=tenant,
            created=existing is None,
            changed=existing is not None,
            digest="",
            paths_written=(target,),
            location=target,
        )
