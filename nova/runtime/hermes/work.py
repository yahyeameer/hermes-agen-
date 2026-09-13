"""Read-only view of the work the Hermes runtime is tracking.

The control plane observes; it never writes here. Every connection is opened with
SQLite's read-only URI mode, so a bug in this module cannot corrupt the runtime's task
store — the strongest guarantee available short of not opening it at all.

The runtime's own status vocabulary is richer than NOVA's canonical states, so
:data:`STATE_MAP` folds it down for display while ``runtime_status`` keeps the original
value. An operator debugging a stuck task needs the runtime's word, not a translation.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from nova.runtime.base import TaskView

#: Runtime status -> NOVA canonical state. An unknown status maps to ``pending`` and
#: keeps its native value, so a runtime that adds a status does not break this view.
STATE_MAP = {
    "triage": "pending",
    "todo": "pending",
    "scheduled": "pending",
    "ready": "pending",
    "running": "running",
    "blocked": "blocked",
    "review": "review",
    "done": "done",
    "archived": "archived",
}

#: Default board's work store, relative to the runtime home.
WORK_STORE_NAME = "kanban.db"


def work_store_path(home: Path) -> Path:
    return home / WORK_STORE_NAME


@contextmanager
def _readonly(path: Path) -> Iterator[Optional[sqlite3.Connection]]:
    """A read-only connection, or None when the store is absent or unreadable.

    Absence is normal on a fresh deployment — the runtime creates the store the first
    time it runs — so this yields None rather than raising.
    """
    if not path.is_file():
        yield None
        return
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        yield connection
    except sqlite3.Error:
        # A locked or malformed store is an operational fact to report, not a crash.
        yield None
    finally:
        if connection is not None:
            connection.close()


def _row_to_task(row: sqlite3.Row) -> TaskView:
    keys = set(row.keys())

    def get(name: str, default=None):
        return row[name] if name in keys else default

    status = str(get("status", "") or "")
    return TaskView(
        task_id=str(get("id", "") or ""),
        title=str(get("title", "") or ""),
        state=STATE_MAP.get(status, "pending"),
        runtime_status=status,
        agent_id=str(get("assignee", "") or ""),
        created_at=get("created_at"),
        started_at=get("started_at"),
        completed_at=get("completed_at"),
        priority=int(get("priority", 0) or 0),
        consecutive_failures=int(get("consecutive_failures", 0) or 0),
        last_error=str(get("last_failure_error", "") or ""),
        tenant_id=str(get("tenant", "") or ""),
        # The supervisor matches its plan steps to board items by this key, never by
        # title: two objectives with a "Review findings" step would otherwise report each
        # other's progress. Carried in ``detail`` because "the caller's own identifier" is
        # a NOVA concept, and putting it on the contract would oblige every future adapter
        # to have one.
        detail={"idempotency_key": str(get("idempotency_key", "") or "")},
    )


#: Columns read, named explicitly rather than ``SELECT *`` so this module says what it
#: depends on. Only ``id``, ``title`` and ``status`` are load-bearing; the rest enrich the
#: view and :func:`_row_to_task` already tolerates any of them being absent.
_WANTED_COLUMNS = (
    "id", "title", "status", "assignee", "created_at", "started_at", "completed_at",
    "priority", "consecutive_failures", "last_failure_error", "tenant", "idempotency_key",
)


def _columns(connection: sqlite3.Connection) -> str:
    """The wanted columns that this store actually has, as a SELECT list.

    Asking rather than assuming, because the alternative fails in the worst available way.
    A fixed list against a store missing one column raises inside the query, and both
    readers below turn a ``sqlite3.Error`` into an empty result — so a single renamed or
    not-yet-added column upstream would render the dashboard as "no tasks at all" rather
    than as an error anyone could act on. Selecting the intersection degrades one field at
    a time instead, which is what the defensive accessors in :func:`_row_to_task` were
    always written for.
    """
    try:
        present = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
    except sqlite3.Error:
        present = set()
    usable = [name for name in _WANTED_COLUMNS if name in present]
    # An empty intersection means this is not a task table at all; let the caller's error
    # handling report an unreadable store rather than emitting `SELECT  FROM tasks`.
    return ", ".join(usable) if usable else "id, title, status"


def list_tasks(
    home: Path, *, agent_id: str = "", limit: int = 200, tenant_id: str = ""
) -> list[TaskView]:
    """Tasks the runtime holds, newest first. Empty when there is no work store yet.

    ``tenant_id`` restricts the result to work stamped for that tenant. An empty value
    means unscoped, which is what a caller with no tenant identity gets — deliberately
    permissive there, because the alternative is a control plane that silently shows
    nothing when its tenant id is simply unset.
    """
    limit = max(1, min(int(limit), 1000))
    with _readonly(work_store_path(home)) as connection:
        if connection is None:
            return []
        available = _columns(connection)
        query = f"SELECT {available} FROM tasks"  # noqa: S608 — names from PRAGMA
        clauses: list[str] = []
        params: list[object] = []
        if agent_id:
            clauses.append("assignee = ?")
            params.append(agent_id)
        # Rows written before the tenant was stamped carry NULL/'' and stay visible: a
        # filter that hid a deployment's own pre-existing work would read as data loss.
        if tenant_id and "tenant" in available:
            clauses.append("(tenant = ? OR tenant IS NULL OR tenant = '')")
            params.append(tenant_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        try:
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
    return [_row_to_task(row) for row in rows]


def get_task(home: Path, task_id: str, *, tenant_id: str = "") -> Optional[TaskView]:
    """One task, or None when it is absent or belongs to another tenant.

    A direct id lookup is the one place a tenant filter matters most: ids are guessable
    from another tenant's dashboard, and "not found" is the right answer to a request for
    someone else's work.
    """
    with _readonly(work_store_path(home)) as connection:
        if connection is None:
            return None
        try:
            row = connection.execute(
                f"SELECT {_columns(connection)} FROM tasks WHERE id = ?",  # noqa: S608
                (task_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
    if row is None:
        return None
    view = _row_to_task(row)
    if tenant_id and view.tenant_id and view.tenant_id != tenant_id:
        return None
    return view


def store_status(home: Path) -> tuple[bool, str]:
    """``(present, detail)`` for the work store, for health reporting."""
    path = work_store_path(home)
    if not path.is_file():
        return False, "no work store yet — the runtime creates it on first run"
    with _readonly(path) as connection:
        if connection is None:
            return False, f"work store present at {path} but could not be opened read-only"
        try:
            connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            return False, f"work store present but unreadable: {exc}"
    return True, ""
