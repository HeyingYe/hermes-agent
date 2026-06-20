"""Tests for Honcho ranked context packing."""

from __future__ import annotations

import json
from types import SimpleNamespace

from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho.packing import (
    MemoryContextItem,
    build_items_from_sections,
    pack_memory_context,
)


def _cfg(tokens=80, packing_enabled=True):
    return SimpleNamespace(
        context_tokens=tokens,
        packing_enabled=packing_enabled,
        packing_layer_budgets={
            "stable": 0.30,
            "task": 0.30,
            "constraints": 0.25,
            "dialectic": 0.15,
        },
    )


def test_honcho_packing_config_is_disabled_by_default(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"apiKey": "key"}))

    cfg = HonchoClientConfig.from_global_config(config_path=config_file)

    assert cfg.packing_enabled is False
    assert cfg.packing_layer_budgets == {}


def test_honcho_packing_config_host_block_wins(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "apiKey": "key",
        "contextPacking": {"enabled": True, "layerBudgets": {"stable": 0.9}},
        "hosts": {"hermes": {"contextPacking": {"enabled": False}}},
    }))

    cfg = HonchoClientConfig.from_global_config(config_path=config_file)

    assert cfg.packing_enabled is False
    assert cfg.packing_layer_budgets == {}


def test_ranked_packer_preserves_context_packing_query_over_unrelated_recency():
    """Representative query 1: current Honcho packing work."""
    items = [
        MemoryContextItem("User Representation", "User likes baseball and old trivia. " * 20, layer="stable"),
        MemoryContextItem("User Peer Card", "User prefers concise security-first answers.", layer="stable", importance=3),
        MemoryContextItem("Operational Constraints", "Never expose secrets. Ask before irreversible operations.", layer="constraints", safety_boost=4),
        MemoryContextItem("Recent Observation", "Discussed pizza toppings yesterday. " * 8, layer="task", recency=4),
        MemoryContextItem("Recent Observation", "Working on ranked context packing for Honcho Hermes memory.", layer="task", recency=3),
        MemoryContextItem("Dialectic Supplement", "The user is currently implementing Honcho context packing and deduplication.", layer="dialectic"),
    ]

    packed = pack_memory_context(
        items,
        query="implement ranked context packing and deduplication for Honcho Hermes",
        config=_cfg(tokens=80),
    )
    aggressive = "\n\n".join(f"## {item.title}\n{item.content}" for item in items)

    assert "ranked context packing" in packed
    assert "pizza toppings" not in packed
    assert packed.index("ranked context packing") < aggressive.index("pizza toppings")
    assert "Never expose secrets" in packed
    assert "concise security-first" in packed
    assert len(packed) < len(aggressive)
    assert len(packed) <= 80 * 4 + 5


def test_ranked_packer_preserves_lark_delivery_safety_over_chatty_recent_history():
    """Representative query 2: Lark delivery and bot safety work."""
    items = [
        MemoryContextItem(
            "User Representation",
            "User is an Agent developer. User prefers privacy, safety, and permission boundaries first.",
            layer="stable",
            importance=2,
        ),
        MemoryContextItem(
            "User Peer Card",
            "User prefers privacy, safety, and permission boundaries first.",
            layer="stable",
            importance=3,
        ),
        MemoryContextItem(
            "Operational Constraints",
            "For bot group delivery, explain permissions and use an allowlist before enabling automation.",
            layer="constraints",
            safety_boost=4,
        ),
        MemoryContextItem(
            "Recent Observation",
            "Talked about laptop stickers, coffee preferences, and a weekend reading list. " * 5,
            layer="task",
            recency=5,
        ),
        MemoryContextItem(
            "Recent Observation",
            "Configured Lark weekly digest delivery with explicit chat_id and single message card formatting.",
            layer="task",
            recency=3,
        ),
        MemoryContextItem(
            "Dialectic Supplement",
            "The next answer should focus on Lark bot permissions, allowlisted delivery, and card formatting.",
            layer="dialectic",
        ),
    ]

    packed = pack_memory_context(
        items,
        query="enable Lark bot group delivery with permissions allowlist and single card format",
        config=_cfg(tokens=95),
    )
    aggressive = "\n\n".join(f"## {item.title}\n{item.content}" for item in items)

    assert "allowlist" in packed
    assert "permissions" in packed
    assert "single message card" in packed or "card formatting" in packed
    assert "laptop stickers" not in packed
    assert packed.count("privacy, safety, and permission boundaries first") == 1
    assert len(packed) < len(aggressive)
    assert len(packed) <= 95 * 4 + 5


def test_ranked_packer_preserves_github_capability_context_and_hybrid_tools():
    """Representative query 3: GitHub/Honcho tool-capability work."""
    items = [
        MemoryContextItem(
            "User Peer Card",
            "User GitHub account is HeyingYe and prefers ed25519 SSH authentication.",
            layer="stable",
            importance=3,
        ),
        MemoryContextItem(
            "User Representation",
            "User GitHub account is HeyingYe and prefers ed25519 SSH authentication.",
            layer="stable",
            importance=2,
        ),
        MemoryContextItem(
            "Operational Constraints",
            "Never print tokens or secrets. Keep credential handling permission-safe.",
            layer="constraints",
            safety_boost=4,
        ),
        MemoryContextItem(
            "Recent Observation",
            "Reviewed recipes, desk setup ideas, and non-work podcast notes. " * 6,
            layer="task",
            recency=5,
        ),
        MemoryContextItem(
            "Recent Observation",
            "Investigating Honcho recallMode hybrid so auto context and memory tools remain available.",
            layer="task",
            recency=2,
        ),
        MemoryContextItem(
            "Dialectic Supplement",
            "For repository work, preserve Honcho tool paths such as honcho_search and honcho_context.",
            layer="dialectic",
        ),
    ]

    packed = pack_memory_context(
        items,
        query="GitHub repo work with Honcho recallMode hybrid and memory tool capability paths",
        config=_cfg(tokens=115),
    )
    aggressive = "\n\n".join(f"## {item.title}\n{item.content}" for item in items)

    provider = HonchoMemoryProvider()
    provider._recall_mode = "hybrid"
    tool_names = {schema["name"] for schema in provider.get_tool_schemas()}

    assert "Honcho recallMode hybrid" in packed
    assert "honcho_search" in packed or "honcho_context" in packed
    assert "Never print tokens" in packed
    assert "podcast notes" not in packed
    assert packed.count("GitHub account is HeyingYe") == 1
    assert {"honcho_search", "honcho_context", "honcho_reasoning"} <= tool_names
    assert len(packed) < len(aggressive)
    assert len(packed) <= 115 * 4 + 5


def test_provider_uses_ranked_packer_only_when_enabled():
    provider = HonchoMemoryProvider()
    provider._config = _cfg(tokens=50, packing_enabled=True)

    packed = provider._pack_or_truncate_context(
        base_context=(
            "## User Representation\nUnrelated golf context. " * 20
            + "\n\n## User Peer Card\nUser prefers concise security-first answers."
        ),
        dialectic_result="## Dialectic Supplement\nCurrent work: ranked context packing for Honcho.",
        query="ranked context packing Honcho",
    )

    assert "ranked context packing" in packed
    assert "concise security-first" in packed
    assert len(packed) <= 50 * 4 + 5


def test_build_items_from_sections_assigns_packable_layers_for_honcho_sections():
    items = build_items_from_sections(
        base_context=(
            "## User Representation\nStable user preference.\n\n"
            "## User Peer Card\nDurable card fact.\n\n"
            "## Summary\nRecent task-relevant observation.\n\n"
            "## Operational Constraints\nNever expose secrets."
        ),
        dialectic_result="## Dialectic Supplement\nReasoning supplement.",
    )

    assert [(item.title, item.layer) for item in items] == [
        ("User Representation", "stable"),
        ("User Peer Card", "stable"),
        ("Summary", "task"),
        ("Operational Constraints", "constraints"),
        ("Dialectic Supplement", "dialectic"),
    ]
