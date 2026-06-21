"""Integration test: the P0 observability seam fires through the REAL turn prologue.

``test_route_decision.py`` covers the route_decision functions in isolation. This
file proves the *wiring*: that ``build_turn_context`` (the real prologue) invokes
``observe_turn_start``, and that the full turn-start -> first-usage flow produces
one JSONL record — without disturbing the prologue's output (zero behavior change).

Mirrors the lightweight fake-agent harness from ``test_turn_context.py``.
"""

from __future__ import annotations

import json
import types
from unittest.mock import patch

import pytest

from hermes_constants import get_hermes_home
from agent import route_decision as rd
from agent.turn_context import TurnContext, build_turn_context


class _FakeTodoStore:
    def has_items(self):
        return True


class _FakeGuardrails:
    def reset_for_turn(self):
        pass


class _FakeAgent:
    def __init__(self):
        # Today's reality: 100% gpt-5.5 via Codex.
        self.session_id = "sess-int-1"
        self.model = "gpt-5.5"
        self.provider = "codex"
        self.base_url = ""
        self.api_key = "sk-x"
        self.api_mode = "codex_responses"
        self.reasoning_config = {"effort": "xhigh"}
        self.platform = "cli"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(protect_first_n=2, protect_last_n=2)
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self._invalid_tool_retries = -1
        self._vision_supported = None

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _persist_session(self, *_a, **_k):
        pass


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _build(agent, **overrides):
    kwargs = dict(
        agent=agent,
        user_message="please debug the api repo and run the tests",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )
    kwargs.update(overrides)
    return build_turn_context(**kwargs)


def _log_lines():
    p = get_hermes_home() / "logs" / "route_decisions.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_real_prologue_stashes_pending_and_flow_writes_one_record():
    agent = _FakeAgent()
    ctx = _build(agent)

    # Zero behavior change: the prologue still produces a correct TurnContext.
    assert isinstance(ctx, TurnContext)
    assert ctx.messages[-1]["content"] == "please debug the api repo and run the tests"
    assert ctx.active_system_prompt == "SYSTEM"

    # The wired seam fired: a pending record is stashed (computed, not consumed).
    pending = agent.__dict__.get("_route_decision_pending")
    assert isinstance(pending, dict)
    assert pending["actual_model"] == "gpt-5.5"      # ACTUAL engine (FakeAgent) unchanged
    assert pending["chosen_pool"] == "codex"
    assert pending["task_kind"] == "engineering"
    # PLANNED route (computed, not consumed unless enabled): subscription Sonnet.
    assert pending["planned_provider"] == "claude-code-acp"
    assert "sonnet" in pending["planned_model"]
    assert pending["_flushed"] is False
    # Nothing written until usage arrives.
    assert _log_lines() == []

    # First API call's usage flushes exactly one record.
    usage = types.SimpleNamespace(cache_read_tokens=950, prompt_tokens=1000)
    rd.observe_usage(agent, usage, prompt_tokens=1000)
    lines = _log_lines()
    assert len(lines) == 1
    rec = lines[0]
    assert rec["session_id"] == "sess-int-1"
    assert rec["chosen_pool"] == "codex"
    assert rec["cache_hit"] is True
    assert rec["cache_hit_pct"] == 95.0
    assert rec["reasoning_effort"] == "xhigh"


def test_logging_off_means_no_pending_and_no_write(monkeypatch):
    monkeypatch.setattr(rd, "_config", lambda: {"enabled": False, "log": False})
    agent = _FakeAgent()
    ctx = _build(agent)
    # Prologue output is unaffected.
    assert isinstance(ctx, TurnContext)
    # With logging off the seam is a strict no-op.
    assert agent.__dict__.get("_route_decision_pending") is None
    rd.observe_usage(agent, types.SimpleNamespace(cache_read_tokens=1, prompt_tokens=2))
    assert _log_lines() == []
