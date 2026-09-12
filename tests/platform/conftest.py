"""Shared fixtures for NOVA platform tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from nova.audit import AuditLog
from nova.runtime.hermes import HermesRuntime
from nova.spec import load_bundle

EXAMPLE_BUNDLE = Path(__file__).resolve().parents[2] / "nova" / "examples" / "acme"


@pytest.fixture
def bundle():
    return load_bundle(EXAMPLE_BUNDLE)


@pytest.fixture
def home(tmp_path) -> Path:
    """An empty runtime home."""
    root = tmp_path / "home"
    root.mkdir()
    return root


@pytest.fixture
def audit(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl", tenant_id="acme", actor="test")


@pytest.fixture
def runtime(home) -> HermesRuntime:
    return HermesRuntime(home=home, tenant_id="acme")
