"""Tests for the Feishu DM task/chit-chat classifier (gateway/dm_task_router.py)."""

from dataclasses import dataclass
from typing import Optional

from gateway.dm_task_router import (
    ClassifyResult,
    classify_dm_message,
    needs_aux,
    resolve_label,
)


@dataclass
class _Routed:
    kind: str = "general_chat"
    safety: str = "local-dry-run-safe"


def _label(text, routed=None):
    return classify_dm_message(text, routed=routed).label


def test_exact_social_is_chitchat():
    for t in ["谢谢", "ok", "好的", "收到", "在吗", "👍", "继续", "哈哈", "?"]:
        assert _label(t) == "chitchat", t


def test_task_verbs_are_tasks():
    for t in ["帮我查一下今天的会议", "生成一份周报", "修复这个 bug", "部署到测试环境",
              "整理一个 lark 文档给我", "analyze the logs", "refactor this module"]:
        assert _label(t) == "task", t


def test_safety_requires_confirmation_is_task():
    r = _Routed(kind="general_chat", safety="requires-confirmation")
    assert _label("把这条发到群里", routed=r) == "task"


def test_task_kind_is_task():
    for k in ("engineering", "executive_brief", "product_strategy", "research"):
        assert _label("xx", routed=_Routed(kind=k)) == "task", k


def test_long_text_is_task():
    assert _label("这是一段很长的描述" * 6) == "task"


def test_short_no_verb_is_chitchat():
    assert _label("嗯哼") == "chitchat"
    assert _label("是吗") == "chitchat"


def test_general_chat_no_verb_is_chitchat():
    assert _label("今天天气不错", routed=_Routed(kind="general_chat")) == "chitchat"


def test_ambiguous_middle_ground():
    # medium length, no verb, no kind signal → ambiguous
    res = classify_dm_message("关于那个方案的第三点我有点疑问想确认下")
    # may be task (long) or ambiguous depending on length; assert it's not chitchat
    assert res.label in ("task", "ambiguous")


def test_resolve_label_modes():
    amb = ClassifyResult("ambiguous", "x", 0.0)
    task = ClassifyResult("task", "x", 0.8)
    chit = ClassifyResult("chitchat", "x", 0.9)
    # all-tasks forces task
    assert resolve_label(chit, mode="all-tasks") == "task"
    # explicit labels pass through
    assert resolve_label(task, mode="heuristic") == "task"
    assert resolve_label(chit, mode="heuristic") == "chitchat"
    # ambiguous defaults to task in heuristic-only
    assert resolve_label(amb, mode="heuristic") == "task"
    # ambiguous uses aux label when provided under heuristic+aux
    assert resolve_label(amb, mode="heuristic+aux", aux_label="chitchat") == "chitchat"
    assert resolve_label(amb, mode="heuristic+aux", aux_label=None) == "task"


def test_needs_aux():
    amb = ClassifyResult("ambiguous", "x", 0.0)
    task = ClassifyResult("task", "x", 0.8)
    assert needs_aux(amb, mode="heuristic+aux") is True
    assert needs_aux(amb, mode="heuristic") is False
    assert needs_aux(task, mode="heuristic+aux") is False


# ── FeishuAdapter._dm_task_should_spawn gate (no full adapter needed) ──────────
def _gate(classify="heuristic"):
    from types import SimpleNamespace
    from gateway.platforms.feishu import FeishuAdapter
    return FeishuAdapter._dm_task_should_spawn.__get__(
        SimpleNamespace(_dm_task_classify=classify)
    )


def _event(text, *, chat_type="dm", mid="om_1", command=False):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from gateway.config import Platform
    src = SessionSource(platform=Platform.FEISHU, chat_id="oc_1", chat_type=chat_type, message_id=mid)
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND if command else MessageType.TEXT,
        source=src,
        message_id=mid,
    )


def test_gate_task_in_dm_spawns():
    assert _gate()(_event("帮我生成一份周报并推送给我")) is True


def test_gate_chitchat_in_dm_does_not_spawn():
    assert _gate()(_event("谢谢")) is False


def test_gate_group_excluded():
    assert _gate()(_event("帮我生成一份周报", chat_type="group")) is False


def test_gate_command_excluded():
    assert _gate()(_event("/new")) is False


def test_gate_nontext_excluded():
    assert _gate()(_event("帮我生成一份周报", command=True)) is False


def test_gate_missing_message_id_excluded():
    assert _gate()(_event("帮我生成一份周报", mid=None)) is False


def test_gate_all_tasks_mode_spawns_chitchat_too():
    assert _gate(classify="all-tasks")(_event("谢谢")) is True
