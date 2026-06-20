"""Ranked context packing for Honcho memory injection.

The packer is intentionally local and deterministic: it avoids extra model or
network calls while preserving the existing contextTokens character-estimate
budget contract used by HonchoMemoryProvider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class MemoryContextItem:
    """One candidate memory chunk with lightweight scoring metadata."""

    title: str
    content: str
    layer: str = "task"
    importance: float = 1.0
    recency: float = 0.0
    safety_boost: float = 0.0


_DEFAULT_LAYER_BUDGETS = {
    "stable": 0.30,
    "task": 0.30,
    "constraints": 0.25,
    "dialectic": 0.15,
}
_LAYER_ORDER = ("constraints", "stable", "task", "dialectic")
_SAFETY_TERMS = {
    "safe", "safety", "security", "privacy", "permission", "permissions",
    "secret", "secrets", "credential", "credentials", "token", "tokens",
    "pii", "risk", "risks", "approval", "irreversible", "destructive",
    "权限", "隐私", "安全", "密钥", "凭证", "风险",
}
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about",
    "what", "when", "where", "which", "who", "how", "are", "was", "were",
    "has", "have", "had", "user", "prefers", "preference", "assistant",
}


def pack_memory_context(
    items: Iterable[MemoryContextItem],
    *,
    query: str,
    config: Any,
) -> str:
    """Rank, deduplicate, and pack memory context items within contextTokens.

    If packing is disabled or no contextTokens cap is configured, callers should
    keep the legacy path. This function still behaves sensibly when invoked with
    no cap by returning all ranked/deduplicated items.
    """
    normalized = [item for item in items if item.content and item.content.strip()]
    if not normalized:
        return ""

    budget_chars = _context_budget_chars(config)
    layer_budgets = _resolve_layer_budgets(getattr(config, "packing_layer_budgets", None))
    scored = _score_and_dedup(normalized, query=query)

    if budget_chars is None:
        return "\n\n".join(_format_item(item) for item, _ in scored)

    reserve = min(64, max(0, budget_chars // 20))
    target = max(0, budget_chars - reserve)
    selected: list[MemoryContextItem] = []
    selected_ids: set[int] = set()

    # First pass: honor per-layer budgets so operational constraints and stable
    # cards are not starved by verbose recent observations.
    for layer in _LAYER_ORDER:
        layer_limit = max(1, int(target * layer_budgets.get(layer, 0)))
        used = 0
        for item, _score in (entry for entry in scored if entry[0].layer == layer):
            item_id = id(item)
            if item_id in selected_ids:
                continue
            formatted = _format_item(item)
            cost = len(formatted) + (2 if selected else 0)
            if used + cost <= layer_limit or (not selected and cost <= target):
                selected.append(item)
                selected_ids.add(item_id)
                used += cost

    # Second pass: fill remaining global budget with highest-scoring leftovers.
    used_total = len("\n\n".join(_format_item(item) for item in selected))
    for item, _score in scored:
        item_id = id(item)
        if item_id in selected_ids:
            continue
        formatted = _format_item(item)
        cost = len(formatted) + (2 if selected else 0)
        if used_total + cost <= target:
            selected.append(item)
            selected_ids.add(item_id)
            used_total += cost

    # If every complete item is too large, keep the single best item and trim it.
    if not selected and scored:
        selected = [scored[0][0]]

    packed = "\n\n".join(_format_item(item) for item in selected)
    return _truncate_to_chars(packed, budget_chars)


def build_items_from_sections(
    *,
    base_context: str = "",
    dialectic_result: str = "",
) -> list[MemoryContextItem]:
    """Convert formatted Honcho context text into packable items."""
    items: list[MemoryContextItem] = []
    for title, content in _split_sections(base_context):
        layer = _layer_for_title(title)
        items.append(MemoryContextItem(
            title=title,
            content=content,
            layer=layer,
            importance=_importance_for_title(title, content),
            recency=_recency_for_title(title),
            safety_boost=_safety_boost(content),
        ))
    if dialectic_result and dialectic_result.strip():
        for title, content in _split_sections(dialectic_result, default_title="Dialectic Supplement"):
            items.append(MemoryContextItem(
                title=title if title.startswith("Dialectic") else "Dialectic Supplement",
                content=content,
                layer="dialectic",
                importance=1.5,
                recency=2.0,
                safety_boost=_safety_boost(content),
            ))
    return items


def _context_budget_chars(config: Any) -> int | None:
    tokens = getattr(config, "context_tokens", None)
    if not tokens:
        return None
    try:
        return int(tokens) * 4
    except (TypeError, ValueError):
        return None


def _resolve_layer_budgets(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        return dict(_DEFAULT_LAYER_BUDGETS)
    resolved = dict(_DEFAULT_LAYER_BUDGETS)
    for key, value in raw.items():
        if key not in resolved:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            resolved[key] = parsed
    total = sum(resolved.values())
    if total <= 0:
        return dict(_DEFAULT_LAYER_BUDGETS)
    return {key: value / total for key, value in resolved.items()}


def _score_and_dedup(
    items: list[MemoryContextItem],
    *,
    query: str,
) -> list[tuple[MemoryContextItem, float]]:
    query_terms = _terms(query)
    ranked = sorted(
        ((item, _score(item, query_terms)) for item in items),
        key=lambda pair: pair[1],
        reverse=True,
    )

    kept: list[tuple[MemoryContextItem, float]] = []
    kept_terms: list[set[str]] = []
    for item, score in ranked:
        terms = _terms(item.content)
        duplicate_penalty = 0.0
        for existing in kept_terms:
            similarity = _jaccard(terms, existing)
            if similarity >= 0.72 or _normalized_text(item.content) in {
                _normalized_text(existing_item.content) for existing_item, _ in kept
            }:
                duplicate_penalty = max(duplicate_penalty, similarity or 1.0)
        if duplicate_penalty >= 0.72:
            continue
        kept.append((item, score - duplicate_penalty * 2.0))
        kept_terms.append(terms)
    return sorted(kept, key=lambda pair: pair[1], reverse=True)


def _score(item: MemoryContextItem, query_terms: set[str]) -> float:
    content_terms = _terms(item.content)
    relevance = _jaccard(query_terms, content_terms) if query_terms else 0.0
    safety = item.safety_boost or _safety_boost(item.content)
    layer_boost = {
        "constraints": 2.2,
        "stable": 1.4,
        "task": 1.0,
        "dialectic": 0.8,
    }.get(item.layer, 0.5)
    # Recency helps only when the item is also relevant; unrelated recent
    # observations should not beat stable preferences or constraints.
    recency = item.recency * (0.15 + min(relevance, 0.5))
    return relevance * 8.0 + item.importance * 1.2 + safety * 1.5 + recency + layer_boost


def _split_sections(text: str, *, default_title: str = "Context") -> list[tuple[str, str]]:
    text = (text or "").strip()
    if not text:
        return []
    pattern = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return [(default_title, text)]
    sections: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            sections.append((match.group(1).strip(), content))
    return sections


def _layer_for_title(title: str) -> str:
    normalized = title.lower()
    if "constraint" in normalized or "operational" in normalized:
        return "constraints"
    if "dialectic" in normalized:
        return "dialectic"
    if "summary" in normalized or "recent" in normalized or "observation" in normalized:
        return "task"
    if "representation" in normalized or "card" in normalized or "identity" in normalized:
        return "stable"
    return "task"


def _importance_for_title(title: str, content: str) -> float:
    normalized = title.lower()
    value = 1.0
    if "card" in normalized:
        value += 1.0
    if "constraint" in normalized or "operational" in normalized:
        value += 2.0
    if "representation" in normalized:
        value += 0.5
    if _safety_boost(content):
        value += 0.5
    return value


def _recency_for_title(title: str) -> float:
    normalized = title.lower()
    if "recent" in normalized or "summary" in normalized or "dialectic" in normalized:
        return 2.0
    return 0.0


def _safety_boost(text: str) -> float:
    terms = _terms(text)
    return 2.0 if terms & _SAFETY_TERMS else 0.0


def _format_item(item: MemoryContextItem) -> str:
    title = item.title.strip() or "Context"
    return f"## {title}\n{item.content.strip()}"


def _truncate_to_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    last_space = truncated.rfind(" ")
    if last_space > limit * 0.8:
        truncated = truncated[:last_space]
    return truncated.rstrip() + " …"


def _terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w\u4e00-\u9fff]+", (text or "").lower())
        if len(token) > 1 and token not in _STOPWORDS
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _normalized_text(text: str) -> str:
    return " ".join(sorted(_terms(text)))
