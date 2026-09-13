"""The runtime adapter contract.

Deliberately expressed in NOVA's vocabulary and no one else's. There is no ``profile``
in this interface, because a profile is a Hermes concept; there is no ``session``,
because that is a DSH concept. An adapter maps NOVA's *agent* onto whatever its runtime
calls the same idea.

Two rules keep this boundary honest, and both are checked by
``tests/platform/test_runtime_contract.py``:

1. Nothing in this module may import a runtime.
2. No runtime-specific vocabulary may appear in the names here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from nova.audit import AuditLog
from nova.errors import RuntimeAdapterError
from nova.policy import CompiledPolicy, agent_digest
from nova.knowledge.sources import KnowledgeCatalog
from nova.spec import AgentSpec, IdentitySpec
from nova.spec.deployment import DeploymentSpec


@dataclass(frozen=True)
class RuntimeCapabilities:
    """What a runtime can actually do.

    NOVA asks rather than assumes. A capability a runtime lacks is reported here so the
    platform can degrade honestly — refusing the operation, or warning — instead of
    silently producing an agent that will not behave as its spec says.

    The fields are chosen to be the ones that differ between real runtimes, and each is
    a property a workforce deployment depends on.
    """

    #: Work survives a process restart and runs with no interactive session attached.
    durable_tasks: bool = False
    #: Concurrent agents get isolated working directories.
    worktree_isolation: bool = False
    #: Each agent runs in its own OS process.
    process_isolation: bool = False
    #: Each agent has its own credential scope.
    credential_isolation: bool = False
    #: Per-agent tool restriction is enforced by the runtime.
    tool_scoping: bool = False
    #: An agent can search a declared corpus from inside a run. False means a declared
    #: knowledge source is carried and recorded but never reaches a model.
    knowledge_retrieval: bool = False
    #: The runtime can pull text out of formats beyond UTF-8 (PDF, Office, OpenDocument),
    #: through :meth:`AgentRuntime.extract_text`. Ingestion still works without it — it
    #: just skips what it cannot read, and says which files it skipped and why.
    document_extraction: bool = False
    #: NOVA can place work on the runtime's own board, with dependencies between items.
    #: False means an objective can be planned and routed but never submitted.
    work_submission: bool = False
    #: Per-agent tool policy is enforced inside the runtime, including escalation of
    #: business actions to a human. False means a declared policy would be inert.
    policy_enforcement: bool = False
    #: Branding can be projected into the runtime's own display surfaces.
    brand_projection: bool = False

    def missing_for(self, required: Sequence[str]) -> list[str]:
        """Which of ``required`` this runtime does not provide."""
        return [name for name in required if not getattr(self, name, False)]


#: NOVA's canonical work states. Deliberately a small, generic set — a runtime whose own
#: vocabulary is richer maps into these and keeps its native value in
#: :attr:`TaskView.runtime_status`, so the UI is portable and nothing is lost.
TASK_STATES = ("pending", "running", "blocked", "review", "done", "archived")


@dataclass(frozen=True)
class TaskView:
    """One unit of work, as the control plane sees it.

    A read model: it is assembled for display and never written back. ``runtime_status``
    carries the runtime's own word for the state, because an operator debugging a stuck
    task needs the runtime's vocabulary, not a translation of it.
    """

    task_id: str
    title: str
    state: str
    runtime_status: str = ""
    agent_id: str = ""
    created_at: Optional[int] = None
    started_at: Optional[int] = None
    completed_at: Optional[int] = None
    priority: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    tenant_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        """Blocked, or failing repeatedly. What an operator should look at first."""
        return self.state == "blocked" or self.consecutive_failures > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "state": self.state,
            "runtime_status": self.runtime_status,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "priority": self.priority,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "needs_attention": self.needs_attention,
        }


@dataclass(frozen=True)
class RuntimeHealth:
    """Whether the runtime is present and readable.

    ``reachable`` false is a normal state, not an error: a fresh deployment has no work
    store until the runtime first runs. The control plane reports that plainly rather
    than failing.
    """

    reachable: bool
    detail: str = ""
    work_store_present: bool = False
    agent_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "detail": self.detail,
            "work_store_present": self.work_store_present,
            "agent_count": self.agent_count,
        }


@dataclass(frozen=True)
class ModelUsage:
    """Reported usage for one model."""

    model: str
    provider: str = ""
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float = 0.0
    actual_cost_usd: float = 0.0
    cost_status: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "actual_cost_usd": round(self.actual_cost_usd, 6),
            "cost_status": self.cost_status,
        }


@dataclass(frozen=True)
class UsageSummary:
    """One agent's reported usage, with its own caveats attached.

    ``enforcement`` is fixed at ``observed_only`` and is part of the payload so no
    consumer can render these numbers as a budget without contradicting the data it was
    handed.
    """

    agent_id: str
    available: bool
    models: tuple[ModelUsage, ...] = ()
    detail: str = ""
    enforcement: str = "observed_only"
    #: Why these figures must not be treated as a ceiling or an invoice.
    caveats: tuple[str, ...] = field(
        default_factory=lambda: (
            "Not a spending limit: no plugin can veto a model call in this runtime.",
            "Lagging: usage is written by a coalescing background thread.",
            "Estimated: reconcile against the provider's billing before charging anyone.",
        )
    )

    @property
    def total_tokens(self) -> int:
        return sum(usage.total_tokens for usage in self.models)

    @property
    def estimated_cost_usd(self) -> float:
        return sum(usage.estimated_cost_usd for usage in self.models)

    @property
    def api_calls(self) -> int:
        return sum(usage.api_calls for usage in self.models)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "available": self.available,
            "detail": self.detail,
            "enforcement": self.enforcement,
            "caveats": list(self.caveats),
            "api_calls": self.api_calls,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "models": [usage.to_dict() for usage in self.models],
        }


@dataclass(frozen=True)
class ExtractedDocument:
    """One document's text, as pulled out of whatever format it arrived in."""

    path: str
    text: str
    extracted: bool
    #: How the text was obtained: ``native`` (plain text read by NOVA) or the adapter's
    #: own extractor. Recorded so an operator can tell a real PDF extraction from a
    #: fallback that read the bytes as text.
    method: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "extracted": self.extracted,
            "method": self.method,
            "detail": self.detail,
            "characters": len(self.text),
        }


@dataclass(frozen=True)
class WorkItem:
    """One unit of work NOVA is asking a runtime to schedule.

    ``depends_on`` names other items by their NOVA ``key``, not by any runtime id: the
    caller does not know what the runtime will call them, and in a dry run there are no
    runtime ids at all. The adapter resolves keys to its own identifiers as it goes, which
    is why items arrive in dependency order.

    ``key`` is also the idempotency key. Submitting the same objective twice must produce
    one set of work items, not two — a supervisor that duplicates a month-end close on a
    retry is worse than one that does nothing.
    """

    key: str
    title: str
    assignee: str
    body: str = ""
    depends_on: tuple[str, ...] = ()
    priority: int = 0
    max_runtime_seconds: Optional[int] = None
    max_retries: Optional[int] = None
    #: Ask the runtime to decompose this item rather than run it as a single unit. An
    #: adapter with no decomposer treats it as an ordinary item and says so in the result.
    decompose: bool = False
    tenant_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "assignee": self.assignee,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "decompose": self.decompose,
        }


@dataclass(frozen=True)
class SubmittedItem:
    """One work item as the runtime now holds it."""

    key: str
    task_id: str
    created: bool
    assignee: str = ""
    state: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "task_id": self.task_id,
            "created": self.created,
            "assignee": self.assignee,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SubmitResult:
    """What a submission did."""

    items: tuple[SubmittedItem, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = False

    @property
    def created(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items if item.created)

    @property
    def existing(self) -> tuple[str, ...]:
        """Items a previous submission already created. Re-submitting is a no-op, by design."""
        return tuple(item.key for item in self.items if not item.created)

    def task_id(self, key: str) -> str:
        for item in self.items:
            if item.key == key:
                return item.task_id
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "created": list(self.created),
            "existing": list(self.existing),
            "items": [item.to_dict() for item in self.items],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class MaterializedAgent:
    """An agent as it currently exists inside a runtime.

    ``digest`` is the spec digest NOVA recorded when it wrote this agent; comparing it
    with a freshly loaded spec's digest detects drift — either the bundle changed, or
    someone edited the runtime directly.
    """

    agent_id: str
    display_name: str
    enabled: bool
    digest: str = ""
    location: Optional[Path] = None
    managed_by_nova: bool = True
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MaterializeResult:
    """What a materialization actually did."""

    agent_id: str
    created: bool
    changed: bool
    digest: str
    paths_written: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    location: Optional[Path] = None

    @property
    def unchanged(self) -> bool:
        return not self.created and not self.changed

    def to_detail(self) -> dict[str, Any]:
        """Audit-friendly summary."""
        return {
            "created": self.created,
            "changed": self.changed,
            "paths_written": [str(p) for p in self.paths_written],
            "warnings": list(self.warnings),
            "location": str(self.location) if self.location else "",
        }


class AgentRuntime(ABC):
    """Adapter onto one agent runtime.

    Implementations own every runtime-specific decision — where agents live on disk,
    what their configuration is called, how branding reaches the display. Callers get
    the same five operations regardless.
    """

    #: Short stable identifier, e.g. ``"hermes"``.
    name: str = ""

    @property
    @abstractmethod
    def capabilities(self) -> RuntimeCapabilities:
        """What this runtime supports. Never assume; always ask."""

    @abstractmethod
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
        """Create or update one agent inside the runtime.

        Must be idempotent: materializing an unchanged spec twice leaves the runtime
        identical and reports ``unchanged``. Must route the change through
        ``audit.model_visible_change`` — an agent's instructions and tools are
        model-visible by definition.

        ``policy`` is the agent's compiled policy, or None when the tenant declares none.
        An adapter that cannot enforce policy must say so through
        :attr:`RuntimeCapabilities.policy_enforcement` rather than accepting it silently.

        ``knowledge`` is the tenant's whole declared catalog, not this agent's slice of it.
        The adapter narrows it by ``spec.knowledge.sources`` and installs whatever retrieval
        mechanism it has. Passing the catalog rather than a resolved grant keeps the caller
        free of any assumption about how a corpus reaches an agent — a file beside a
        profile here, something else entirely in the next adapter.
        """

    def expected_digest(
        self,
        spec: AgentSpec,
        *,
        policy: Optional[CompiledPolicy] = None,
        knowledge: Optional[KnowledgeCatalog] = None,
    ) -> str:
        """The digest :meth:`materialize_agent` would record for this spec, without writing.

        The drift check and the materializer must agree on what an agent's identity *is*, and
        only the adapter knows everything that goes into it — an adapter that compiles extra
        runtime-specific state into an agent has to fold that state in here too. Two
        definitions of this would disagree the moment one of them grew a field, and the
        symptom is an agent that reports "out of sync" forever and re-materializes on every
        apply.

        The default covers the portable inputs; :func:`nova.policy.agent_digest` is the one
        implementation underneath.
        """
        return agent_digest(spec, policy)

    @abstractmethod
    def list_agents(self) -> list[MaterializedAgent]:
        """Every agent present in the runtime, NOVA-managed or not.

        Includes unmanaged ones deliberately: an operator needs to see what is there,
        and NOVA needs to refuse to overwrite what it does not own.
        """

    @abstractmethod
    def remove_agent(
        self, agent_id: str, *, audit: AuditLog, correlation_id: str, dry_run: bool = False
    ) -> bool:
        """Remove a NOVA-managed agent. Returns False when it was not present.

        Must refuse to remove an agent NOVA did not create, and must never delete
        customer data such as credentials or conversation history.
        """

    @abstractmethod
    def apply_identity(
        self,
        identity: IdentitySpec,
        *,
        audit: AuditLog,
        correlation_id: str,
        dry_run: bool = False,
    ) -> MaterializeResult:
        """Project branding into the runtime's display surfaces."""

    @abstractmethod
    def list_tasks(self, *, agent_id: str = "", limit: int = 200) -> list[TaskView]:
        """Work the runtime currently holds, newest first.

        A read model over whatever the runtime uses to track work. Implementations must
        open that store read-only: the control plane observes, it does not write.
        Returns an empty list when the runtime has no work store yet.
        """

    @abstractmethod
    def get_task(self, task_id: str) -> Optional[TaskView]:
        """One task, or None when it is not present."""

    @abstractmethod
    def health(self) -> RuntimeHealth:
        """Whether the runtime is present and readable."""

    def extract_text(self, path: Path) -> ExtractedDocument:
        """Pull readable text out of a document.

        Document extraction is a *runtime capability*: a mature runtime already handles
        PDF, Office and OpenDocument formats, and re-implementing that would be weeks of
        work and a permanent liability. Adapters therefore expose theirs here rather than
        the platform carrying its own.

        The default reads UTF-8 text and nothing else, so a runtime that offers no
        extractor still ingests plain text and markdown instead of failing.
        """
        try:
            return ExtractedDocument(
                path=str(path),
                text=path.read_text(encoding="utf-8"),
                extracted=True,
                method="native",
            )
        except (OSError, UnicodeDecodeError) as exc:
            return ExtractedDocument(
                path=str(path),
                text="",
                extracted=False,
                method="native",
                detail=f"not readable as UTF-8 text and this runtime offers no extractor: {exc}",
            )

    def submit_work(
        self,
        items: Sequence[WorkItem],
        *,
        audit: AuditLog,
        correlation_id: str,
        dry_run: bool = False,
    ) -> SubmitResult:
        """Place work on the runtime's board, in dependency order.

        Items arrive already ordered so every item's dependencies precede it; an adapter
        resolves each ``depends_on`` key to the identifier it minted moments earlier.

        Must be idempotent on ``WorkItem.key``. Re-submitting an objective after a partial
        failure has to finish it, not duplicate the half that succeeded.

        Creating work is model-visible — a worker will read the title and body this places
        on the board — so an implementation routes it through
        ``audit.model_visible_change``.

        The default refuses. An adapter that cannot schedule work must not silently accept
        a plan and drop it; it reports ``work_submission=False`` and this is what happens
        if something calls it anyway.
        """
        raise RuntimeAdapterError(
            f"runtime {self.name!r} cannot accept submitted work "
            "(capabilities.work_submission is False)"
        )

    @abstractmethod
    def usage(self, agent_id: str) -> UsageSummary:
        """Reported model usage for one agent.

        **Observation, not control.** An implementation must not present these figures as
        a budget: this runtime family cannot veto a model call, so a token or cost ceiling
        is not enforceable. The returned summary carries that caveat in its own payload.
        """

    @property
    @abstractmethod
    def state_location(self) -> Path:
        """Where this runtime keeps the state NOVA materializes into.

        Named in NOVA's terms because every runtime has one; what it contains and how it
        is laid out is the adapter's business and nobody else's.
        """

    def deployment_readiness(
        self,
        spec: AgentSpec,
        deployment: Optional[DeploymentSpec] = None,
    ) -> dict[str, Any]:
        """Which credentials this agent still needs, and where they must go.

        On the contract because the answer is runtime-shaped — *where* an operator puts a
        credential differs per runtime — while the question is not. A platform that could
        only ask this of one adapter would report a second runtime as permanently ready.

        The default reports nothing required, which is the honest answer for an adapter that
        does not resolve credentials at all.
        """
        return {
            "agent_id": spec.id,
            "ready": True,
            "required": [],
            "missing": [],
            "warnings": [],
            "provider": {},
        }

    @property
    def knowledge_index_path(self) -> Path:
        """Where this runtime's deployment keeps the corpus index.

        On the contract rather than computed by callers, for the same reason
        :attr:`state_location` is: a CLI or a control plane that built this path itself
        would be encoding one adapter's directory layout, and would quietly point at the
        wrong file the first time an adapter laid its state out differently.
        """
        return self.state_location / "nova-knowledge.db"

    def limit_facts(self) -> tuple:
        """What each NOVA limit actually does on this runtime.

        Enforcement is a claim about a runtime, not about a limit, so each adapter
        declares its own. The default is empty: an adapter that says nothing is taken to
        enforce nothing, which is the safe reading.
        """
        return ()

    def describe(self) -> dict[str, Any]:
        """Adapter summary for operator tooling and the Control API."""
        return {
            "runtime": self.name,
            "state_location": str(self.state_location),
            "capabilities": {
                key: getattr(self.capabilities, key)
                for key in RuntimeCapabilities.__dataclass_fields__
            },
        }
