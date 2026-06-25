"""Tests for the event-driven task_done notification hook in kanban_db.

This replaces the polling cron notifier
(``jarvis_kanban_completion_idle_retry.py``): completing a task fires a
single, best-effort, configurable command hook — no polling, no dedup
state file.
"""
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_recorder(tmp_path: Path) -> tuple[Path, str]:
    """Write a tiny notifier script and return (output_path, command).

    The script appends one JSON line per invocation capturing the
    ``HERMES_KANBAN_DONE_*`` env vars, so the test can assert exactly-once
    delivery and the humanized payload.
    """
    out = tmp_path / "notifications.jsonl"
    script = tmp_path / "recorder.py"
    script.write_text(
        "import json, os, sys\n"
        "rec = {k: os.environ.get(k) for k in "
        "('HERMES_KANBAN_DONE_TASK_ID','HERMES_KANBAN_DONE_TITLE',"
        "'HERMES_KANBAN_DONE_ASSIGNEE','HERMES_KANBAN_DONE_SUMMARY')}\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(rec, ensure_ascii=False) + '\\n')\n"
    )
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {shlex.quote(str(out))}"
    return out, cmd


def _wait_for_lines(path: Path, n: int, timeout: float = 5.0) -> list[dict]:
    """Poll for the fire-and-forget child to flush ``n`` JSON records."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if len(lines) >= n:
                return [json.loads(l) for l in lines]
        time.sleep(0.05)
    lines = (
        [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if path.exists()
        else []
    )
    return [json.loads(l) for l in lines]


# ---------------------------------------------------------------------------
# Humanizer (no DB needed)
# ---------------------------------------------------------------------------

def test_humanize_bg_claude_code_command():
    title = (
        "[bg] bash ~/Jarvis/.hermes/scripts/jarvis_claude_code_runner.sh "
        "-p 'fix the foo bug in bar'"
    )
    human = kb._humanize_task_title(title)
    assert "[bg]" not in human
    assert "jarvis_claude_code_runner.sh" not in human
    assert human.startswith("Claude Code 后台任务")
    assert "fix the foo bug" in human


def test_humanize_passthrough_plain_title():
    assert kb._humanize_task_title("Write the release notes") == "Write the release notes"


def test_humanize_env_prefixed_command():
    title = "HERMES_HOME=/x/.hermes bash /opt/run/jarvis_claude_code_runner.sh"
    assert kb._humanize_task_title(title) == "Claude Code 后台任务"


# ---------------------------------------------------------------------------
# Delivery via complete_task
# ---------------------------------------------------------------------------

def test_complete_task_fires_single_notification(kanban_home, tmp_path, monkeypatch):
    out, cmd = _make_recorder(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DONE_NOTIFY_CMD", cmd)
    monkeypatch.setenv("HERMES_KANBAN_DONE_NOTIFY_IN_TESTS", "1")

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="[bg] bash /x/jarvis_claude_code_runner.sh -p 'fix the login crash'",
            assignee="worker1",
        )
        assert kb.complete_task(conn, tid, result="done — patched auth.py\nmore detail")
    finally:
        conn.close()

    records = _wait_for_lines(out, 1)
    assert len(records) == 1, f"expected exactly one notification, got {records}"
    rec = records[0]
    assert rec["HERMES_KANBAN_DONE_TASK_ID"] == tid
    assert rec["HERMES_KANBAN_DONE_ASSIGNEE"] == "worker1"
    assert rec["HERMES_KANBAN_DONE_TITLE"].startswith("Claude Code 后台任务")
    assert "[bg]" not in rec["HERMES_KANBAN_DONE_TITLE"]
    # Summary is first line of result only.
    assert rec["HERMES_KANBAN_DONE_SUMMARY"] == "done — patched auth.py"


def test_no_notification_when_cmd_unset(kanban_home, tmp_path, monkeypatch):
    out, _cmd = _make_recorder(tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DONE_NOTIFY_CMD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DONE_NOTIFY_IN_TESTS", "1")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="plain task", assignee="w")
        assert kb.complete_task(conn, tid, result="ok")
    finally:
        conn.close()

    assert not out.exists() or out.read_text() == ""


def test_skipped_under_pytest_without_optin(kanban_home, tmp_path, monkeypatch):
    """With cmd set but the in-tests opt-in absent, the hook must no-op.

    PYTEST_CURRENT_TEST is always present while the suite runs, so this
    guards against the test runner ever emitting real notifications.
    """
    out, cmd = _make_recorder(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DONE_NOTIFY_CMD", cmd)
    monkeypatch.delenv("HERMES_KANBAN_DONE_NOTIFY_IN_TESTS", raising=False)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="plain task", assignee="w")
        assert kb.complete_task(conn, tid, result="ok")
    finally:
        conn.close()

    time.sleep(0.3)
    assert not out.exists() or out.read_text() == ""


def test_config_driven_command_fires_notification(kanban_home, tmp_path, monkeypatch):
    out, cmd = _make_recorder(tmp_path)
    cfg = {
        "kanban": {
            "done_notify": {
                "enabled": True,
                "command": cmd,
                "allow_in_tests": True,
            }
        }
    }
    monkeypatch.setattr(kb, "_done_notify_config", lambda: cfg["kanban"]["done_notify"])
    monkeypatch.delenv("HERMES_KANBAN_DONE_NOTIFY_CMD", raising=False)

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="plain task", assignee="w")
        assert kb.complete_task(conn, tid, result="ok")
    finally:
        conn.close()

    records = _wait_for_lines(out, 1)
    assert len(records) == 1
    assert records[0]["HERMES_KANBAN_DONE_TASK_ID"] == tid
    assert records[0]["HERMES_KANBAN_DONE_TITLE"] == "plain task"


def test_completing_already_done_task_does_not_refire(kanban_home, tmp_path, monkeypatch):
    out, cmd = _make_recorder(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_DONE_NOTIFY_CMD", cmd)
    monkeypatch.setenv("HERMES_KANBAN_DONE_NOTIFY_IN_TESTS", "1")

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="plain task", assignee="w")
        assert kb.complete_task(conn, tid, result="first")
        # Second call: task is already 'done', rowcount==0, returns False.
        assert kb.complete_task(conn, tid, result="second") is False
    finally:
        conn.close()

    records = _wait_for_lines(out, 1)
    assert len(records) == 1, f"a second complete must not re-notify, got {records}"
