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
    )


#: Columns read. Named explicitly rather than ``SELECT *`` so a schema change upstream
#: surfaces as a clear error here instead of silently reshaping the view.
_COLUMNS = (
    "id, title, status, assignee, created_at, started_at, completed_at, priority, "
    "consecutive_failures, last_failure_error, tenant"
)


def list_tasks(home: Path, *, agent_id: str = "", limit: int = 200) -> list[TaskView]:
    """Tasks the runtime holds, newest first. Empty when there is no work store yet."""
    limit = max(1, min(int(limit), 1000))
    with _readonly(work_store_path(home)) as connection:
        if connection is None:
            return []
        query = f"SELECT {_COLUMNS} FROM tasks"  # noqa: S608 — fixed column list, no user input
        params: list[object] = []
        if agent_id:
            query += " WHERE assignee = ?"
            params.append(agent_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        try:
            rows = connection.execute(query, params).fetchall()
        except sqlite3.Error:
            return []
    return [_row_to_task(row) for row in rows]


def get_task(home: Path, task_id: str) -> Optional[TaskView]:
    with _readonly(work_store_path(home)) as connection:
        if connection is None:
            return None
        try:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE id = ?",  # noqa: S608 — fixed columns
                (task_id,),
            ).fetchone()
        except sqlite3.Error:
            return None
    return _row_to_task(row) if row is not None else None


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
