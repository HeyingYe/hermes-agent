"""Optional Kanban subscriber for managed background-process lifecycle events.

This module is intentionally separate from ``tools.process_registry`` so generic
background process tracking does not import Kanban DB code or create board
artifacts unless a Kanban/Jarvis profile opts in via config/env.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


def kanban_bridge_enabled() -> bool:
    """Whether spawned background processes should mirror onto the Kanban board."""
    env = os.environ.get("HERMES_KANBAN_TRACK_BACKGROUND")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        return bool(kcfg.get("track_background_processes", False))
    except Exception:
        return False


def kanban_background_process_subscriber(event: Any) -> None:
    """Mirror opted-in background process lifecycle events to Kanban."""
    if event.kind == "started":
        _kanban_bridge_create(event.session)
    elif event.kind == "finished":
        _kanban_bridge_finish(event.session)


def _kanban_bridge_create(session: Any) -> None:
    """Best-effort: create a visibility-only Kanban card for a background task."""
    if not kanban_bridge_enabled():
        return
    try:
        from hermes_cli import kanban_db as kb

        first_line = (session.command or "").strip().splitlines()[0] if session.command.strip() else ""
        title = "[bg] " + (first_line[:116] or "background process")
        assignee = os.environ.get("HERMES_PROFILE") or "background"
        with kb.connect_closing() as conn:
            task_id = kb.create_task(
                conn,
                title=title,
                body=session.command,
                assignee=assignee,
                created_by=assignee,
                workspace_kind="scratch",
                initial_status="running",
                session_id=session.session_key or None,
                idempotency_key=f"bgproc:{session.id}",
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'running', last_heartbeat_at = ? WHERE id = ?",
                    (int(time.time()), task_id),
                )
        session.kanban_task_id = task_id
        if session.exited:
            _kanban_bridge_finish(session)
    except Exception:
        logger.debug("kanban bridge: create card failed", exc_info=True)


def _kanban_bridge_finish(session: Any) -> None:
    """Best-effort: close the mirrored Kanban card when the process exits."""
    task_id = getattr(session, "kanban_task_id", "")
    if not task_id:
        return
    try:
        from hermes_cli import kanban_db as kb

        code = session.exit_code
        if code in (0, None):
            summary = f"Background process {session.id} exited (code {code})."
        else:
            summary = f"Background process {session.id} exited non-zero (code {code})."
        with kb.connect_closing() as conn:
            kb.complete_task(conn, task_id, summary=summary, result=f"exit_code={code}")
    except Exception:
        logger.debug("kanban bridge: complete card failed", exc_info=True)
