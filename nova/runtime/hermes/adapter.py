"""HermesRuntime — the AgentRuntime implementation for the Hermes runtime."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from nova.audit import AuditLog
from nova.errors import RuntimeAdapterError
from nova.knowledge.sources import KnowledgeCatalog
from nova.spec.deployment import DeploymentSpec
from nova.runtime.hermes.skin import build_skin, skin_filename
from nova.runtime.base import (
    AgentRuntime,
    MaterializedAgent,
    MaterializeResult,
    RuntimeCapabilities,
    ExtractedDocument,
    RuntimeHealth,
    SubmitResult,
    TaskView,
    UsageSummary,
    WorkItem,
)
from nova.runtime.hermes import materialize as _materialize
from nova.runtime.hermes import submit as _submit
from nova.runtime.hermes import readiness as _readiness
from nova.runtime.hermes import provider as _provider
from nova.runtime.hermes import extract as _extract
from nova.runtime.hermes import usage as _usage
from nova.runtime.hermes.limits import LIMIT_FACTS
from nova.runtime.hermes import work as _work
from nova.runtime.hermes.paths import HermesPaths
from nova.policy import CompiledPolicy
from nova.spec import AgentSpec, IdentitySpec

#: What the Hermes runtime provides, as established by the Phase 0 audit.
#:
#: ``tool_scoping`` is True because tool DENIALS compile to the runtime's unconditional
#: deny list and are genuinely enforced ahead of any bypass. Positive scoping
#: (toolsets/allow) is not yet compiled — see ``materialize.warnings_for`` for why — and
#: the materializer warns whenever a spec declares it.
#: ``knowledge_retrieval`` is True: a granted agent gets a ``knowledge_search`` tool
#: installed as a per-agent plugin, scoped in SQL to the corpora its tenant granted it.
#: Plugin toolsets are enabled by default (``hermes_cli/tools_config.py``), so the tool
#: reaches the model without NOVA writing ``platform_toolsets`` — the key it deliberately
#: does not touch.
#: ``document_extraction`` is True because the runtime ships extractors for PDF, Office and
#: OpenDocument formats, which NOVA borrows rather than reimplements (see ``extract.py``).
#: ``work_submission`` is True: NOVA creates tasks through ``kanban_db.create_task`` — the
#: runtime's own API for its own shared board, which is a different database from the
#: conversation and credential state on ``materialize.NEVER_WRITE``.
#: ``policy_enforcement`` is True: policy compiles to a plugin on the runtime's documented
#: pre-tool-call hook, which vetoes a call or escalates it to the same human gate that
#: guards dangerous shell commands — and that gate fails closed with no human present.
HERMES_CAPABILITIES = RuntimeCapabilities(
    durable_tasks=True,
    worktree_isolation=True,
    process_isolation=True,
    credential_isolation=True,
    tool_scoping=True,
    knowledge_retrieval=True,
    document_extraction=True,
    work_submission=True,
    policy_enforcement=True,
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

    def limit_facts(self) -> tuple:
        return LIMIT_FACTS

    # -- agents ---------------------------------------------------------------

    def materialize_agent(
        self,
        spec: AgentSpec,
        *,
        audit: AuditLog,
        correlation_id: str,
        identity: Optional[IdentitySpec] = None,
        policy: Optional[CompiledPolicy] = None,
        knowledge: Optional[KnowledgeCatalog] = None,
        deployment: Optional[DeploymentSpec] = None,
        dry_run: bool = False,
    ) -> MaterializeResult:
        deployment = deployment or DeploymentSpec()
        resolved = deployment.provider.merged_with(spec.model.deployment)
        runtime_config = deployment.runtime_config
        grant = _materialize.build_knowledge_config(
            spec,
            self.paths,
            knowledge,
            tenant_id=audit.tenant_id,
            audit_log=audit.path,
        )
        if dry_run:
            # A dry run changes nothing, so it is not a model-visible change. It is
            # still recorded: knowing what an operator previewed is useful during an
            # incident, and a plain record() carries no intent/commit pair.
            result = _materialize.materialize(
                spec, self.paths, identity=identity, policy=policy, knowledge=grant,
                provider=resolved, runtime_config=runtime_config, dry_run=True,
            )
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
            digest=_materialize._combined_digest(spec, policy, grant),
            detail={
                "runtime": self.name,
                "agent_name": spec.name,
                "policy": bool(policy),
                "knowledge_sources": [
                    entry["id"] for entry in (grant or {}).get("sources", [])
                ],
            },
        ) as outcome:
            result = _materialize.materialize(
                spec, self.paths, identity=identity, policy=policy, knowledge=grant,
                provider=resolved, runtime_config=runtime_config, dry_run=False,
            )
            outcome.update(result.to_detail())
        return result

    def submit_work(
        self,
        items: Sequence[WorkItem],
        *,
        audit: AuditLog,
        correlation_id: str,
        dry_run: bool = False,
    ) -> SubmitResult:
        detail = {
            "runtime": self.name,
            "items": len(items),
            "assignees": sorted({item.assignee for item in items}),
        }
        if dry_run:
            result = _submit.submit(self.paths.home, items, dry_run=True)
            audit.record(
                "work.submit_preview",
                correlation_id=correlation_id,
                subject=items[0].key.split(":")[0] if items else "",
                detail={**detail, **result.to_dict()},
            )
            return result

        with audit.model_visible_change(
            "work.submitted",
            correlation_id=correlation_id,
            subject=items[0].key.split(":")[0] if items else "",
            detail=detail,
        ) as outcome:
            result = _submit.submit(self.paths.home, items, dry_run=False)
            outcome.update(result.to_dict())
        return result

    def deployment_readiness(
        self,
        spec: AgentSpec,
        deployment: Optional[DeploymentSpec] = None,
    ) -> dict[str, Any]:
        deployment = deployment or DeploymentSpec()
        resolved = deployment.provider.merged_with(spec.model.deployment)
        provider_config, warnings = _provider.build_provider_config(resolved)
        required = _provider.required_env(resolved, dict(deployment.runtime_config or {}))
        report = _readiness.check(
            spec.id, required, profile_dir=self.paths.profile_dir(spec.id)
        ).to_dict()
        # Warnings ride along rather than being fetched separately: a caller that had to
        # import the adapter to ask for them would be encoding which runtime it is talking
        # to, which is the one thing the contract exists to prevent.
        report["warnings"] = warnings
        report["provider"] = resolved.to_dict()
        return report

    @property
    def knowledge_index_path(self) -> Path:
        return self.paths.knowledge_index

    def expected_digest(
        self,
        spec: AgentSpec,
        *,
        policy: Optional[CompiledPolicy] = None,
        knowledge: Optional[KnowledgeCatalog] = None,
    ) -> str:
        """As the contract, plus this agent's resolved knowledge grant.

        The grant is resolved rather than taken from the spec because the corpus titles the
        tool description carries come from the tenant catalog, not from the agent.
        """
        grant = _materialize.build_knowledge_config(spec, self.paths, knowledge)
        return _materialize._combined_digest(spec, policy, grant)

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

    def extract_text(self, path: Path) -> ExtractedDocument:
        """Delegates to the runtime's extractor; see :mod:`nova.runtime.hermes.extract`."""
        return _extract.extract(path)

    def usage(self, agent_id: str) -> UsageSummary:
        """Reported usage for one agent. Observation only — see the returned caveats."""
        return _usage.read_usage(self.paths.profile_dir(agent_id), agent_id)

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
