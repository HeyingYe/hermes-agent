"""Focused regressions for the Copilot ACP shim safety layer."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.copilot_acp_client import CopilotACPClient


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()


class CopilotACPClientSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = CopilotACPClient(acp_cwd="/tmp")

    def _dispatch(self, message: dict, *, cwd: str) -> dict:
        process = _FakeProcess()
        handled = self.client._handle_server_message(
            message,
            process=process,
            cwd=cwd,
            text_parts=[],
            reasoning_parts=[],
        )
        self.assertTrue(handled)
        payload = process.stdin.getvalue().strip()
        self.assertTrue(payload)
        return json.loads(payload)

    def test_request_permission_is_not_auto_allowed(self) -> None:
        response = self._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/request_permission",
                "params": {},
            },
            cwd="/tmp",
        )

        outcome = (((response.get("result") or {}).get("outcome") or {}).get("outcome"))
        self.assertEqual(outcome, "cancelled")

    def test_read_text_file_blocks_internal_hermes_hub_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            blocked = home / ".hermes" / "skills" / ".hub" / "index-cache" / "entry.json"
            blocked.parent.mkdir(parents=True, exist_ok=True)
            blocked.write_text('{"token":"sk-test-secret-1234567890"}')

            with patch.dict(
                os.environ,
                {"HOME": str(home), "HERMES_HOME": str(home / ".hermes")},
                clear=False,
            ):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "fs/read_text_file",
                        "params": {"path": str(blocked)},
                    },
                    cwd=str(home),
                )

        self.assertIn("error", response)

    def test_read_text_file_redacts_sensitive_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            secret_file = root / "config.env"
            secret_file.write_text("OPENAI_API_KEY=sk-proj-abc123def456ghi789jkl012")

            # agent.redact snapshots HERMES_REDACT_SECRETS at import time into
            # _REDACT_ENABLED, so patching os.environ is a no-op. Flip the
            # module-level constant directly for the duration of the call.
            with patch("agent.redact._REDACT_ENABLED", True):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "fs/read_text_file",
                        "params": {"path": str(secret_file)},
                    },
                    cwd=str(root),
                )

        content = ((response.get("result") or {}).get("content") or "")
        self.assertNotIn("abc123def456", content)
        self.assertIn("OPENAI_API_KEY=", content)

    def test_write_text_file_reuses_write_denylist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            target = home / ".ssh" / "id_rsa"
            target.parent.mkdir(parents=True, exist_ok=True)

            with patch("agent.copilot_acp_client.is_write_denied", return_value=True, create=True):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(target),
                            "content": "fake-private-key",
                        },
                    },
                    cwd=str(home),
                )

        self.assertIn("error", response)
        self.assertFalse(target.exists())

    def test_write_text_file_respects_safe_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            safe_root = root / "workspace"
            safe_root.mkdir()
            outside = root / "outside.txt"

            with patch.dict(os.environ, {"HERMES_WRITE_SAFE_ROOT": str(safe_root)}, clear=False):
                response = self._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "fs/write_text_file",
                        "params": {
                            "path": str(outside),
                            "content": "should-not-write",
                        },
                    },
                    cwd=str(root),
                )

        self.assertIn("error", response)
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()


# ── HOME env propagation tests (from PR #11285) ─────────────────────

from unittest.mock import patch as _patch
import pytest


def _make_home_client(tmp_path):
    return CopilotACPClient(
        api_key="copilot-acp",
        base_url="acp://copilot",
        acp_command="copilot",
        acp_args=["--acp", "--stdio"],
        acp_cwd=str(tmp_path),
    )


def _fake_popen_capture(captured):
    def _fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        raise FileNotFoundError("copilot not found")
    return _fake


def test_run_prompt_preserves_real_home_when_profile_home_available(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes"
    (hermes_home / "home").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    captured = {}
    client = _make_home_client(tmp_path)

    with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
        with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
            client._run_prompt("hello", timeout_seconds=1)

    assert captured["kwargs"]["env"]["HOME"] == str(real_home)
    assert captured["kwargs"]["env"]["HERMES_REAL_HOME"] == str(real_home)


def test_run_prompt_passes_home_when_parent_env_is_clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    captured = {}
    client = _make_home_client(tmp_path)

    with _patch("agent.copilot_acp_client.subprocess.Popen", side_effect=_fake_popen_capture(captured)):
        with pytest.raises(RuntimeError, match="Could not start Copilot ACP command"):
            client._run_prompt("hello", timeout_seconds=1)

    assert "env" in captured["kwargs"]
    assert captured["kwargs"]["env"]["HOME"]


# ── Empty-response receiving-layer recovery ────────────────────────────
# Regression for the "Processing completed but no response was generated"
# empty-response bug on the Claude Code / ACP path: a turn that did work via
# the engine's own tools (or was permission-declined, or stopped abnormally)
# but streamed no agent_message_chunk text used to return "" — a silent empty
# that burned the loop's empty-retry budget and surfaced the bland gateway
# warning, hiding that real work happened.

from agent.copilot_acp_client import _synthesize_empty_turn_note


class _StdinlessProc:
    """session/update is handled before the stdin guard, so no stdin needed."""

    stdin = None


def _update_msg(kind, *, text=None, content=None, title=None):
    update = {"sessionUpdate": kind}
    if content is not None:
        update["content"] = content
    elif text is not None:
        update["content"] = {"type": "text", "text": text}
    if title is not None:
        update["title"] = title
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"update": update}}


def _acp_client(tmp_path):
    return CopilotACPClient(acp_cwd=str(tmp_path))


def test_handle_message_captures_list_shaped_content(tmp_path):
    client = _acp_client(tmp_path)
    text_parts: list = []
    client._handle_server_message(
        _update_msg(
            "agent_message_chunk",
            content=[{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}],
        ),
        process=_StdinlessProc(),
        cwd=str(tmp_path),
        text_parts=text_parts,
        reasoning_parts=[],
        meta={},
    )
    assert "".join(text_parts) == "hello world"


def test_handle_message_records_tool_activity(tmp_path):
    client = _acp_client(tmp_path)
    meta: dict = {}
    client._handle_server_message(
        _update_msg("tool_call", title="Edit"),
        process=_StdinlessProc(),
        cwd=str(tmp_path),
        text_parts=[],
        reasoning_parts=[],
        meta=meta,
    )
    assert meta.get("saw_tool_activity") is True
    assert "Edit" in meta.get("tools", [])


def test_handle_message_records_permission_cancel(tmp_path):
    client = _acp_client(tmp_path)
    meta: dict = {}
    process = _FakeProcess()  # real StringIO stdin (permission path writes a reply)
    msg = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {"toolCall": {"kind": "execute", "title": "Bash"}},
    }
    client._handle_server_message(
        msg,
        process=process,
        cwd=str(tmp_path),
        text_parts=[],
        reasoning_parts=[],
        meta=meta,
    )
    assert meta.get("permission_cancelled") is True
    assert "Bash" in meta.get("blocked_tools", [])
    # Still declines on the wire (no security regression).
    reply = json.loads(process.stdin.getvalue().strip())
    assert reply["result"]["outcome"]["outcome"] == "cancelled"


def test_synthesize_note_for_tool_activity():
    note = _synthesize_empty_turn_note(
        "end_turn", {"saw_tool_activity": True, "tools": ["Edit", "Bash"]}, ""
    )
    assert note and "tool" in note.lower()


def test_synthesize_note_for_permission_cancel():
    note = _synthesize_empty_turn_note(
        "end_turn", {"permission_cancelled": True, "blocked_tools": ["Bash"]}, ""
    )
    assert note and "permission" in note.lower()


def test_synthesize_note_for_abnormal_stop_reason():
    assert "max_tokens" in _synthesize_empty_turn_note("max_tokens", {}, "")


def test_synthesize_note_empty_for_clean_empty():
    # No positive evidence of work: keep the transient-empty retry path intact.
    assert _synthesize_empty_turn_note("end_turn", {}, "") == ""
    assert _synthesize_empty_turn_note(None, {}, "") == ""


def test_send_prompt_synthesizes_note_on_tool_only_turn(tmp_path, monkeypatch):
    client = _acp_client(tmp_path)

    def fake_acp_request(conn, method, params, *, timeout_seconds, text_parts=None, reasoning_parts=None, meta=None):
        if meta is not None:
            meta["saw_tool_activity"] = True
            meta.setdefault("tools", []).append("Edit")
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(client, "_acp_request", fake_acp_request)
    text, reasoning, usage = client._send_prompt(None, "sess", "do the edit", 1.0)
    assert text and "tool" in text.lower()


def test_send_prompt_returns_text_on_normal_turn(tmp_path, monkeypatch):
    client = _acp_client(tmp_path)

    def fake_acp_request(conn, method, params, *, timeout_seconds, text_parts=None, reasoning_parts=None, meta=None):
        if text_parts is not None:
            text_parts.append("the answer")
        return {"stopReason": "end_turn", "usage": {"input_tokens": 1}}

    monkeypatch.setattr(client, "_acp_request", fake_acp_request)
    text, reasoning, usage = client._send_prompt(None, "sess", "q", 1.0)
    assert text == "the answer"


def test_send_prompt_clean_empty_stays_empty(tmp_path, monkeypatch):
    client = _acp_client(tmp_path)

    def fake_acp_request(conn, method, params, *, timeout_seconds, text_parts=None, reasoning_parts=None, meta=None):
        return {"stopReason": "end_turn"}

    monkeypatch.setattr(client, "_acp_request", fake_acp_request)
    text, reasoning, usage = client._send_prompt(None, "sess", "q", 1.0)
    # Preserved as empty so the conversation loop's transient-empty
    # retry/fallback ladder runs unchanged.
    assert text == ""
