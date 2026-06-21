"""Tests for ExternalACPClient model matching + usage + generalization (token-maximization P1/item4).

Model menus appear in two ACP wire shapes the client must both support:
* claude-code-acp@0.16.2 — ``result.models.availableModels`` + ``session/set_model``.
* claude-agent-acp>=0.48 — ``result.configOptions[id=model].options`` +
  ``session/set_config_option {configId:"model"}`` (``result.models`` is null).
Copilot advertises neither -> no-op. These tests lock the requested-name ->
modelId mapping, the wire-method selection per shape, the PromptResponse.usage
parse (item 4 ruler), and the Copilot-compat invariants. Invariants, not snapshots.
"""

from types import SimpleNamespace

from agent.copilot_acp_client import (
    _acp_usage_to_namespace,
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


def test_claude_code_acp_threads_command_through_agent_init():
    """Regression (token-max live E2E, 2026-06-21): the gateway pins
    provider=claude-code-acp / base_url=acp://claude-code and passes the spawn
    command via the generic ``command`` kwarg. ``init_agent`` must thread it
    into ``_client_kwargs`` so the built ExternalACPClient launches
    'claude-code-acp', NOT the Copilot default 'copilot'.

    A half-broadened guard (``provider == "copilot-acp"`` only) silently
    dropped the command for claude-code-acp, so the bot launched 'copilot'
    (not installed) and erred on every routed turn. Invariant: any external
    ACP provider threads its own command + args.
    """
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="claude-code-acp",
        base_url="acp://claude-code",
        provider="claude-code-acp",
        api_mode="chat_completions",
        command="claude-code-acp",
        args=[],
        model="claude-sonnet-4-6",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    assert agent._client_kwargs.get("command") == "claude-code-acp"
    assert isinstance(agent.client, ExternalACPClient)
    assert agent.client._acp_command == "claude-code-acp"
    # claude-code-acp's explicit empty args must survive (no Copilot flags).
    assert agent.client._acp_args == []


def test_copilot_acp_command_threading_not_regressed():
    """The broadening must not regress copilot-acp: it still threads its own
    command and default --acp/--stdio flags."""
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="copilot-acp",
        base_url="acp://copilot",
        provider="copilot-acp",
        api_mode="chat_completions",
        command="copilot",
        args=["--acp", "--stdio"],
        model="gpt-5.5",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    assert agent._client_kwargs.get("command") == "copilot"
    assert agent.client._acp_command == "copilot"
    assert agent.client._acp_args == ["--acp", "--stdio"]


# ── PromptResponse.usage parse (item 4: the T2b cache-hit ruler) ──────────────

def test_acp_usage_maps_to_openai_shape():
    # claude-agent-acp PromptResponse.usage (Anthropic semantics: inputTokens
    # EXCLUDES cache) -> OpenAI chat_completions shape normalize_usage() reads.
    u = _acp_usage_to_namespace(
        {"inputTokens": 3, "outputTokens": 13, "cachedReadTokens": 100,
         "cachedWriteTokens": 25708, "totalTokens": 25824}
    )
    # prompt_tokens is the FULL input (input + cache read + cache write).
    assert u.prompt_tokens == 3 + 100 + 25708
    assert u.completion_tokens == 13
    assert u.total_tokens == 25824
    assert u.prompt_tokens_details.cached_tokens == 100
    assert u.prompt_tokens_details.cache_write_tokens == 25708


def test_acp_usage_none_is_all_zero():
    # Copilot / old claude-code-acp report no usage -> zeros (prior behavior;
    # observe_usage then falls back to the context_tokens estimate).
    u = _acp_usage_to_namespace(None)
    assert u.prompt_tokens == 0
    assert u.completion_tokens == 0
    assert u.prompt_tokens_details.cached_tokens == 0
    assert u.prompt_tokens_details.cache_write_tokens == 0


def test_acp_usage_normalizes_through_pricing():
    # End-to-end: a reused-session cache hit surfaces as cache_read in CanonicalUsage.
    from agent.usage_pricing import normalize_usage

    raw = _acp_usage_to_namespace(
        {"inputTokens": 3, "outputTokens": 50, "cachedReadTokens": 25700,
         "cachedWriteTokens": 0, "totalTokens": 25753}
    )
    cu = normalize_usage(raw, provider="claude-code-acp", api_mode="chat_completions")
    assert cu.input_tokens == 3
    assert cu.output_tokens == 50
    assert cu.cache_read_tokens == 25700
    assert cu.prompt_tokens == 25703  # input + cache_read + cache_write
    assert cu.cache_read_tokens > 0   # cache_hit=True downstream


# ── Model selection wire choice per ACP shape (set_config_option vs set_model) ──

# Real claude-agent-acp@0.48 session/new configOptions[model], value-keyed
# (captured 2026-06-22). Note: `sonnet` (included) precedes `sonnet[1m]` (which
# "Draws from usage credits" = the $400 pool); claude-opus-4-8 is an exact value.
CONFIG_OPTIONS = [
    {"id": "mode", "currentValue": "default", "options": [{"value": "default", "name": "Default"}]},
    {
        "id": "model",
        "currentValue": "claude-opus-4-8",
        "options": [
            {"value": "default", "name": "Default (recommended)", "description": "Opus 4.7 with 1M context · Most capable for complex work"},
            {"value": "sonnet", "name": "Sonnet", "description": "Sonnet 4.6 · Best for everyday tasks"},
            {"value": "sonnet[1m]", "name": "Sonnet (1M context)", "description": "Sonnet 4.6 with 1M context · Draws from usage credits · $3/$15 per Mtok"},
            {"value": "haiku", "name": "Haiku", "description": "Haiku 4.5 · Fastest for quick answers"},
            {"value": "claude-fable-5[1m]", "name": "Fable (disabled)", "description": "Claude Fable 5 is currently unavailable."},
            {"value": "claude-opus-4-8", "name": "claude-opus-4-8", "description": "Custom model"},
        ],
    },
]


def _capture_select(session, model):
    """Run _select_session_model with _acp_request stubbed; return recorded calls."""
    client = ExternalACPClient(command="/bin/echo", args=[])
    calls = []

    def _fake_request(conn, method, params, *, timeout_seconds, **_):
        calls.append((method, params))
        return {}

    client._acp_request = _fake_request  # type: ignore[assignment]
    client._select_session_model(conn=None, session=session, session_id="sid", model=model, timeout_seconds=5.0)
    return calls


def test_new_adapter_uses_set_config_option():
    calls = _capture_select({"sessionId": "sid", "models": None, "configOptions": CONFIG_OPTIONS}, "claude-sonnet-4-6")
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "session/set_config_option"
    # Cost invariant: sonnet routing must pick the INCLUDED `sonnet`, never
    # `sonnet[1m]` (which draws from $400 usage credits).
    assert params == {"sessionId": "sid", "configId": "model", "value": "sonnet"}


def test_new_adapter_noop_when_already_current():
    # Requesting the model already pinned (claude-opus-4-8 == currentValue) -> no call.
    calls = _capture_select({"sessionId": "sid", "configOptions": CONFIG_OPTIONS}, "claude-opus-4-8")
    assert calls == []


def test_old_adapter_uses_set_model():
    calls = _capture_select({"sessionId": "sid", "models": {"availableModels": MENU, "currentModelId": "default"}}, "claude-sonnet-4-6")
    assert len(calls) == 1
    method, params = calls[0]
    assert method == "session/set_model"
    assert params == {"sessionId": "sid", "modelId": "sonnet"}


def test_copilot_no_model_menu_is_noop():
    # No models, no configOptions (Copilot) -> never tries to bind a model.
    assert _capture_select({"sessionId": "sid"}, "gpt-5.5") == []
    # Also: empty model request is a no-op regardless of menu.
    assert _capture_select({"sessionId": "sid", "configOptions": CONFIG_OPTIONS}, "") == []
