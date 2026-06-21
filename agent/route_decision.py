"""Route-decision skeleton + read-only observability (Jarvis token-maximization, P0).

This is the **P0 observability seam** for the "Jarvis Bot 用量最大化" plan: per
turn it records what engine/pool the agent *actually* used, plus a *planned*
route decision that is **computed but not consumed** in P0. Collecting this for a
baseline window is the prerequisite for every later 放量 (volume) action — it lets
us compare planned-vs-actual the moment routing is switched on in P1.

Invariants this module must never break (SELF_ARCHITECTURE.md §12 + the
token-maximization spec §0.2 no-touch list):

* **``decide_route`` is pure.** No I/O, no side effects, deterministic on its
  input. In P0 it returns the *current* behavior (Codex / gpt-5.5) regardless of
  features, so wiring it in changes nothing. The full L1-L5 × stakes × pool-health
  matrix arrives in T2.1.
* **Observability is fail-silent.** ``observe_turn_start`` / ``observe_usage``
  catch every exception internally and return ``None``. A logging failure must
  never affect the conversation — zero behavior change is the P0 contract.
* **Config-gated, single flag to roll back.** ``route_decision.log: false`` stops
  JSONL writes; ``route_decision.enabled: false`` keeps the (future) decision
  unused. Both default to the behavior-neutral state in P0 (``enabled=false``,
  ``log=true``).

The JSONL log lands at ``$HERMES_HOME/logs/route_decisions.jsonl`` (one object per
turn). This module deliberately does NOT register a core tool, mutate context, or
touch model selection — it is internal-only and invisible to the model.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Bump when the JSONL record shape changes so downstream aggregators can adapt.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Pure routing types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RouteFeatures:
    """Inputs to ``decide_route`` (the decision is a pure function of these).

    The pool-health floats are ``1.0`` (full) until the Opus-wall governor (T3.2)
    feeds real subscription usage in; in P0 they are placeholders only.
    """

    profile: str = "general"             # jarviscode / jarvisresearch / jarvisreview / general
    complexity: int = 1                  # L1..L5 (cheap heuristic in P0; T2.1 replaces)
    stakes: str = "normal"               # low | normal | high
    context_tokens: int = 0              # >400K => long-context gate (future)
    opus_week_remaining: float = 1.0     # 0..1 Opus included weekly remaining (the one scarce wall)
    extra400_remaining: float = 1.0      # 0..1 $400 extra-usage remaining (overflow buffer)
    codex_month_remaining: float = 1.0   # 0..1 $200 Codex remaining (re-overflow)
    fanout_candidate: bool = False


@dataclass(frozen=True)
class RouteDecision:
    """Output of ``decide_route`` — a *plan*, never applied directly by this module."""

    provider: str
    model: str
    reasoning_effort: str                # "" = inherit current
    pool: str                            # billing/quota pool label (see ``classify_pool``)
    fanout_width: int = 0
    verify_depth: str = "none"           # none | self-check | reviewer-pass
    reason: str = ""


# ── Routing targets (T1.3): subscription provider via Path C, config-overridable ──
_SUBSCRIPTION_PROVIDER = "claude-code-acp"
_DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"
_DEFAULT_OPUS_MODEL = "claude-opus-4-8"
_DEFAULT_OPUS_COMPLEXITY_MIN = 4


def _route_models() -> tuple[str, str, int]:
    cfg = _config()
    return (
        str(cfg.get("sonnet_model") or _DEFAULT_SONNET_MODEL),
        str(cfg.get("opus_model") or _DEFAULT_OPUS_MODEL),
        int(cfg.get("opus_complexity_min") or _DEFAULT_OPUS_COMPLEXITY_MIN),
    )


def decide_route(f: RouteFeatures) -> RouteDecision:
    """Pure routing decision (T1.3, minimal): subscription provider, simple→Sonnet,
    complex→Opus.

    This returns the *plan*. Whether it is CONSUMED is gated by
    ``route_decision.enabled`` at the call site:

    * ``enabled=false`` (P0): the result is computed for observability only and the
      caller does NOT apply it — strict no-op on behavior (still gpt-5.5).
    * ``enabled=true`` (T1.3): the caller pins this engine/model once per session
      (cache-safe; never switched mid-session — no-touch #1).
    """
    sonnet, opus, opus_min = _route_models()
    want_opus = f.complexity >= opus_min or (f.stakes == "high" and f.complexity >= 3)
    return RouteDecision(
        provider=_SUBSCRIPTION_PROVIDER,
        model=opus if want_opus else sonnet,
        reasoning_effort="",
        pool="included_opus" if want_opus else "included_sonnet",
        fanout_width=0,
        verify_depth="none",
        reason="t1.3-simple-sonnet-complex-opus",
    )


def classify_pool(
    provider: Optional[str], model: Optional[str], api_mode: Optional[str] = None
) -> str:
    """Best-effort map ``(provider, model)`` -> billing/quota pool label.

    Pools follow the token-maximization spec §2.1 waterfall:

    * ``included_sonnet`` / ``included_opus`` — Claude included weekly quota,
      reachable only via the subscription CLI provider (P1+).
    * ``extra400`` — Opus on the $400 extra-usage layer (bare ``anthropic`` api_key
      path, e.g. the pre-2026-06-20 direct-API path).
    * ``codex`` — Codex $200 (gpt-5.5).

    P0 reality is 100% ``codex``; the Claude pools become reachable in P1, at which
    point this mapping is the seam that tells included from $400 spend.
    """
    p = (provider or "").lower()
    m = (model or "").lower()
    am = (api_mode or "").lower()
    _subscription = any(tok in p for tok in ("claude-code", "subscription", "oauth", "acp", "external"))
    if "opus" in m:
        if _subscription:
            return "included_opus"
        if p == "anthropic" and am == "anthropic_messages":
            return "extra400"
        return "opus_other"
    if "sonnet" in m:
        return "included_sonnet" if _subscription else "sonnet_other"
    if m.startswith("gpt-5") or p in ("codex", "openai"):
        return "codex"
    return p or "unknown"


# ---------------------------------------------------------------------------
# Config access (read-only; never mutate the returned dict)
# ---------------------------------------------------------------------------
def _config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly()
        rd = cfg.get("route_decision")
        return rd if isinstance(rd, dict) else {}
    except Exception:
        return {}


def is_enabled() -> bool:
    """Whether ``decide_route`` output should be *consumed* (P1+). P0 default: False."""
    return bool(_config().get("enabled", False))


def is_logging() -> bool:
    """Whether to write the JSONL observability log. P0 default: True."""
    return bool(_config().get("log", True))


def _log_path():
    from pathlib import Path

    raw = (_config().get("log_path") or "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "logs" / "route_decisions.jsonl"
    except Exception:
        return Path("~/.hermes/logs/route_decisions.jsonl").expanduser()


def append_record(record: dict) -> None:
    """Append one JSON object as a line to the route_decisions JSONL log.

    Fail-silent: any error (disk, encoding, path) is swallowed — this is
    observability, never a behavior dependency.
    """
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.debug("route_decision.append_record failed", exc_info=True)


# ---------------------------------------------------------------------------
# Cheap P0 feature heuristics (placeholders; T2.1 replaces with the real matrix)
# ---------------------------------------------------------------------------
def _profile_from_kind(kind: str) -> str:
    return {
        "engineering": "jarviscode",
        "executive_brief": "jarvisreview",
        "product_strategy": "jarvisresearch",
    }.get(kind, "general")


def _stakes_from_safety(safety: str) -> str:
    return "high" if safety == "requires-confirmation" else "normal"


def _cheap_complexity(message: str, kind: str, context_tokens: int) -> int:
    """Very cheap L1..L5 heuristic for the baseline only (T2.1 replaces this)."""
    n = len(message or "")
    score = 1
    if n > 280:
        score += 1
    if n > 1200:
        score += 1
    if kind in ("engineering", "product_strategy"):
        score += 1
    if context_tokens > 400_000:
        score += 1
    return max(1, min(5, score))


def _read_effort(agent) -> str:
    try:
        rc = getattr(agent, "reasoning_config", None)
        if isinstance(rc, dict):
            return str(rc.get("effort") or rc.get("reasoning_effort") or "")
        eff = getattr(agent, "reasoning_effort", None)
        return str(eff or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Observability seams (fail-silent; zero behavior change)
# ---------------------------------------------------------------------------
def features_from_message(user_message: Any, messages: Optional[list] = None) -> RouteFeatures:
    """Build ``RouteFeatures`` from a user message (cheap heuristics; T2.1 deepens).

    Shared by the P0 observability hook and the T1.3 session-routing pin so both
    classify identically.
    """
    msg_str = user_message if isinstance(user_message, str) else ""
    kind = "general_chat"
    safety = "local-dry-run-safe"
    try:
        from hermes_cli.jarvis import infer_profile_and_route

        routed = infer_profile_and_route(msg_str or " ")
        kind = routed.kind
        safety = routed.safety
    except Exception:
        pass
    context_tokens = 0
    try:
        from agent.model_metadata import estimate_messages_tokens_rough

        context_tokens = int(estimate_messages_tokens_rough(messages or []))
    except Exception:
        pass
    return RouteFeatures(
        profile=_profile_from_kind(kind),
        complexity=_cheap_complexity(msg_str, kind, context_tokens),
        stakes=_stakes_from_safety(safety),
        context_tokens=context_tokens,
        fanout_candidate=False,
    )


def observe_turn_start(
    agent,
    user_message: Any,
    conversation_history: Optional[list],
    messages: Optional[list],
    *,
    task_id: str = "",
    turn_id: str = "",
) -> None:
    """P0 seam A — at turn start (before the first API call), compute route features
    + a (currently unused) ``decide_route`` result and stash a pending record on the
    agent. ``observe_usage`` later enriches and flushes it.

    Fail-silent. Reads only; never mutates ``messages`` or model selection.
    """
    try:
        if not is_logging():
            agent.__dict__["_route_decision_pending"] = None
            return

        msg_str = user_message if isinstance(user_message, str) else ""

        kind = "general_chat"
        safety = "local-dry-run-safe"
        try:
            from hermes_cli.jarvis import infer_profile_and_route

            routed = infer_profile_and_route(msg_str or " ")
            kind = routed.kind
            safety = routed.safety
        except Exception:
            pass

        context_tokens = 0
        try:
            from agent.model_metadata import estimate_messages_tokens_rough

            context_tokens = int(estimate_messages_tokens_rough(messages or []))
        except Exception:
            pass

        profile = _profile_from_kind(kind)
        complexity = _cheap_complexity(msg_str, kind, context_tokens)
        stakes = _stakes_from_safety(safety)

        features = RouteFeatures(
            profile=profile,
            complexity=complexity,
            stakes=stakes,
            context_tokens=context_tokens,
            fanout_candidate=False,
        )
        decision = decide_route(features)  # computed, NOT consumed in P0

        actual_provider = getattr(agent, "provider", "") or ""
        actual_model = getattr(agent, "model", "") or ""
        actual_api_mode = getattr(agent, "api_mode", "") or ""
        actual_effort = _read_effort(agent)

        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": time.time(),
            "session_id": getattr(agent, "session_id", "") or "",
            "task_id": task_id,
            "turn_id": turn_id,
            "is_child": bool(
                getattr(agent, "is_subagent", False)
                or getattr(agent, "_is_delegated_child", False)
                or getattr(agent, "_is_subagent", False)
            ),
            # ── features (decision inputs) ──
            "task_kind": kind,
            "profile": profile,
            "complexity": complexity,
            "stakes": stakes,
            "context_tokens": context_tokens,
            "fanout_width": decision.fanout_width,
            # ── actual behavior this turn (the baseline reality) ──
            "actual_provider": actual_provider,
            "actual_model": actual_model,
            "reasoning_effort": actual_effort,
            "chosen_pool": classify_pool(actual_provider, actual_model, actual_api_mode),
            # ── planned route (decide_route output, UNUSED in P0) ──
            "planned_provider": decision.provider,
            "planned_model": decision.model,
            "planned_pool": decision.pool,
            "planned_effort": decision.reasoning_effort,
            # ── usage (filled by observe_usage after the first API call) ──
            "cache_hit": None,
            "cache_hit_pct": None,
            "prompt_tokens": None,
            "cache_read_tokens": None,
            # ── quality (placeholder; wired in dashboard / later phases) ──
            "quality_signal": None,
            # internal bookkeeping, stripped before write
            "_flushed": False,
        }
        agent.__dict__["_route_decision_pending"] = record
    except Exception:
        logger.debug("route_decision.observe_turn_start failed", exc_info=True)
        try:
            agent.__dict__["_route_decision_pending"] = None
        except Exception:
            pass


def observe_usage(agent, canonical_usage, prompt_tokens: Optional[int] = None) -> None:
    """P0 seam B — after the FIRST API call of the turn, enrich the pending record
    with realized usage (cache-hit, prompt/cache-read tokens) and flush it once.

    Fail-silent; idempotent per turn via the ``_flushed`` marker (only the first
    call's usage is recorded — that is the representative cache-hit for the cached
    prefix of the turn).
    """
    try:
        record = getattr(agent, "_route_decision_pending", None)
        if not isinstance(record, dict) or record.get("_flushed"):
            return
        cr = int(getattr(canonical_usage, "cache_read_tokens", 0) or 0)
        if prompt_tokens is not None:
            pt = int(prompt_tokens)
        else:
            pt = int(getattr(canonical_usage, "prompt_tokens", 0) or 0)
        # The ACP path (claude-code-acp) reports usage=0; fall back to the estimated
        # context_tokens so the included-pool volume isn't a flat zero on the board.
        if pt == 0:
            ct = record.get("context_tokens")
            if isinstance(ct, (int, float)) and ct > 0:
                pt = int(ct)
                record["prompt_tokens_estimated"] = True
        record["prompt_tokens"] = pt
        record["cache_read_tokens"] = cr
        record["cache_hit"] = bool(cr > 0)
        record["cache_hit_pct"] = round(100.0 * cr / pt, 1) if pt else 0.0
        record["_flushed"] = True
        out = {k: v for k, v in record.items() if not str(k).startswith("_")}
        append_record(out)
    except Exception:
        logger.debug("route_decision.observe_usage failed", exc_info=True)
