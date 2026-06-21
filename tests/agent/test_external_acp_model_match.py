"""Tests for ExternalACPClient model matching + generalization (token-maximization P1).

The claude-code-acp adapter advertises a model menu (``result.models.availableModels``
with ids like ``sonnet`` / ``haiku`` / ``default`` + host-custom ids) and selection
happens via ACP ``session/set_model``. These tests lock the requested-name ->
modelId mapping and the Copilot-compatibility invariants (alias + the explicit
empty-args fix). Invariants, not snapshots.
"""

from agent.copilot_acp_client import (
    _match_acp_model_id,
    ExternalACPClient,
    CopilotACPClient,
)

# Real menu shape observed from claude-code-acp@0.16.2 session/new.
MENU = [
    {"modelId": "default", "name": "Default (recommended)", "description": "Opus 4.6 · Most capable for complex work"},
    {"modelId": "sonnet", "name": "Sonnet", "description": "Sonnet 4.5 · Best for everyday tasks"},
    {"modelId": "haiku", "name": "Haiku", "description": "Haiku 4.5 · Fastest for quick answers"},
    {"modelId": "claude-opus-4-8", "name": "claude-opus-4-8", "description": "Custom model"},
]


def test_exact_modelid_wins():
    assert _match_acp_model_id("claude-opus-4-8", MENU) == "claude-opus-4-8"


def test_family_token_sonnet_and_haiku():
    assert _match_acp_model_id("claude-sonnet-4-6", MENU) == "sonnet"
    assert _match_acp_model_id("claude-haiku-4-5", MENU) == "haiku"


def test_opus_family_falls_to_opus_capable_id():
    # No exact id; the opus family resolves to an opus-capable entry.
    assert _match_acp_model_id("claude-opus-4-6", MENU) in ("default", "claude-opus-4-8")


def test_no_match_returns_none():
    # A non-Claude model must not map to any ACP model — caller keeps the current
    # session model rather than silently picking the wrong one.
    assert _match_acp_model_id("gpt-5.5", MENU) is None
    assert _match_acp_model_id("", MENU) is None
    assert _match_acp_model_id("sonnet", []) is None


def test_external_alias_identity():
    # The generalized name and the legacy name are the same class (back-compat).
    assert ExternalACPClient is CopilotACPClient


def test_explicit_empty_args_not_overridden():
    # Regression: claude-code-acp passes args=[] (it needs no --acp/--stdio flags);
    # the constructor must NOT fall through to the Copilot default for an explicit [].
    assert ExternalACPClient(command="/bin/echo", args=[])._acp_args == []
    # But "no args provided" still defaults to Copilot's flags.
    assert ExternalACPClient(command="/bin/echo")._acp_args == ["--acp", "--stdio"]
