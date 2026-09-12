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
