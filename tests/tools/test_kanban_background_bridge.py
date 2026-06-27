"""Tests for the optional Kanban background-process bridge subscriber."""

from contextlib import contextmanager

from tools.kanban_background_bridge import (
    kanban_background_process_subscriber,
    kanban_bridge_enabled,
)
from tools.process_registry import BackgroundProcessEvent, ProcessSession


def test_kanban_background_bridge_defaults_off(monkeypatch):
    """Generic Hermes background processes should not create Kanban cards by default."""
    monkeypatch.delenv("HERMES_KANBAN_TRACK_BACKGROUND", raising=False)
    import hermes_cli.config as config

    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {}})

    assert kanban_bridge_enabled() is False


def test_kanban_background_bridge_config_can_enable(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TRACK_BACKGROUND", raising=False)
    import hermes_cli.config as config

    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"kanban": {"track_background_processes": True}},
    )

    assert kanban_bridge_enabled() is True


def test_kanban_background_bridge_env_can_enable(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TRACK_BACKGROUND", "1")

    assert kanban_bridge_enabled() is True


def test_kanban_background_bridge_env_can_disable(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TRACK_BACKGROUND", "0")

    assert kanban_bridge_enabled() is False


def test_kanban_background_bridge_finishes_already_exited_session(monkeypatch):
    """Fast processes can exit before their Kanban card is created; close the card."""
    calls = []

    class _Conn:
        def execute(self, query, params=()):
            calls.append(("execute", query, params))

    @contextmanager
    def _connect_closing():
        yield _Conn()

    @contextmanager
    def _write_txn(conn):
        yield conn

    def _create_task(conn, **kwargs):
        calls.append(("create", kwargs))
        return "t_bgfast"

    def _complete_task(conn, task_id, **kwargs):
        calls.append(("complete", task_id, kwargs))
        return True

    monkeypatch.setenv("HERMES_KANBAN_TRACK_BACKGROUND", "1")
    monkeypatch.setattr("hermes_cli.kanban_db.connect_closing", _connect_closing)
    monkeypatch.setattr("hermes_cli.kanban_db.write_txn", _write_txn)
    monkeypatch.setattr("hermes_cli.kanban_db.create_task", _create_task)
    monkeypatch.setattr("hermes_cli.kanban_db.complete_task", _complete_task)

    session = ProcessSession(
        id="proc_fast",
        command="python -c 'print(1)'",
        exited=True,
        exit_code=0,
    )

    kanban_background_process_subscriber(
        BackgroundProcessEvent(kind="started", session=session)
    )

    assert session.kanban_task_id == "t_bgfast"
    assert any(call[0] == "create" for call in calls)
    assert any(
        call[0] == "complete"
        and call[1] == "t_bgfast"
        and call[2]["result"] == "exit_code=0"
        for call in calls
    )
