"""Tests for Feishu DM 'topic = session' lane routing (gateway/run.py).

Each top-level Feishu DM starts a brand-new session and the bot opens a 话题
(topic) for it; follow-ups inside that topic resume the bound session.
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from hermes_state import SessionDB
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


def _source(thread_id=None):
    return SessionSource(
        platform=Platform.FEISHU,
        user_id="u1",
        chat_id="oc_x",
        user_name="tester",
        chat_type="dm",
        thread_id=thread_id,
    )


def _runner(*, session_db=None, topic_sessions=True, send_thread_id="omt_new"):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="***")},
        feishu_topic_sessions=topic_sessions,
    )
    adapter = MagicMock()
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="om_reply", thread_id=send_thread_id)
    )
    runner.adapters = {Platform.FEISHU: adapter}
    runner._session_db = session_db
    runner._cache_session_source = MagicMock()
    return runner, adapter


def _entry(session_id="s1", thread_id=None):
    src = _source(thread_id=thread_id)
    return SessionEntry(
        session_key="agent:main:feishu:dm:oc_x" + (f":{thread_id}" if thread_id else ""),
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.FEISHU,
        chat_type="dm",
        origin=src,
    )


def test_lane_detection():
    runner, _ = _runner()
    assert runner._is_feishu_topic_top_level(_source(None)) is True
    assert runner._is_feishu_topic_top_level(_source("omt_1")) is False
    assert runner._is_feishu_topic_lane(_source("omt_1")) is True
    assert runner._is_feishu_topic_lane(_source(None)) is False
    # quote-reply ids (om_) are NOT topics — they must not become lanes
    assert runner._is_feishu_topic_lane(_source("om_quotereply")) is False


def test_lane_detection_disabled_by_config():
    runner, _ = _runner(topic_sessions=False)
    assert runner._is_feishu_topic_top_level(_source(None)) is False
    assert runner._is_feishu_topic_lane(_source("omt_1")) is False


def test_group_messages_are_never_lanes():
    runner, _ = _runner()
    grp = SessionSource(
        platform=Platform.FEISHU, user_id="u1", chat_id="oc_g",
        user_name="t", chat_type="group", thread_id="omt_1",
    )
    assert runner._is_feishu_topic_lane(grp) is False
    assert runner._is_feishu_topic_top_level(grp) is False


def test_open_topic_creates_binds_and_rewrites_source(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="s1", source="feishu", user_id="u1")
    runner, adapter = _runner(session_db=db)
    src = _source(None)
    event = MessageEvent(text="dispatch task A", source=src, message_id="om_root")

    new_source = asyncio.run(
        runner._open_feishu_topic_for_session(src, event, _entry("s1"))
    )

    # The kickoff send asked Feishu to create a topic, replying to the root msg.
    adapter.send.assert_awaited_once()
    _, kwargs = adapter.send.call_args
    assert kwargs["metadata"]["create_feishu_topic"] is True
    assert kwargs["metadata"]["reply_to_message_id"] == "om_root"

    # The source is rewritten into the freshly-minted topic so the rest of the
    # turn replies there.
    assert new_source.thread_id == "omt_new"
    assert event.source.thread_id == "omt_new"

    # And the topic is bound to the session for follow-up resolution.
    binding = db.get_feishu_topic_binding(chat_id="oc_x", thread_id="omt_new")
    assert binding is not None and binding["session_id"] == "s1"


def test_open_topic_graceful_when_no_thread_id(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="s2", source="feishu", user_id="u1")
    runner, _ = _runner(session_db=db, send_thread_id=None)
    src = _source(None)
    event = MessageEvent(text="hi", source=src, message_id="om_root")

    new_source = asyncio.run(
        runner._open_feishu_topic_for_session(src, event, _entry("s2"))
    )

    # No thread_id came back -> stay top-level, no binding recorded.
    assert new_source.thread_id is None
    assert db.get_feishu_topic_binding_by_session(session_id="s2") is None
