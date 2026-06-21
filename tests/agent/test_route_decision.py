"""Tests for agent/route_decision.py — the P0 observability seam.

P0 contract under test:
* ``decide_route`` is a *pure no-op router* in P0 — it always returns the current
  engine (Codex / gpt-5.5) regardless of features, and writes nothing.
* The observability seams (``observe_turn_start`` -> ``observe_usage``) emit exactly
  one JSONL record per turn, are idempotent per turn, honor the ``log`` flag, and
  are fail-silent on bad input (zero behavior change is the whole point).

Invariants, not snapshots: we assert relationships (current-engine, one-record,
fail-silent), not literal field counts.
"""

import json
from types import SimpleNamespace

from hermes_constants import get_hermes_home
from agent import route_decision as rd
from agent.route_decision import (
    RouteFeatures,
    RouteDecision,
    decide_route,
    classify_pool,
    append_record,
)


class _FakeAgent:
    """Minimal stand-in for AIAgent (route_decision only reads a few attrs)."""

    def __init__(
        self,
        provider="codex",
        model="gpt-5.5",
        api_mode="codex_responses",
        session_id="sess_test",
        reasoning_config=None,
    ):
        self.provider = provider
        self.model = model
        self.api_mode = api_mode
        self.session_id = session_id
        self.reasoning_config = reasoning_config or {"effort": "xhigh"}


def _log_file():
    return get_hermes_home() / "logs" / "route_decisions.jsonl"


def _read_lines():
    p = _log_file()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_decide_route_simple_sonnet_complex_opus():
    # T1.3 contract: subscription provider; simple→Sonnet, complex→Opus. (Whether
    # this is *applied* is gated by route_decision.enabled at the call site; P0
    # keeps it disabled so behavior is unchanged — that gate is tested in the
    # gateway, not here.)
    simple = decide_route(RouteFeatures(complexity=1))
    assert simple.provider == "claude-code-acp"
    assert "sonnet" in simple.model and simple.pool == "included_sonnet"

    complex_ = decide_route(RouteFeatures(complexity=5))
    assert complex_.provider == "claude-code-acp"
    assert "opus" in complex_.model and complex_.pool == "included_opus"

    # High stakes promotes to Opus even at moderate complexity.
    assert "opus" in decide_route(RouteFeatures(complexity=3, stakes="high")).model


def test_features_from_message_classifies():
    from agent.route_decision import features_from_message

    eng = features_from_message("fix the bug in the api repo and run tests")
    assert eng.profile == "jarviscode"  # engineering keywords
    assert features_from_message("hi there").profile == "general"


def test_decide_route_is_pure_no_side_effects():
    for _ in range(5):
        decide_route(RouteFeatures(complexity=4))
    # A pure decision must never touch the filesystem.
    assert not _log_file().exists()


def test_classify_pool_waterfall_labels():
    assert classify_pool("codex", "gpt-5.5") == "codex"
    assert classify_pool("openai", "gpt-5.5-mini") == "codex"
    assert classify_pool("claude-code-acp", "claude-sonnet-4-6") == "included_sonnet"
    assert classify_pool("claude-code-acp", "claude-opus-4-8") == "included_opus"
    # The pre-2026-06-20 bare-API-key Opus path is the $400 layer, NOT included.
    assert classify_pool("anthropic", "claude-opus-4-8", "anthropic_messages") == "extra400"


def test_append_record_writes_jsonl_roundtrip():
    append_record({"hello": "world", "n": 1})
    lines = _read_lines()
    assert lines and lines[-1]["hello"] == "world" and lines[-1]["n"] == 1


def test_observe_turn_then_usage_flushes_exactly_one_record():
    agent = _FakeAgent()
    rd.observe_turn_start(
        agent,
        "fix the bug in the api repo and run tests",
        None,
        [{"role": "user", "content": "fix the bug"}],
        task_id="t1",
        turn_id="turn1",
    )
    # Decision is stashed but nothing is written until the first API call's usage.
    assert _read_lines() == []

    usage = SimpleNamespace(cache_read_tokens=900, prompt_tokens=1000)
    rd.observe_usage(agent, usage, prompt_tokens=1000)

    lines = _read_lines()
    assert len(lines) == 1
    rec = lines[0]
    # Actual behavior recorded as the baseline reality.
    assert rec["actual_model"] == "gpt-5.5"
    assert rec["chosen_pool"] == "codex"
    # Planned (computed-but-unused) route is logged alongside for planned-vs-actual.
    # Engineering + short msg => complexity ~2 => simple => Sonnet on the subscription.
    assert rec["planned_provider"] == "claude-code-acp"
    assert "sonnet" in rec["planned_model"]
    # Usage enrichment from the first API call.
    assert rec["prompt_tokens"] == 1000
    assert rec["cache_read_tokens"] == 900
    assert rec["cache_hit"] is True
    assert rec["cache_hit_pct"] == 90.0
    # Feature extraction: engineering keywords -> jarviscode profile.
    assert rec["task_kind"] == "engineering"
    assert rec["profile"] == "jarviscode"
    # Internal bookkeeping is stripped; quality placeholder is present.
    assert "_flushed" not in rec
    assert rec["quality_signal"] is None


def test_observe_usage_idempotent_per_turn():
    agent = _FakeAgent()
    rd.observe_turn_start(agent, "hello there", None, [], task_id="t", turn_id="x")
    u = SimpleNamespace(cache_read_tokens=0, prompt_tokens=10)
    rd.observe_usage(agent, u)
    rd.observe_usage(agent, u)  # second call must NOT write a duplicate
    lines = _read_lines()
    assert len(lines) == 1
    assert lines[0]["cache_hit"] is False
    assert lines[0]["cache_hit_pct"] == 0.0


def test_logging_disabled_writes_nothing(monkeypatch):
    monkeypatch.setattr(rd, "_config", lambda: {"enabled": False, "log": False})
    agent = _FakeAgent()
    rd.observe_turn_start(agent, "anything", None, [], task_id="t", turn_id="x")
    rd.observe_usage(agent, SimpleNamespace(cache_read_tokens=1, prompt_tokens=2))
    assert _read_lines() == []


def test_observe_is_fail_silent_on_bad_input():
    # Must never raise, even with a None agent / bad usage object — the loop
    # depends on these being best-effort no-ops.
    rd.observe_turn_start(None, None, None, None)
    rd.observe_usage(None, None)
    rd.observe_usage(object(), object())
