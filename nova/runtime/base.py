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
from nova.spec import AgentSpec, IdentitySpec


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
    #: The runtime can retrieve from a knowledge corpus. False everywhere in Phase 1.
    knowledge_retrieval: bool = False
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
        dry_run: bool = False,
    ) -> MaterializeResult:
        """Create or update one agent inside the runtime.

        Must be idempotent: materializing an unchanged spec twice leaves the runtime
        identical and reports ``unchanged``. Must route the change through
        ``audit.model_visible_change`` — an agent's instructions and tools are
        model-visible by definition.
        """

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

    @property
    @abstractmethod
    def state_location(self) -> Path:
        """Where this runtime keeps the state NOVA materializes into.

        Named in NOVA's terms because every runtime has one; what it contains and how it
        is laid out is the adapter's business and nobody else's.
        """

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
