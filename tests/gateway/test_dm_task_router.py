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
