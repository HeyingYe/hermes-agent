"""Tests for the Feishu DM topic -> session binding storage layer.

Mirrors the Telegram topic-binding tables: each Feishu 话题 (topic,
thread_id ``omt_…``) binds to exactly one Hermes session.
"""

import pytest

from hermes_state import SessionDB


def _db(tmp_path) -> SessionDB:
    return SessionDB(tmp_path / "state.db")


def test_bind_and_lookup_roundtrip(tmp_path):
    db = _db(tmp_path)
    db.create_session(session_id="s1", source="feishu", user_id="u1")

    db.bind_feishu_topic(
        chat_id="oc_x", thread_id="omt_1", user_id="u1",
        session_key="agent:main:feishu:dm:oc_x", session_id="s1",
    )

    binding = db.get_feishu_topic_binding(chat_id="oc_x", thread_id="omt_1")
    assert binding is not None
    assert binding["session_id"] == "s1"
    assert binding["session_key"] == "agent:main:feishu:dm:oc_x"

    by_session = db.get_feishu_topic_binding_by_session(session_id="s1")
    assert by_session is not None
    assert by_session["thread_id"] == "omt_1"

    listed = db.list_feishu_topic_bindings_for_chat(chat_id="oc_x")
    assert len(listed) == 1
    assert listed[0]["thread_id"] == "omt_1"


def test_rebind_same_topic_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.create_session(session_id="s1", source="feishu", user_id="u1")
    for _ in range(2):
        db.bind_feishu_topic(
            chat_id="oc_x", thread_id="omt_1", user_id="u1",
            session_key="k", session_id="s1",
        )
    assert len(db.list_feishu_topic_bindings_for_chat(chat_id="oc_x")) == 1


def test_linking_session_to_second_topic_raises(tmp_path):
    db = _db(tmp_path)
    db.create_session(session_id="s1", source="feishu", user_id="u1")
    db.bind_feishu_topic(
        chat_id="oc_x", thread_id="omt_1", user_id="u1",
        session_key="k", session_id="s1",
    )
    with pytest.raises(ValueError):
        db.bind_feishu_topic(
            chat_id="oc_x", thread_id="omt_2", user_id="u1",
            session_key="k", session_id="s1",
        )


def test_reads_are_safe_before_any_bind(tmp_path):
    """Reads must not raise (or trigger migration) when the table is absent."""
    db = _db(tmp_path)
    assert db.get_feishu_topic_binding(chat_id="oc_x", thread_id="omt_1") is None
    assert db.get_feishu_topic_binding_by_session(session_id="s1") is None
    assert db.list_feishu_topic_bindings_for_chat(chat_id="oc_x") == []


def test_binding_cleared_when_session_pruned(tmp_path):
    """ON DELETE CASCADE removes the binding when its session row is deleted."""
    db = _db(tmp_path)
    db.create_session(session_id="s1", source="feishu", user_id="u1")
    db.bind_feishu_topic(
        chat_id="oc_x", thread_id="omt_1", user_id="u1",
        session_key="k", session_id="s1",
    )
    with db._lock:
        db._conn.execute("DELETE FROM sessions WHERE id = ?", ("s1",))
        db._conn.commit()
    assert db.get_feishu_topic_binding(chat_id="oc_x", thread_id="omt_1") is None
