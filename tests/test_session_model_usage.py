"""Unit tests for P5.3 per-model usage accounting (hermes_state.session_model_usage).

Uses a throwaway temp DB — never touches the real ~/.hermes/state.db.
"""

import tempfile
from pathlib import Path

from hermes_state import SessionDB


def _fresh_db():
    d = tempfile.mkdtemp(prefix="p53_test_")
    return SessionDB(db_path=Path(d) / "state.db")


def test_records_per_model_breakdown_not_collapsed():
    db = _fresh_db()
    db.create_session("s1", "test")
    # Simulate the 36.8M-style session: mostly Opus + a few gpt-5.5.
    for _ in range(3):
        db.record_model_usage(
            "s1", "claude-opus-4-8", "anthropic",
            prompt_tokens=245_000, input_tokens=1_000, cache_read_tokens=244_000,
            output_tokens=500, total_tokens=245_500, api_calls=1,
        )
    db.record_model_usage(
        "s1", "gpt-5.5", "openai-codex",
        prompt_tokens=90_000, input_tokens=5_000, cache_read_tokens=85_000,
        output_tokens=400, total_tokens=90_400, api_calls=1,
    )
    rows = db.get_model_usage("s1")
    assert len(rows) == 2  # two distinct models, NOT collapsed to one
    by_model = {r["model"]: r for r in rows}
    assert by_model["claude-opus-4-8"]["api_calls"] == 3
    assert by_model["claude-opus-4-8"]["prompt_tokens"] == 735_000
    assert by_model["claude-opus-4-8"]["provider"] == "anthropic"
    assert by_model["gpt-5.5"]["api_calls"] == 1
    assert by_model["gpt-5.5"]["prompt_tokens"] == 90_000
    # ordered biggest-first
    assert rows[0]["model"] == "claude-opus-4-8"


def test_upsert_accumulates_same_key():
    db = _fresh_db()
    db.record_model_usage("s2", "gpt-5.5", "openai-codex", prompt_tokens=100, api_calls=1)
    db.record_model_usage("s2", "gpt-5.5", "openai-codex", prompt_tokens=250, api_calls=1)
    rows = db.get_model_usage("s2")
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 350
    assert rows[0]["api_calls"] == 2


def test_missing_model_or_session_is_noop():
    db = _fresh_db()
    db.record_model_usage("", "gpt-5.5", prompt_tokens=100)
    db.record_model_usage("s3", "", prompt_tokens=100)
    assert db.get_model_usage("s3") == []


def test_blank_provider_defaults_empty_string():
    db = _fresh_db()
    db.record_model_usage("s4", "gpt-5.5", prompt_tokens=10, api_calls=1)
    rows = db.get_model_usage("s4")
    assert rows[0]["provider"] == ""
