"""OpenAI-compatible shim that forwards Hermes requests to `copilot --acp`.

This adapter lets Hermes treat the GitHub Copilot ACP server as a chat-style
backend. Each request starts a short-lived ACP session, sends the formatted
conversation as a single prompt, collects text chunks, and converts the result
back into the minimal shape Hermes expects from an OpenAI client.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.file_safety import get_read_block_error, is_write_denied
from agent.redact import redact_sensitive_text

ACP_MARKER_BASE_URL = "acp://copilot"
_DEFAULT_TIMEOUT_SECONDS = 900.0

logger = logging.getLogger(__name__)

_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_TOOL_CALL_JSON_RE = re.compile(r"\{\s*\"id\"\s*:\s*\"[^\"]+\"\s*,\s*\"type\"\s*:\s*\"function\"\s*,\s*\"function\"\s*:\s*\{.*?\}\s*\}", re.DOTALL)

# Stderr fingerprint of the deprecated `gh copilot` CLI extension
# (https://github.blog/changelog/2025-09-25-upcoming-deprecation-of-gh-copilot-cli-extension).
# We require BOTH the literal product name ("gh-copilot") AND a deprecation
# marker, so generic stderr from the NEW `@github/copilot` CLI — whose repo
# is github.com/github/copilot-cli and which legitimately mentions "copilot-cli"
# in its own banners and error messages — doesn't get misclassified as the
# deprecated extension.
_DEPRECATION_REQUIRED = ("gh-copilot",)
_DEPRECATION_MARKERS = (
    "has been deprecated",
    "no commands will be executed",
)


def _is_gh_copilot_deprecation_message(stderr_text: str) -> bool:
    """True iff stderr looks like the deprecated gh-copilot extension's banner."""

    lower = stderr_text.lower()
    if not any(req in lower for req in _DEPRECATION_REQUIRED):
        return False
    return any(marker in lower for marker in _DEPRECATION_MARKERS)


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_COPILOT_ACP_ARGS", "").strip()
    if not raw:
        return ["--acp", "--stdio"]
    return shlex.split(raw)


def _match_acp_model_id(requested: str, available: list[dict[str, Any]]) -> str | None:
    """Map a requested model name to an ACP ``availableModels[].modelId``.

    The claude-code-acp adapter advertises modelIds like ``sonnet`` / ``haiku`` /
    ``default`` (+ any host-custom id such as ``claude-opus-4-8``). Match exact id
    first, then a substring on id/name, then a family token (opus/sonnet/haiku)
    over id+name+description. Returns ``None`` when nothing matches (the caller
    then keeps the session's current model). Copilot ACP advertises no model menu,
    so this is never reached for it.
    """
    if not requested or not available:
        return None
    req = requested.strip().lower()
    for m in available:
        if str(m.get("modelId") or "").lower() == req:
            return str(m.get("modelId"))
    for m in available:
        mid = str(m.get("modelId") or "").lower()
        name = str(m.get("name") or "").lower()
        if mid and (req in mid or mid in req):
            return str(m.get("modelId"))
        if name and (req in name or name in req):
            return str(m.get("modelId"))
    for fam in ("opus", "sonnet", "haiku"):
        if fam in req:
            for m in available:
                blob = (
                    f"{m.get('modelId') or ''} {m.get('name') or ''} {m.get('description') or ''}"
                ).lower()
                if fam in blob:
                    return str(m.get("modelId"))
    return None


def _acp_usage_to_namespace(acp_usage: dict[str, Any] | None) -> SimpleNamespace:
    """Map an ACP ``PromptResponse.usage`` object to the OpenAI chat-completions
    usage shape that :func:`agent.usage_pricing.normalize_usage` understands.

    The claude-agent-acp adapter (>=0.48) reports per-turn token usage on the
    ``session/prompt`` result as
    ``{inputTokens, outputTokens, cachedReadTokens, cachedWriteTokens, totalTokens}``
    (Anthropic semantics: ``inputTokens`` EXCLUDES cache). The Copilot ACP server
    and the older claude-code-acp adapter report none → ``acp_usage`` is ``None``
    and we fall back to all-zero, exactly the prior behavior.

    ``normalize_usage`` runs the chat_completions branch for this provider, so we
    surface ``prompt_tokens`` as the FULL input (input + cache read + cache write)
    and break the cache out under ``prompt_tokens_details`` — the cached_tokens =
    cache reads, cache_write_tokens = cache creation. NOTE: the ``usage`` field is
    marked UNSTABLE in the ACP schema, hence the defensive shape here.
    """
    if not isinstance(acp_usage, dict):
        return SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
        )

    def _i(key: str) -> int:
        v = acp_usage.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    inp = _i("inputTokens")
    out = _i("outputTokens")
    cache_read = _i("cachedReadTokens")
    cache_write = _i("cachedWriteTokens")
    prompt_tokens = inp + cache_read + cache_write  # OpenAI: prompt includes cache
    total = _i("totalTokens") or (prompt_tokens + out)
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=out,
        total_tokens=total,
        prompt_tokens_details=SimpleNamespace(
            cached_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
    )


def _resolve_home_dir() -> str:
    """Return a stable HOME for child ACP processes."""
    home = os.environ.get("HOME", "").strip()
    if home:
        return home

    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        return expanded

    try:
        import pwd

        resolved = pwd.getpwuid(os.getuid()).pw_dir.strip()  # windows-footgun: ok — POSIX fallback inside try/except (pwd import fails on Windows)
        if resolved:
            return resolved
    except Exception:
        pass

    # Last resort: /tmp (writable on any POSIX system). Avoids crashing the
    # subprocess with no HOME; callers can set HERMES_HOME explicitly if they
    # need a different writable dir.
    return "/tmp"


def _build_subprocess_env(command: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    home = _resolve_home_dir()
    env["HOME"] = home
    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(env)
    # Never let a spawned Claude Code session inherit our nesting markers: the
    # claude-code-acp adapter boots `claude`, which refuses to run "inside another
    # Claude Code session" when CLAUDECODE is set. Only relevant when Hermes itself
    # runs inside a Claude Code session; harmless for Copilot.
    for _nest in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(_nest, None)
    # The claude-agent-acp adapter (Agent SDK 0.3.x) bundles a per-arch `claude`
    # binary and prefers it; under an x64-Rosetta node the bundled darwin-x64
    # binary hangs. Pin the adapter to a known-good native `claude` via
    # CLAUDE_CODE_EXECUTABLE (the SDK option the adapter honors:
    # `pathToClaudeCodeExecutable: process.env.CLAUDE_CODE_EXECUTABLE ?? claudeCliPath()`).
    # Only when unset (explicit override always wins) and only for claude
    # adapters (Copilot ignores this var, but we scope it to be tidy).
    if "CLAUDE_CODE_EXECUTABLE" not in env and (command is None or "claude" in os.path.basename(command).lower()):
        _claude = os.getenv("HERMES_CLAUDE_CODE_EXECUTABLE", "").strip() or shutil.which("claude")
        if _claude:
            env["CLAUDE_CODE_EXECUTABLE"] = _claude
    return env


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _permission_denied(message_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "outcome": {
                "outcome": "cancelled",
            }
        },
    }


def _format_messages_as_prompt(
    messages: list[dict[str, Any]],
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    sections: list[str] = [
        "You are being used as the active ACP agent backend for Hermes.",
        "Use ACP capabilities to complete tasks.",
        "IMPORTANT: If you take an action with a tool, you MUST output tool calls using <tool_call>{...}</tool_call> blocks with JSON exactly in OpenAI function-call shape.",
        "If no tool is needed, answer normally.",
    ]
    if model:
        sections.append(f"Hermes requested model hint: {model}")

    if isinstance(tools, list) and tools:
        tool_specs: list[dict[str, Any]] = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function") or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            tool_specs.append(
                {
                    "name": name.strip(),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }
            )
        if tool_specs:
            sections.append(
                "Available tools (OpenAI function schema). "
                "When using a tool, emit ONLY <tool_call>{...}</tool_call> with one JSON object "
                "containing id/type/function{name,arguments}. arguments must be a JSON string.\n"
                + json.dumps(tool_specs, ensure_ascii=False)
            )

    if tool_choice is not None:
        sections.append(f"Tool choice hint: {json.dumps(tool_choice, ensure_ascii=False)}")

    transcript: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").strip().lower()
        if role == "tool":
            role = "tool"
        elif role not in {"system", "user", "assistant"}:
            role = "context"

        content = message.get("content")
        rendered = _render_message_content(content)
        if not rendered:
            continue

        label = {
            "system": "System",
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool",
            "context": "Context",
        }.get(role, role.title())
        transcript.append(f"{label}:\n{rendered}")

    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))

    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        if "content" in content and isinstance(content.get("content"), str):
            return str(content.get("content") or "").strip()
        return json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return str(content).strip()


def _extract_tool_calls_from_text(text: str) -> tuple[list[SimpleNamespace], str]:
    if not isinstance(text, str) or not text.strip():
        return [], ""

    extracted: list[SimpleNamespace] = []
    consumed_spans: list[tuple[int, int]] = []

    def _try_add_tool_call(raw_json: str) -> None:
        try:
            obj = json.loads(raw_json)
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        fn = obj.get("function")
        if not isinstance(fn, dict):
            return
        fn_name = fn.get("name")
        if not isinstance(fn_name, str) or not fn_name.strip():
            return
        fn_args = fn.get("arguments", "{}")
        if not isinstance(fn_args, str):
            fn_args = json.dumps(fn_args, ensure_ascii=False)
        call_id = obj.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"acp_call_{len(extracted)+1}"

        extracted.append(
            SimpleNamespace(
                id=call_id,
                call_id=call_id,
                response_item_id=None,
                type="function",
                function=SimpleNamespace(name=fn_name.strip(), arguments=fn_args),
            )
        )

    for m in _TOOL_CALL_BLOCK_RE.finditer(text):
        raw = m.group(1)
        _try_add_tool_call(raw)
        consumed_spans.append((m.start(), m.end()))

    # Only try bare-JSON fallback when no XML blocks were found.
    if not extracted:
        for m in _TOOL_CALL_JSON_RE.finditer(text):
            raw = m.group(0)
            _try_add_tool_call(raw)
            consumed_spans.append((m.start(), m.end()))

    if not consumed_spans:
        return extracted, text.strip()

    consumed_spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in consumed_spans:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(text):
        parts.append(text[cursor:])

    cleaned = "\n".join(p.strip() for p in parts if p and p.strip()).strip()
    return extracted, cleaned



def _ensure_path_within_cwd(path_text: str, cwd: str) -> Path:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved = candidate.resolve()
    root = Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside the session cwd '{root}'.") from exc
    return resolved


def _acp_persistence_settings() -> tuple[bool, int, float]:
    """``(persistent, pool_size, idle_timeout_seconds)`` from config (T2a).

    Read from ``route_decision.{acp_persistent_process,acp_pool_size,
    acp_idle_timeout_seconds}`` via the runtime-safe ``load_config_readonly``
    (the same loader the gateway loop uses for ``token_budget`` — see
    SELF_ARCHITECTURE §7 on the 3-loader gotcha). Defaults ``(False, 5, 120)``:
    when disabled the client spawns a throwaway process per call exactly as
    before (Copilot path unchanged). ``load_config_readonly`` is mtime-cached,
    so calling this per prompt is cheap.
    """
    try:
        from hermes_cli.config import load_config_readonly

        rd = (load_config_readonly() or {}).get("route_decision", {}) or {}
        persistent = bool(rd.get("acp_persistent_process", False))
        pool_size = int(rd.get("acp_pool_size", 5) or 5)
        idle = float(rd.get("acp_idle_timeout_seconds", 120) or 120)
        return persistent, max(1, pool_size), max(1.0, idle)
    except Exception:
        return False, 5, 120.0


class _AcpConnection:
    """One ACP subprocess + its stdio reader threads and JSON-RPC inbox.

    Owns the transport for an external ACP CLI (Copilot or claude-code-acp).
    When persistence is enabled it is reused across prompts (kept warm in the
    pool); otherwise it is created throwaway for a single prompt. NOT safe for
    concurrent prompts — the owner serializes via ``lock``. ``initialized``
    tracks the once-per-connection ACP ``initialize`` handshake so reuse skips
    the cold start.
    """

    def __init__(self, command: str, args: list[str], cwd: str) -> None:
        self.command = command
        self.args = list(args)
        self.cwd = cwd
        self.proc: subprocess.Popen[str] | None = None
        self.inbox: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.next_id = 0
        self.initialized = False
        self.last_used = 0.0
        self.lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        try:
            proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=_build_subprocess_env(self.command),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start Copilot ACP command '{self.command}'. "
                "Install GitHub Copilot CLI or set HERMES_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH."
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("Copilot ACP process did not expose stdin/stdout pipes.")

        self.proc = proc

        def _stdout_reader() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                try:
                    self.inbox.put(json.loads(line))
                except Exception:
                    self.inbox.put({"raw": line.rstrip("\n")})

        def _stderr_reader() -> None:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip("\n"))

        threading.Thread(target=_stdout_reader, daemon=True).start()
        threading.Thread(target=_stderr_reader, daemon=True).start()

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        self.initialized = False
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class _AcpPool:
    """Module-level pool of warm :class:`_AcpConnection`\\ s, keyed by
    ``(command, args, cwd)`` (T2a).

    Persistence must live here, NOT on the client instance: the live API call
    runs on a fresh per-request ``shared=False`` client that the loop closes
    after every call, so a connection cached on the client would be re-spawned
    each turn. Reaping is lazy (on acquire/release; no background thread) +
    ``atexit`` so we never leak node/Claude child processes.
    """

    def __init__(self) -> None:
        self._idle: dict[tuple, list[_AcpConnection]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(command: str, args: list[str], cwd: str) -> tuple:
        return (command, tuple(args), cwd)

    def acquire(self, command: str, args: list[str], cwd: str, idle_timeout: float) -> _AcpConnection:
        """Return a warm connection (reused if alive and within the idle
        window), else a freshly started one. Dead/stale idle conns are reaped."""
        key = self._key(command, args, cwd)
        now = time.monotonic()
        chosen: _AcpConnection | None = None
        with self._lock:
            bucket = self._idle.get(key) or []
            survivors: list[_AcpConnection] = []
            for conn in bucket:
                if conn.alive() and (now - conn.last_used) <= idle_timeout:
                    if chosen is None:
                        chosen = conn  # take the first reusable; leave the rest warm
                    else:
                        survivors.append(conn)
                else:
                    conn.close()  # dead or idle-expired → reap
            self._idle[key] = survivors
        if chosen is not None:
            return chosen
        conn = _AcpConnection(command, args, cwd)
        conn.start()
        return conn

    def release(
        self,
        command: str,
        args: list[str],
        cwd: str,
        conn: _AcpConnection,
        *,
        pool_size: int,
        idle_timeout: float,
    ) -> None:
        """Return a connection to the pool (kept warm) if alive and there's
        room; otherwise close it. Also lazily reaps dead/idle-expired peers."""
        key = self._key(command, args, cwd)
        now = time.monotonic()
        conn.last_used = now
        with self._lock:
            bucket = self._idle.get(key) or []
            survivors = [
                c for c in bucket
                if c.alive() and (now - c.last_used) <= idle_timeout
            ]
            for c in bucket:
                if c not in survivors:
                    c.close()
            if conn.alive() and len(survivors) < pool_size:
                survivors.append(conn)
            else:
                conn.close()  # pool full or conn dead → discard
            self._idle[key] = survivors

    def close_all(self) -> None:
        with self._lock:
            for bucket in self._idle.values():
                for conn in bucket:
                    conn.close()
            self._idle.clear()


_POOL = _AcpPool()
atexit.register(_POOL.close_all)


class _ACPChatCompletions:
    def __init__(self, client: "CopilotACPClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ACPChatNamespace:
    def __init__(self, client: "CopilotACPClient"):
        self.completions = _ACPChatCompletions(client)


class CopilotACPClient:
    """Minimal OpenAI-client-compatible facade for Copilot ACP."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "copilot-acp"
        self.base_url = base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command()
        # Distinguish an explicit empty arg list (e.g. the claude-code-acp adapter
        # needs NO --acp/--stdio flags) from "args not provided". A plain
        # `acp_args or args or default` would treat ``[]`` as falsy and wrongly
        # fall through to the Copilot default.
        _args = acp_args if acp_args is not None else args
        self._acp_args = list(_args if _args is not None else _resolve_args())
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = _ACPChatNamespace(self)
        self.is_closed = False
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_lock = threading.Lock()

    def close(self) -> None:
        proc: subprocess.Popen[str] | None
        with self._active_process_lock:
            proc = self._active_process
            self._active_process = None
        self.is_closed = True
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_: Any,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(
            messages or [],
            model=model,
            tools=tools,
            tool_choice=tool_choice,
        )
        # Normalise timeout: run_agent.py may pass an httpx.Timeout object
        # (used natively by the OpenAI SDK) rather than a plain float.
        if timeout is None:
            _effective_timeout = _DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, (int, float)):
            _effective_timeout = float(timeout)
        else:
            # httpx.Timeout or similar — pick the largest component so the
            # subprocess has enough wall-clock time for the full response.
            _candidates = [
                getattr(timeout, attr, None)
                for attr in ("read", "write", "connect", "pool", "timeout")
            ]
            _numeric = [float(v) for v in _candidates if isinstance(v, (int, float))]
            _effective_timeout = max(_numeric) if _numeric else _DEFAULT_TIMEOUT_SECONDS

        response_text, reasoning_text, acp_usage = self._run_prompt(
            prompt_text,
            timeout_seconds=_effective_timeout,
            model=model,
        )

        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)

        usage = _acp_usage_to_namespace(acp_usage)
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        choice = SimpleNamespace(message=assistant_message, finish_reason=finish_reason)
        return SimpleNamespace(
            choices=[choice],
            usage=usage,
            model=model or "copilot-acp",
        )

    def _run_prompt(self, prompt_text: str, *, timeout_seconds: float, model: str | None = None) -> tuple[str, str, dict[str, Any] | None]:
        persistent, pool_size, idle_timeout = _acp_persistence_settings()

        if persistent:
            # T2a: borrow a warm process from the pool, run the prompt, return
            # it warm. The pool owns the lifecycle, so the per-request client's
            # post-call close() must NOT kill it — we expose the process via
            # _active_process only for the duration of the prompt (so an
            # interrupt can still abort), then clear it.
            conn = _POOL.acquire(self._acp_command, self._acp_args, self._acp_cwd, idle_timeout)
            with conn.lock:
                with self._active_process_lock:
                    self._active_process = conn.proc
                    self.is_closed = False
                try:
                    result = self._run_on_connection(conn, prompt_text, timeout_seconds, model)
                except Exception:
                    conn.close()  # poison a broken connection — never re-pool it
                    raise
                finally:
                    with self._active_process_lock:
                        self._active_process = None
            _POOL.release(
                self._acp_command, self._acp_args, self._acp_cwd, conn,
                pool_size=pool_size, idle_timeout=idle_timeout,
            )
            return result

        # Default (persistence off): one throwaway process per call —
        # behaviorally identical to the original per-call spawn.
        conn = _AcpConnection(self._acp_command, self._acp_args, self._acp_cwd)
        try:
            conn.start()
            with self._active_process_lock:
                self._active_process = conn.proc
                self.is_closed = False
            return self._run_on_connection(conn, prompt_text, timeout_seconds, model)
        finally:
            conn.close()
            with self._active_process_lock:
                self._active_process = None
            self.is_closed = True

    def _run_on_connection(
        self,
        conn: "_AcpConnection",
        prompt_text: str,
        timeout_seconds: float,
        model: str | None,
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Run one ACP prompt cycle on ``conn``: initialize (once per
        connection), open a fresh session, bind the model, send the prompt,
        collect text. A fresh ``session/new`` per call keeps output identical
        to the per-call-spawn path — only the spawn + ``initialize`` cold start
        is amortized when the connection is reused."""
        if conn.proc is None:
            conn.start()
            with self._active_process_lock:
                self._active_process = conn.proc

        # Drain any trailing messages from a prior prompt on a reused
        # connection so late notifications can't bleed into this prompt.
        while True:
            try:
                conn.inbox.get_nowait()
            except queue.Empty:
                break

        if not conn.initialized:
            self._acp_request(
                conn,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": True,
                            "writeTextFile": True,
                        }
                    },
                    "clientInfo": {
                        "name": "hermes-agent",
                        "title": "Hermes Agent",
                        "version": "0.0.0",
                    },
                },
                timeout_seconds=timeout_seconds,
            )
            conn.initialized = True

        session = self._acp_request(
            conn,
            "session/new",
            {
                "cwd": conn.cwd,
                "mcpServers": [],
            },
            timeout_seconds=timeout_seconds,
        ) or {}
        session_id = str(session.get("sessionId") or "").strip()
        if not session_id:
            raise RuntimeError("Copilot ACP did not return a sessionId.")

        # Bind the requested model on this session. The model is fixed once at
        # session start — never switched mid-session (cache-safe; no-touch #1).
        self._select_session_model(conn, session, session_id, model, timeout_seconds)

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        prompt_result = self._acp_request(
            conn,
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [
                    {
                        "type": "text",
                        "text": prompt_text,
                    }
                ],
            },
            timeout_seconds=timeout_seconds,
            text_parts=text_parts,
            reasoning_parts=reasoning_parts,
        )
        # ``PromptResponse.usage`` (claude-agent-acp >=0.48; UNSTABLE field) carries
        # real per-turn tokens incl. cache reads — the only truthful usage signal on
        # the ACP path. Absent (Copilot / old claude-code-acp) -> None -> zeros.
        acp_usage = None
        if isinstance(prompt_result, dict):
            _u = prompt_result.get("usage")
            if isinstance(_u, dict):
                acp_usage = _u
        return "".join(text_parts), "".join(reasoning_parts), acp_usage

    def _select_session_model(
        self,
        conn: "_AcpConnection",
        session: dict[str, Any],
        session_id: str,
        model: str | None,
        timeout_seconds: float,
    ) -> None:
        """Pin the requested model on a fresh session, supporting both ACP wire
        shapes (back-compat) and no-op for servers without a model menu:

        * claude-agent-acp (>=0.48): model lives in ``result.configOptions[id=model]``
          and is set via ``session/set_config_option {configId:"model", value}``.
          (``result.models`` is ``null`` here.)
        * claude-code-acp (0.16.2): model lives in ``result.models.availableModels``
          and is set via ``session/set_model {modelId}``.
        * Copilot ACP: advertises neither -> no-op.
        """
        if not model:
            return
        try:
            # New adapter: configOptions[id=model] + session/set_config_option
            for opt in (session.get("configOptions") or []):
                if not (isinstance(opt, dict) and opt.get("id") == "model"):
                    continue
                options = opt.get("options") or []
                current = str(opt.get("currentValue") or "").strip()
                mapped = [
                    {
                        "modelId": o.get("value"),
                        "name": o.get("name"),
                        "description": o.get("description"),
                    }
                    for o in options
                    if isinstance(o, dict)
                ]
                target = _match_acp_model_id(model, mapped)
                if target and target != current:
                    self._acp_request(
                        conn,
                        "session/set_config_option",
                        {"sessionId": session_id, "configId": "model", "value": target},
                        timeout_seconds=timeout_seconds,
                    )
                    logger.info(
                        "ACP session/set_config_option model: %s -> %s (requested %r)",
                        current or "?", target, model,
                    )
                return
            # Old adapter: result.models.availableModels + session/set_model
            models = session.get("models") or {}
            available = models.get("availableModels") or []
            if available:
                current = str(models.get("currentModelId") or "").strip()
                target = _match_acp_model_id(model, available)
                if target and target != current:
                    self._acp_request(
                        conn,
                        "session/set_model",
                        {"sessionId": session_id, "modelId": target},
                        timeout_seconds=timeout_seconds,
                    )
                    logger.info(
                        "ACP session/set_model: %s -> %s (requested %r)",
                        current or "?", target, model,
                    )
                return
            # Copilot / no model menu: nothing to bind.
        except Exception:
            logger.debug("ACP model selection skipped/failed", exc_info=True)

    def _acp_request(
        self,
        conn: "_AcpConnection",
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        text_parts: list[str] | None = None,
        reasoning_parts: list[str] | None = None,
    ) -> Any:
        proc = conn.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("Copilot ACP process is not running.")
        conn.next_id += 1
        request_id = conn.next_id
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                msg = conn.inbox.get(timeout=0.1)
            except queue.Empty:
                continue

            if self._handle_server_message(
                msg,
                process=proc,
                cwd=conn.cwd,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
            ):
                continue

            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                err = msg.get("error") or {}
                raise RuntimeError(
                    f"Copilot ACP {method} failed: {err.get('message') or err}"
                )
            return msg.get("result")

        stderr_text = "\n".join(conn.stderr_tail).strip()
        if proc.poll() is not None and stderr_text:
            if _is_gh_copilot_deprecation_message(stderr_text):
                raise RuntimeError(
                    "Hermes ACP mode requires the NEW GitHub Copilot CLI "
                    "(github.com/github/copilot-cli), but the binary it just "
                    "spawned is the deprecated `gh copilot` extension.\n\n"
                    "Install the new CLI:\n"
                    "  npm install -g @github/copilot\n"
                    "  # then verify with: copilot --help\n\n"
                    "If `copilot` already resolves to the new CLI but you still see this,\n"
                    "point Hermes at it explicitly:\n"
                    "  export HERMES_COPILOT_ACP_COMMAND=/path/to/new/copilot\n\n"
                    "Alternative: use the `copilot` provider (no ACP, hits the Copilot API\n"
                    "directly with a Copilot subscription token) via `hermes setup`.\n\n"
                    f"Original error:\n{stderr_text}"
                )
            raise RuntimeError(f"Copilot ACP process exited early: {stderr_text}")
        raise TimeoutError(f"Timed out waiting for Copilot ACP response to {method}.")

    def _handle_server_message(
        self,
        msg: dict[str, Any],
        *,
        process: subprocess.Popen[str],
        cwd: str,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
    ) -> bool:
        method = msg.get("method")
        if not isinstance(method, str):
            return False

        if method == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            kind = str(update.get("sessionUpdate") or "").strip()
            content = update.get("content") or {}
            chunk_text = ""
            if isinstance(content, dict):
                chunk_text = str(content.get("text") or "")
            if kind == "agent_message_chunk" and chunk_text and text_parts is not None:
                text_parts.append(chunk_text)
            elif kind == "agent_thought_chunk" and chunk_text and reasoning_parts is not None:
                reasoning_parts.append(chunk_text)
            return True

        if process.stdin is None:
            return True

        message_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "session/request_permission":
            response = _permission_denied(message_id)
        elif method == "fs/read_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                block_error = get_read_block_error(str(path))
                if block_error:
                    raise PermissionError(block_error)
                try:
                    content = path.read_text()
                except FileNotFoundError:
                    content = ""
                line = params.get("line")
                limit = params.get("limit")
                if isinstance(line, int) and line > 1:
                    lines = content.splitlines(keepends=True)
                    start = line - 1
                    end = start + limit if isinstance(limit, int) and limit > 0 else None
                    content = "".join(lines[start:end])
                if content:
                    content = redact_sensitive_text(content, force=True)
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "content": content,
                    },
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        elif method == "fs/write_text_file":
            try:
                path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                if is_write_denied(str(path)):
                    raise PermissionError(
                        f"Write denied: '{path}' is a protected system/credential file."
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content") or ""))
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": None,
                }
            except Exception as exc:
                response = _jsonrpc_error(message_id, -32602, str(exc))
        else:
            response = _jsonrpc_error(
                message_id,
                -32601,
                f"ACP client method '{method}' is not supported by Hermes yet.",
            )

        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True


# Provider-agnostic alias. This facade is not Copilot-specific: the command, args,
# base_url and api_key are all injected per provider, and the ACP wire protocol
# (initialize / session/new / session/prompt / session/update chunks) is the Zed
# Agent Client Protocol that any conformant ACP-over-stdio CLI speaks. The Jarvis
# token-maximization Path C drives the official `claude-code-acp` adapter through
# this same client. New call sites should import ``ExternalACPClient``.
ExternalACPClient = CopilotACPClient
