"""Control API route handlers — tested directly, without a server."""

from __future__ import annotations

import sqlite3
import time

import pytest

from nova.control import ControlAPI
from nova.runtime.hermes.work import work_store_path


@pytest.fixture
def api(bundle, runtime):
    return ControlAPI(bundle, runtime)


def seed_tasks(home, rows):
    """Create a work store shaped like the runtime's own."""
    path = work_store_path(home)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, "
        "assignee TEXT, status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT, "
        "created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, tenant TEXT, "
        "consecutive_failures INTEGER NOT NULL DEFAULT 0, last_failure_error TEXT)"
    )
    now = int(time.time())
    for index, (task_id, title, agent, status, failures) in enumerate(rows):
        connection.execute(
            "INSERT INTO tasks (id,title,assignee,status,created_at,tenant,"
            "consecutive_failures,last_failure_error) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, title, agent, status, now - index, "acme", failures, "boom" if failures else ""),
        )
    connection.commit()
    connection.close()


# -- routing -----------------------------------------------------------------


def test_unknown_route_is_404(api):
    assert api.handle("/nope").status == 404
    assert api.handle("/platform/v1/nope").status == 404


def test_trailing_slash_is_tolerated(api):
    assert api.handle("/platform/v1/health/").status == 200


# -- health ------------------------------------------------------------------


def test_health_reports_runtime_and_bundle(api, bundle):
    body = api.handle("/platform/v1/health").body
    assert body["platform"]["tenant_id"] == "acme"
    assert body["runtime"]["runtime"] == "hermes"
    assert body["bundle"]["digest"] == bundle.digest()


def test_absent_work_store_is_healthy_not_an_error(api):
    """A fresh deployment has no work store. That is normal, not a failure."""
    response = api.handle("/platform/v1/health")
    assert response.status == 200
    assert response.body["runtime"]["work_store_present"] is False
    assert "no work store yet" in response.body["runtime"]["detail"]


# -- identity ----------------------------------------------------------------


def test_identity_serves_resolved_branding(api):
    body = api.handle("/platform/v1/identity").body
    assert body["product_name"] == "Acme Intelligence"
    assert body["theme"]["accent"] == "#1f6f5c"
    assert body["tenant_id"] == "acme"


def test_identity_is_what_makes_one_build_serve_many_brands(runtime, bundle):
    """Swap the identity, and the same code serves a different product name."""
    from dataclasses import replace

    rebranded = ControlAPI(
        replace(bundle, identity=replace(bundle.identity, product_name="Bristol Foods AI")),
        runtime,
    )
    assert rebranded.handle("/platform/v1/identity").body["product_name"] == "Bristol Foods AI"


# -- agents ------------------------------------------------------------------


def test_agents_uses_branded_display_names(api):
    rows = {a["id"]: a for a in api.handle("/platform/v1/agents").body["agents"]}
    assert rows["customer-support"]["display_name"] == "Acme Support Assistant"


def test_agents_reports_unmaterialized(api):
    rows = api.handle("/platform/v1/agents").body["agents"]
    assert all(row["materialized"] is False for row in rows)
    assert all(row["in_sync"] is False for row in rows)


def test_agents_reports_in_sync_after_apply(api, bundle, runtime, audit):
    from nova.apply import apply_bundle

    apply_bundle(bundle, runtime, audit=audit)
    rows = {a["id"]: a for a in api.handle("/platform/v1/agents").body["agents"]}
    assert rows["customer-support"]["in_sync"] is True
    assert rows["customer-support"]["applied_digest"] == bundle.agent("customer-support").digest()


def test_agents_detects_drift(api, bundle, runtime, audit, home):
    """An agent edited in the runtime, or a changed bundle, must be visible as drift."""
    from nova.apply import apply_bundle
    from nova.runtime.hermes.paths import HermesPaths

    apply_bundle(bundle, runtime, audit=audit)
    marker = HermesPaths(home=home).provenance_path("customer-support")
    marker.write_text(marker.read_text(encoding="utf-8").replace("sha256:", "sha256:x"), encoding="utf-8")
    rows = {a["id"]: a for a in api.handle("/platform/v1/agents").body["agents"]}
    assert rows["customer-support"]["in_sync"] is False


def test_undeclared_runtime_agents_are_surfaced(api, home, runtime):
    """An operator needs to see everything that can run, declared or not."""
    from nova.runtime.hermes.paths import HermesPaths

    HermesPaths(home=home).profile_dir("handmade").mkdir(parents=True)
    body = api.handle("/platform/v1/agents").body
    assert [row["id"] for row in body["undeclared"]] == ["handmade"]


# -- tasks -------------------------------------------------------------------


def test_tasks_are_empty_without_a_work_store(api):
    body = api.handle("/platform/v1/tasks").body
    assert body["tasks"] == []
    assert body["counts"] == {}


def test_tasks_map_runtime_status_to_canonical_state(api, home):
    seed_tasks(
        home,
        [
            ("t1", "one", "customer-support", "running", 0),
            ("t2", "two", "operations", "todo", 0),
            ("t3", "three", "operations", "blocked", 2),
            ("t4", "four", "customer-support", "done", 0),
        ],
    )
    body = api.handle("/platform/v1/tasks").body
    assert body["counts"] == {"running": 1, "pending": 1, "blocked": 1, "done": 1}


def test_runtime_status_is_preserved_alongside_the_canonical_state(api, home):
    """An operator debugging a stuck task needs the runtime's own word for it."""
    seed_tasks(home, [("t1", "one", "operations", "triage", 0)])
    task = api.handle("/platform/v1/tasks").body["tasks"][0]
    assert task["state"] == "pending"
    assert task["runtime_status"] == "triage"


def test_needs_attention_counts_blocked_and_failing(api, home):
    seed_tasks(
        home,
        [
            ("t1", "fine", "operations", "running", 0),
            ("t2", "stuck", "operations", "blocked", 0),
            ("t3", "failing", "operations", "running", 3),
        ],
    )
    assert api.handle("/platform/v1/tasks").body["needs_attention"] == 2


def test_tasks_filter_by_agent(api, home):
    seed_tasks(
        home,
        [("t1", "a", "customer-support", "running", 0), ("t2", "b", "operations", "running", 0)],
    )
    body = api.handle("/platform/v1/tasks", {"agent": "operations"}).body
    assert [task["task_id"] for task in body["tasks"]] == ["t2"]


def test_task_limit_is_validated(api):
    assert api.handle("/platform/v1/tasks", {"limit": "abc"}).status == 400
    assert api.handle("/platform/v1/tasks", {"limit": "0"}).status == 400


def test_single_task_lookup(api, home):
    seed_tasks(home, [("t1", "one", "operations", "running", 0)])
    assert api.handle("/platform/v1/tasks/t1").body["task"]["task_id"] == "t1"
    assert api.handle("/platform/v1/tasks/missing").status == 404


def test_unknown_runtime_status_degrades_instead_of_breaking(api, home):
    """A runtime that adds a status must not break the control plane."""
    seed_tasks(home, [("t1", "one", "operations", "some-future-status", 0)])
    task = api.handle("/platform/v1/tasks").body["tasks"][0]
    assert task["state"] == "pending"
    assert task["runtime_status"] == "some-future-status"


def test_work_store_is_opened_read_only(api, home):
    """The control plane observes; it must never be able to write."""
    seed_tasks(home, [("t1", "one", "operations", "running", 0)])
    api.handle("/platform/v1/tasks")
    connection = sqlite3.connect(f"file:{work_store_path(home)}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("UPDATE tasks SET title = 'tampered'")
    connection.close()
