"""Feishu DM task routing: classify an inbound top-level DM message as an
**actionable task** vs **chit-chat**, so the gateway can isolate each task into
its own Kanban card + thread + session (parallel, race-free) while chit-chat is
answered inline on the main DM session.

Design (spec: "每任务一卡一会话，闲聊秒回"):
  - Top-level DM message → ``classify_dm_message`` → task | chitchat | ambiguous.
  - ``task``     → create card + open thread + run in the thread's own session.
  - ``chitchat`` → reply inline on the main DM session (instant, keeps context).
  - ``ambiguous``→ caller may consult the auxiliary model; default is ``task``
    (isolating is the safer default — a spurious card is cheaper than running a
    real task un-isolated and racing the main conversation).

``classify_dm_message`` is a **pure heuristic** (no I/O) so it is cheap (zero
added latency on the common case) and unit-testable. The optional ``routed``
argument carries the existing ``infer_profile_and_route`` signals (kind/safety)
which the gateway already computes for route_decision — reused here, not recomputed.

This module is dormant unless ``feishu.dm_task_mode`` is enabled; with the flag
off the gateway never calls it (strict no-op, zero behavior change).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple

# ── Heuristic vocabularies ────────────────────────────────────────────────
# Short social / acknowledgement openers → chit-chat (answered inline, instant).
_CHITCHAT_EXACT = {
    "", "ok", "okay", "好", "好的", "嗯", "嗯嗯", "收到", "谢谢", "谢谢你", "多谢",
    "thx", "thanks", "thank you", "在吗", "在", "你好", "hi", "hello", "hey",
    "哈哈", "哈哈哈", "👍", "👌", "🙏", "?", "？", "??", "？？", "...", "。。。",
    "辛苦了", "可以", "行", "对", "是的", "没问题", "明白", "知道了", "继续",
}
# A leading task verb / imperative → actionable task (create card + thread).
_TASK_VERBS = (
    "帮我", "帮忙", "请", "麻烦", "生成", "创建", "新建", "查", "查询", "查一下",
    "看一下", "看下", "分析", "整理", "总结", "写", "起草", "拟", "改", "修改",
    "修复", "排查", "调试", "部署", "上线", "发布", "推送", "发", "发送", "下载",
    "上传", "导出", "导入", "对比", "比对", "评估", "设计", "实现", "开发", "重构",
    "测试", "验证", "检查", "复核", "调研", "搜索", "找", "梳理", "拆解", "规划",
    "build", "create", "fix", "analyze", "summar", "draft", "deploy", "review",
    "investigate", "research", "implement", "refactor", "generate", "search",
)
# Task-grade kinds from infer_profile_and_route.
_TASK_KINDS = {"engineering", "executive_brief", "product_strategy", "research"}

_LONG_TEXT_CHARS = 40       # >= this many chars → likely a task, not chatter
_SHORT_CHATTER_CHARS = 8    # <= this many chars + no verb + no '?' task intent


@dataclass(frozen=True)
class ClassifyResult:
    label: str          # "task" | "chitchat" | "ambiguous"
    reason: str
    confidence: float   # 0..1 (heuristic strength)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def classify_dm_message(text: str, *, routed: Optional[Any] = None) -> ClassifyResult:
    """Pure heuristic classifier. ``routed`` is an optional object with ``.kind``
    and ``.safety`` (an ``infer_profile_and_route`` result); when absent the
    decision relies on text shape + vocabularies only.
    """
    norm = _normalize(text)
    low = norm.lower()

    # 1. Exact short social/ack → chit-chat (instant inline).
    if low in _CHITCHAT_EXACT:
        return ClassifyResult("chitchat", "exact-social", 0.95)

    kind = str(getattr(routed, "kind", "") or "").lower()
    safety = str(getattr(routed, "safety", "") or "").lower()

    # 2. Strong task signals.
    if safety == "requires-confirmation":
        return ClassifyResult("task", "safety-requires-confirmation", 0.9)
    if kind in _TASK_KINDS:
        return ClassifyResult("task", f"kind={kind}", 0.85)
    has_verb = any(v in low for v in _TASK_VERBS)
    if has_verb and len(norm) >= 4:
        return ClassifyResult("task", "task-verb", 0.8)
    if len(norm) >= _LONG_TEXT_CHARS:
        return ClassifyResult("task", "long-text", 0.7)

    # 3. Short, no verb, general_chat → chit-chat.
    if len(norm) <= _SHORT_CHATTER_CHARS and not has_verb:
        return ClassifyResult("chitchat", "short-no-verb", 0.75)
    if kind == "general_chat" and not has_verb:
        return ClassifyResult("chitchat", "general-chat-no-verb", 0.6)

    # 4. Middle ground — let the caller decide (aux model or default→task).
    return ClassifyResult("ambiguous", "no-strong-signal", 0.0)


def resolve_label(result: ClassifyResult, *, mode: str, aux_label: Optional[str] = None) -> str:
    """Collapse a classify result to a final 'task'|'chitchat' given the configured
    ``mode`` ('heuristic' | 'heuristic+aux' | 'all-tasks') and an optional
    ``aux_label`` (set by the caller when it consulted the auxiliary model for an
    ambiguous result under 'heuristic+aux').
    """
    if mode == "all-tasks":
        return "task"
    if result.label in ("task", "chitchat"):
        return result.label
    # ambiguous
    if mode == "heuristic+aux" and aux_label in ("task", "chitchat"):
        return aux_label
    # heuristic-only (or aux unavailable): default ambiguous → task (isolate).
    return "task"


def needs_aux(result: ClassifyResult, *, mode: str) -> bool:
    """True iff the caller should consult the auxiliary model to resolve."""
    return mode == "heuristic+aux" and result.label == "ambiguous"
