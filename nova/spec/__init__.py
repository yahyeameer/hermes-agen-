"""Declarative NOVA specifications: agents, identity, organization, tenant bundles."""

from nova.spec.agent import (
    AgentSpec,
    ApprovalSpec,
    DelegationLimits,
    DelegationSpec,
    KnowledgeSpec,
    LimitsSpec,
    ModelSpec,
    ToolsSpec,
)
from nova.spec.bundle import TenantBundle, load_bundle
from nova.spec.identity import IdentitySpec, SupportSpec, ThemeSpec
from nova.spec.organization import OrganizationSpec

__all__ = [
    "AgentSpec",
    "ApprovalSpec",
    "DelegationLimits",
    "DelegationSpec",
    "IdentitySpec",
    "KnowledgeSpec",
    "LimitsSpec",
    "ModelSpec",
    "OrganizationSpec",
    "SupportSpec",
    "TenantBundle",
    "ThemeSpec",
    "ToolsSpec",
    "load_bundle",
]
