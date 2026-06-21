"""Claude Code ACP provider profile (Jarvis token-maximization, P1 / Path C).

Drives the official ``claude-code-acp`` adapter (npm
``@zed-industries/claude-code-acp``) as an external ACP subprocess over stdio. The
adapter wraps the Claude Agent SDK and runs under the host's existing Claude
subscription OAuth login, so requests consume the subscription's **INCLUDED weekly
quota** (Sonnet/Opus) rather than pay-as-you-go API credits — provided
``ANTHROPIC_API_KEY`` is unset and ``--bare`` is not used (see spec §T1.0).

Mirrors the bundled ``copilot-acp`` profile; the actual transport is the
(generalized) ``agent/copilot_acp_client.py:ExternalACPClient``, selected by
provider name / ``acp://`` base_url. Compliance: drives the real official adapter
with the user's own subscription login — no token pooling or identity forgery
(no-touch #7 / spec §11).
"""

from providers import register_provider
from providers.base import ProviderProfile


class ClaudeCodeACPProfile(ProviderProfile):
    """Claude Code ACP — external process, no REST models endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Model listing is handled by the ACP subprocess / configured model."""
        return None


claude_code_acp = ClaudeCodeACPProfile(
    name="claude-code-acp",
    aliases=("claude-code", "claude-subscription-acp"),
    api_mode="chat_completions",  # ACP subprocess uses chat_completions routing (like copilot-acp)
    env_vars=(),  # auth handled by the logged-in claude-code-acp subprocess
    base_url="acp://claude-code",  # ACP internal scheme; selects ExternalACPClient
    auth_type="external_process",
)

register_provider(claude_code_acp)
