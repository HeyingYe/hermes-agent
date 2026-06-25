# Hermes — Self-Architecture Map ☤

> **Audience: me, Hermes, working on my own codebase.**
> This is my runtime self-model — how my pieces actually fit together at execution
> time, and *where to look first* when a task is about me (a bug in my own loop, a
> self-optimization, a regression in a subsystem I own).
>
> **Relationship to `AGENTS.md`:** `AGENTS.md` is the *intent / contribution
> rubric* — what gets merged, what gets rejected, the Footprint Ladder, the testing
> contract. **This file is the *runtime map*** — the control flow and the
> "start-here" pointers. When fixing myself: read this for *where the code is*, read
> `AGENTS.md` for *whether/how a change is allowed*. They are complementary; this
> file does not repeat the rubric.
>
> **How to use the pointers.** Every pointer is `file:line` *relative to the repo
> root* (`$HERMES_HOME/hermes-agent/`, currently `~/Jarvis/.hermes/hermes-agent/`). **Line numbers drift — function and class
> names do not.** Always navigate by the named symbol; treat the line number as a
> hint, not a contract. Snapshot refreshed **2026-06-23** against the working checkout.
> The canonical source is always the filesystem — when a pointer is stale, re-grep
> for the symbol name.

---

## 1. What I Am

I am a **personal AI agent that runs one agent core across many surfaces**: an
interactive CLI, a messaging gateway (~20 platforms), a TUI, an Electron desktop
app, an ACP server (IDE integration), and a web dashboard. I learn across sessions
(memory + skills), delegate to subagents, run scheduled jobs, and drive a real
terminal and browser. I am extended **at the edges** (plugins, skills, MCP
servers), not by growing the core.

### Two invariants that shape almost every decision

1. **Per-conversation prompt caching is sacred.** A long conversation reuses a
   cached prefix every turn. Anything that mutates past context, swaps toolsets, or
   rebuilds the system prompt **mid-conversation** invalidates that cache and
   multiplies cost. The *only* sanctioned exception is context compression (which
   deliberately rotates the session). If I'm tempted to change context mid-turn,
   I'm almost certainly wrong.
2. **The core is a narrow waist; capability lives at the edges.** Every model tool
   ships on *every* API call. New core tools are the expensive last resort. New
   capability should arrive as: extend existing code → CLI command + skill →
   service-gated tool → plugin → MCP server → (last) core tool. (Footprint Ladder,
   `AGENTS.md`.)

Corollaries I must preserve in any change to the loop: **strict message role
alternation** (never two same-role messages in a row; no synthetic user message
injected mid-loop) and a **byte-stable system prompt** for the life of a
conversation.

### My surfaces (all share the same `AIAgent` core)

| Surface | Entry | Backend it drives |
|---|---|---|
| Interactive CLI | `hermes` → `HermesCLI` (`cli.py`) | `AIAgent` directly |
| Messaging gateway | `hermes gateway` → `gateway/run.py` | `AIAgent` per session |
| TUI (Ink/React) | `hermes --tui` | `tui_gateway/` (Python JSON-RPC) → `AIAgent` |
| Web dashboard `/chat` | `hermes dashboard` | embeds the real `hermes --tui` via PTY bridge |
| Electron desktop | `apps/desktop/` | its own `tui_gateway` JSON-RPC client |
| IDE (VS Code/Zed) | ACP | `acp_adapter/` → `AIAgent` |

---

## 2. Top-Level Module Map

The load-bearing files at the repo root and what they own. (God-files are huge by
history; extracting clusters out of them into modules is *welcome* work — see
`AGENTS.md` "Refactor god-files".)

| File / dir | LOC | Owns |
|---|---|---|
| `run_agent.py` | ~5.5k | `AIAgent` class — construction, the public `chat()`/`run_conversation()`, tool-call execution routing (`_execute_tool_calls`) |
| `agent/conversation_loop.py` | ~4.6k | **The actual tool-calling loop body** (`run_conversation()` lives here; `run_agent.py` forwards to it) |
| `agent/tool_executor.py` | ~1.4k | Sequential/concurrent tool-call execution helpers extracted from the loop; inline agent-runtime tools + hook emission + guardrail/file-mutation tracking |
| `agent/turn_finalizer.py` | ~430 | Post-loop finalization: budget-exhaustion summary, trajectory/session persistence, turn diagnostics, response transforms, memory/skill review trigger |
| `agent/system_prompt.py` | — | System-prompt assembly (`build_system_prompt_parts`, `build_system_prompt`) — the 3-tier cache structure |
| `model_tools.py` | ~1.2k | Tool orchestration: `discover_builtin_tools()`, `get_tool_definitions()`, `handle_function_call()` |
| `tools/registry.py` | ~590 | `ToolRegistry` singleton (`registry`), `register()`, auto-discovery |
| `toolsets.py` | ~900 | `TOOLSETS` dict, `_HERMES_CORE_TOOLS`, `resolve_toolset()` |
| `tools/*.py` | ~87 files | Individual tool implementations (self-register at import) |
| `cli.py` | ~15k | `HermesCLI` — interactive CLI orchestrator, `process_command()` |
| `hermes_cli/` | ~129 files | CLI subcommands, setup wizard, config, plugins loader, skin engine, kanban/curator CLIs; `kanban_db.py` is the multi-board CAS/WAL coordination layer |
| `gateway/` | ~36 files | Messaging gateway: `run.py` (runner, session runtime, empty-response normalization), `session.py`, `status.py`, `platforms/` |
| `hermes_state.py` | ~5k | `SessionDB` — SQLite session+message store with FTS5 search |
| `hermes_constants.py` | — | `get_hermes_home()`, `display_hermes_home()` — **profile-aware paths** |
| `hermes_logging.py` | — | `setup_logging()` — `agent.log` / `errors.log` / `gateway.log` |
| `agent/` | ~100 files | Provider adapters, memory, context/compression, curator, credential pool, display, error classifier, etc. |
| `providers/` + `plugins/model-providers/` | — | Inference backends (provider plugin system) |
| `plugins/` | — | Plugin surfaces: `memory/`, `context_engine/`, `model-providers/`, `kanban/`, `image_gen/`, `observability/`, … |
| `skills/` + `optional-skills/` | — | Built-in (default-on) and optional (install-on-demand) skills |
| `cron/` | — | `jobs.py` (store) + `scheduler.py` (tick loop) |
| `hermes_cli/main.py` | — | **Process entry** (`main()`), `_apply_profile_override()` (runs *before* imports) |

**Import dependency chain (bottom → top):**
`tools/registry.py` ← `tools/*.py` (each self-registers at import) ← `model_tools.py`
(triggers discovery) ← `run_agent.py` / `cli.py` / `gateway/run.py` / `batch_runner.py`.

---

## 3. The Request Lifecycle (end to end)

```
            ┌─────────────── ENTRY ───────────────┐
 CLI:   hermes → HermesCLI.process_command/chat   │   Gateway: inbound platform msg
 (cli.py, main() in hermes_cli/main.py:11559)     │   → platforms/<x>.py → gateway/run.py
            └──────────────────┬───────────────────┘     _handle_message()
                               ▼
                  AIAgent (run_agent.py:320)
                  .run_conversation()  (run_agent.py:5259
                   → forwards to agent/conversation_loop.py:run_conversation)
                               ▼
   ┌──────────────────── THE LOOP (conversation_loop.py) ───────────────────┐
   │  while api_call_count < max_iterations and budget.remaining > 0:        │
   │    1. interrupt check  →  break if _interrupt_requested                 │
   │    2. build messages: sanitize, repair role-alternation, inject         │
   │       memory/context snapshot, echo-back reasoning_content              │
   │    3. system prompt = stable (cached) + ephemeral (per-turn)            │
   │    4. API call (streaming or not) via the api_mode adapter              │
   │       └─ on error: classify → backoff / rotate cred / fallback /        │
   │          compress context / shrink image (error_classifier.py)          │
   │    5. parse assistant msg + finish_reason                               │
   │    6. if tool_calls: validate names/args → _execute_tool_calls()        │
   │            (agent/tool_executor.py sequential or concurrent) → append   │
   │            tool results + drain /steer → continue                       │
   │       else: extract final response                                      │
   │    7. finalize_turn(): persist, explain abnormal exits, run hooks,      │
   │       sync memory, possibly spawn bg memory/skill review                │
   └─────────────────────────────────────────────────────────────────────────┘
                               ▼
                  final response string → surface
                  (+ session persisted to state.db, memory.sync_turn() on bg executor)
```

Messages are OpenAI chat format: `{"role": "system|user|assistant|tool", ...}`.
Reasoning content is carried in `assistant_msg["reasoning_content"]` (echoed back
for models that require it: DeepSeek, Kimi, MiMo); the internal `reasoning` field is
trajectory-only and stripped before send.

---

## 4. Core Agent Loop  (`run_agent.py` + `agent/conversation_loop.py`)

### Anchors

| What | Where |
|---|---|
| `AIAgent` class (~60-param `__init__`) | `run_agent.py:320` |
| `chat(message)` — simple, returns final string | `run_agent.py:5282` |
| `run_conversation(...)` — full, forwards to loop | `run_agent.py:5259` |
| **The loop body** — `run_conversation()` | `agent/conversation_loop.py` |
| Tool-call execution router (seq vs concurrent) | `run_agent.py:5157` (`_execute_tool_calls`) → `agent/tool_executor.py` (`execute_tool_calls_concurrent`, `execute_tool_calls_sequential`) |
| Post-loop finalization | `agent/turn_finalizer.py` (`finalize_turn`) |
| Iteration budget | `agent/iteration_budget.py` (`.consume()` / `.refund()` / `.remaining`) |
| Error taxonomy | `agent/error_classifier.py` (`FailoverReason` enum) |

### Loop facts I rely on

- **Iterations:** `max_iterations` default **90** for the parent; subagents use
  `delegation.max_iterations` (default 50). `execute_code` calls are *refunded* (don't
  consume budget). There's a one-turn `_budget_grace_call` so the model gets a final
  word when the budget runs out.
- **Interrupts:** checked at the top of every iteration — `_interrupt_requested`
  breaks the loop. The gateway sets this via `/stop` or a new inbound message.
- **System prompt = 3 tiers** (`agent/system_prompt.py:113`, `build_system_prompt_parts`):
  | Tier | Stability | Holds |
  |---|---|---|
  | **Stable** | per-session, cached | identity (`SOUL.md`), guidance blocks, skills index, environment hints, coding posture |
  | **Context** | per-session (rebuilt only after compression) | context files (`AGENTS.md`/`CLAUDE.md`/`.cursorrules`), caller `system_message` |
  | **Volatile** | per-turn, NOT cached | memory snapshot, user profile, external-memory block, timestamp |
  The volatile tier is appended as `ephemeral_system_prompt` *outside* the cached
  prefix — that's how per-turn freshness coexists with caching. **Do not move
  volatile content into the stable tier** (breaks cache) or stable content into
  volatile (wasted tokens).
- **API call:** built in `agent/chat_completion_helpers.py`; streaming via
  `interruptible_streaming_api_call()`, non-streaming via `interruptible_api_call()`
  (both run on a worker thread with a stale-call detector so a hung provider can't
  freeze me). The `api_mode` selects the adapter (§5).
- **Error/fallback recovery** (in the loop's retry block): rate-limit (429) →
  jittered backoff + credential/provider rotation; overloaded (503/529) → eager
  provider fallback; timeout → rebuild client; **context overflow → compress and
  retry**; image-too-large → shrink and retry; permanent auth → abort.
- **Agent-level tools are intercepted *before* the registry** (`agent/agent_runtime_helpers.py`,
  `AGENT_LEVEL_TOOLS`; execution in `agent/tool_executor.py`): `todo`,
  `session_search`, `memory`, `clarify`, `read_terminal`, `delegate_task`, context-engine
  tools, and memory-provider tools. Registry-dispatched tools still flow to
  `handle_function_call()`.
- **Turn finalization is a seam now** (`agent/turn_finalizer.py`): don't patch the loop
  tail in `conversation_loop.py` for persistence, abnormal-exit explanations,
  `transform_llm_output` / `post_llm_call` / `on_session_end` hooks, or background
  memory/skill review; those belong in `finalize_turn()`.

### Cheat-sheet — to fix/optimize the loop, start in:

| Symptom | Start here |
|---|---|
| Loop control / iteration logic | `agent/conversation_loop.py` `run_conversation()` |
| Budget exhausts too early/late | `agent/iteration_budget.py` + loop guard |
| Interrupt / `/stop` ignored | interrupt check at top of loop; gateway `interrupt()` |
| System prompt wrong / cache misses | `agent/system_prompt.py:113` (tier boundaries) |
| Tool calls not executing | `_execute_tool_calls` (`run_agent.py:5157`) + `agent/tool_executor.py` dispatch |
| Turn says it stopped / empty / partial | `agent/turn_finalizer.py` abnormal-exit explainer + result dict flags |
| Tool result then token budget exhausted | `agent/conversation_loop.py` token-budget guard: if the transcript ends on `role=tool`, one API-call-time finalization pass strips tool schemas, attaches a no-tool final-answer directive to the last tool message, and terminates with a visible fallback if the model returns empty text or tries to call tools again. `agent/turn_finalizer.py` has the final belt-and-braces guard: if any abnormal path still reaches finalization with `last_msg_role=tool` and no `final_response`, it synthesizes and appends a real assistant reply before gateway normalization. Do **not** append a synthetic user turn; preserve role alternation and cached-prefix stability. Regression anchors: `tests/run_agent/test_run_agent.py::TestRunConversation::test_token_budget_after_tool_result_requests_no_tool_final_answer`, `test_token_budget_final_answer_refuses_new_tool_calls`, and `tests/run_agent/test_turn_completion_explainer.py::test_finalize_turn_pending_tool_token_budget_synthesizes_reply`. |
| `memory`/`todo` not saved | `agent/agent_runtime_helpers.py` (agent-level interception) |
| Streaming cuts off / hangs | `agent/chat_completion_helpers.py` (stale detectors) |
| Reasoning content lost between turns | reasoning echo-back in the loop's message-prep step |
| Misclassified provider error | `agent/error_classifier.py` (`FailoverReason`) |
| Gateway says "Processing completed but no response was generated" | First check `gateway/run.py` `_run_agent()` empty-response early return and `_normalize_empty_agent_response()` before changing the agent loop; preserve `failed` / `partial` / `completed` / `error` evidence so the gateway can surface the real reason. |

---

## 5. Tools & Toolsets  (`tools/registry.py`, `model_tools.py`, `toolsets.py`)

### How a tool exists and becomes callable

1. **Self-registration at import.** Each `tools/*.py` calls `registry.register(...)`
   at module top level (`tools/registry.py:151` `ToolRegistry`, singleton at `:544`).
   A `ToolEntry` carries: `name`, `toolset`, `schema` (JSON Schema), `handler`,
   `check_fn` (availability probe), `requires_env`, `emoji`,
   `dynamic_schema_overrides`.
2. **Auto-discovery.** `discover_builtin_tools()` (`tools/registry.py:57`) AST-scans
   `tools/*.py` for top-level `registry.register(...)` and imports only those — **no
   manual import list**. Triggered when `model_tools.py` loads.
3. **Exposure.** A registered tool is only *sent to the model* if its name appears in
   an **enabled toolset**. `toolsets.py` `TOOLSETS` dict + `_HERMES_CORE_TOOLS`
   (default bundle most platforms inherit) decide this. So a new tool needs **two
   touches**: `tools/<name>.py` (register) **and** `toolsets.py` (wire into a toolset).

### `model_tools.py` orchestration

- `get_tool_definitions()` — resolves enabled/disabled toolsets via
  `toolsets.resolve_toolset()`, calls `registry.get_definitions()` (applies
  `check_fn`, memoized ~8-entry LRU keyed on toolset set + registry generation +
  `config.yaml` mtime), then **dynamic post-processing**: rebuilds `execute_code`'s
  schema to list available sandbox tools, applies Discord intent allowlist, strips
  cross-tool references from `browser_navigate` when web tools are absent.
- **Tool Search bridge:** when the deferrable surface (MCP + plugin tools) exceeds
  ~10% of the context window, those tools are hidden behind `tool_search` /
  `tool_describe` / `tool_call` to protect the prompt budget.
- `handle_function_call()` — coerces arg types, runs plugin `pre_tool_call` hooks
  (can block), dispatches via `registry.dispatch()` (wraps exceptions into JSON
  `{"error": ...}`), runs `post_tool_call` / `transform_tool_result` hooks. **Every
  handler returns a JSON string.**
- Process-global `_last_resolved_tool_names` (`model_tools.py`) — set by
  `get_tool_definitions()`, read by `execute_code` to know which tools the sandbox
  may call. `delegate_tool._run_single_child()` saves/restores it around subagents;
  it may be momentarily stale during a child run.
- **Terminal background work is explicit-notify by design:** bounded long jobs should
  set `background=true` + `notify_on_complete=true`; `watch_patterns` is only for rare
  one-shot signals in long-lived processes and rate-limits itself before falling back to
  completion notification. If terminal/process completion behavior looks wrong, inspect
  `tools/terminal_tool.py` watcher setup and `tools/process_tool.py`, not the agent loop.

### Tool inventory (by category)

| Category | Key file(s) | Tools |
|---|---|---|
| Terminal / process | `tools/terminal_tool.py`, `tools/process_tool.py` | `terminal`, `process`, `read_terminal` |
| Files | `tools/file_tools.py` | `read_file`, `write_file`, `patch`, `search_files` |
| Web / search | `tools/web_tools.py`, `x_search_tool.py` | `web_search`, `web_extract`, `x_search` |
| Browser | `tools/browser_tool.py`, `browser_cdp_tool.py`, `browser_dialog_tool.py` | `browser_navigate`, `_snapshot`, `_click`, `_type`, … |
| Vision / media | `tools/vision_tools.py`, `image_generation_tool.py` | `vision_analyze`, `image_generate`, `video_*` |
| Planning / memory | `tools/todo_tool.py`, `memory_tool.py`, `session_search_tool.py` | `todo`, `memory`, `session_search` |
| Delegation / code | `tools/delegate_tool.py`, `code_execution_tool.py` | `delegate_task`, `execute_code` |
| Messaging | `tools/discord_tool.py`, `feishu_*`, `yuanbao_tools.py` | `discord`, `feishu_*`, `yb_*` |
| Cron / kanban | `tools/cronjob_tools.py`, `kanban_tools.py` | `cronjob`, `kanban_*` |
| Skills | `tools/skills_tool.py`, `skills_hub.py` | `skills_list`, `skill_view`, `skill_manage` |
| Smart home | `tools/homeassistant_tool.py` | `ha_*` |
| Other | `tts_tool.py`, `clarify_tool.py`, `mixture_of_agents_tool.py`, `computer_use_tool.py` | `text_to_speech`, `clarify`, `mixture_of_agents`, `computer_use` |

**Toolset keys:** `browser`, `clarify`, `code_execution`, `cronjob`, `debugging`,
`delegation`, `discord`, `discord_admin`, `feishu_doc`, `feishu_drive`, `file`,
`homeassistant`, `image_gen`, `kanban`, `memory`, `messaging`, `moa`, `rl`, `safe`,
`search`, `session_search`, `skills`, `spotify`, `terminal`, `todo`, `tts`, `video`,
`vision`, `web`, `yuanbao` (+ per-platform `hermes-<platform>` composites).

### Cheat-sheet

| Task | Touch |
|---|---|
| Add a core tool | `tools/<name>.py` (register) **+** `toolsets.py` (wire) — see `AGENTS.md` "Adding New Tools" first |
| Add a custom/local tool (no core edit) | `~/.hermes/plugins/<name>/` via `ctx.register_tool(...)` |
| Tool not appearing for a platform | check the platform's base toolset in `toolsets.py`; check `check_fn`/`requires_env` |
| Tool errors swallowed | `registry.dispatch()` wrapping in `tools/registry.py` |
| Schema needs runtime values (limits, paths) | `dynamic_schema_overrides`; use `display_hermes_home()` for paths |

---

## 6. Providers & Models

### Provider plugin system  (`providers/__init__.py`)

- Every inference backend is a plugin under `plugins/model-providers/<name>/`; its
  `__init__.py` calls `register_provider(ProviderProfile(...))` at import
  (`providers/__init__.py:53`).
- **Lazy discovery** `_discover_providers()` (`:140`) — scanned on first
  `get_provider_profile()` (`:65`) / `list_providers()`, **separate** from the
  general `PluginManager`. Scan order: bundled `plugins/model-providers/` → user
  `$HERMES_HOME/plugins/model-providers/` → legacy `providers/<name>.py`.
  **Last-writer-wins**, so a user plugin can shadow a bundled provider by name.
- A `ProviderProfile` carries: base URL, auth scheme, default `api_mode`,
  message-prep hooks, vision capability, model-list fetcher.

### API modes → adapters

`api_mode` selects how OpenAI-format requests/responses are translated:

| `api_mode` | Adapter | Backends |
|---|---|---|
| `chat_completions` (default) | OpenAI-compatible path | OpenRouter, Ollama, most |
| `anthropic_messages` | `agent/anthropic_adapter.py` | Anthropic native + OAuth |
| `bedrock_converse` | `agent/bedrock_adapter.py` | AWS Bedrock (boto3) |
| `codex_responses` | `agent/codex_responses_adapter.py` | ChatGPT Codex backend, xAI, GitHub |
| Gemini | `agent/gemini_native_adapter.py`, `gemini_cloudcode_adapter.py`, `gemini_schema.py` | Google |

### Model resolution & context length

- Active model+provider comes from `config.yaml` `model:` section (set via
  `hermes model`), then `auth.json`, then env. Context-length resolution cascades:
  persistent `context_length_cache.yaml` → live `/models` probe → `models.dev`
  (`agent/models_dev.py`) → static `DEFAULT_CONTEXT_LENGTHS` (`agent/model_metadata.py`)
  → provider-specific overrides.

### Credentials, rotation, auxiliary, fallback

- **Credential pool** (`agent/credential_pool.py` + `credential_sources.py`): multiple
  keys/accounts per provider; rotates on rate-limit/exhaustion with cooldowns (≈5 min
  for 401, ≈1 h for 429/402). Sources: env, OAuth (device-code / loopback-PKCE),
  manual, config. `agent/nous_rate_guard.py` + `account_usage.py` guard Nous Portal
  buckets (the breaker only trips on a *confirmed-empty* bucket — don't "fix" it to
  re-probe during cooldown).
- **Auxiliary client** (`agent/auxiliary_client.py`): the cheap side-LLM for curator,
  vision, title generation, embedding, session-search. Per-task overrides live under
  `auxiliary:` in `config.yaml`; resolution order is `_resolve_auto`.
- **Fallback chain**: configured via `hermes fallback` / `fallback_providers:` in
  config; the loop advances `_fallback_index` through `_fallback_chain` on
  overloaded/exhausted errors.

### Jarvis token-maximization: subscription routing + warm ACP pool

> **⚠️ DISABLED BY DECISION (2026-06-23): `route_decision.enabled=false`.** Driving Claude
> *as the chat backend* over ACP was found to be the key cause of "Jarvis got dumber":
> `copilot_acp_client._format_messages_as_prompt` **flattens** Hermes's structured request
> (real system prompt + role-separated messages + native tool schemas) into a single TEXT
> blob delivered as a user turn inside Claude Code's own harness — so the model runs under
> CC's system prompt, sees tools as *text* it must hand-emit as `<tool_call>` JSON (→ "tool
> call could not be parsed", 0 such failures on the native gpt-5.5 path), and loses role
> structure + prompt caching. Decision: **main/cron/bg-review run the gpt-5.5 native loop**
> (real brain, fast); **Claude Code is used only as an explicit coding tool** (HERMES_HOME
> skill `code-with-claude` → headless `claude -p` with CC's *own* native tools, which is its
> strength and still spends the subscription). The included weekly quota can only be consumed
> via Claude Code/ACP, so "fill the free quota" and "answer quality" are in fundamental
> tension — resolved by spending it on real coding, not on flattened chat turns. The ACP
> machinery below remains in-tree but **dormant** (single flag re-enables it).

The "fill the paid quota" layer (spec `~/jarvis-ops/docs/jarvis-token-maximization-spec.md`,
branch `feat/jarvis-token-max`) rides on top of the provider system as **internal modules
+ config**, invisible to the model (no new core tool, no schema/system-prompt change):

- **Routing decision** (`agent/route_decision.py`): pure `decide_route(RouteFeatures)` →
  subscription provider `claude-code-acp`, simple→Sonnet / complex→Opus (`complexity >= 4`
  or high-stakes ≥3). `classify_pool()` labels billing pools
  (included_sonnet / included_opus / extra400 / codex). Fail-silent observability writes
  `logs/route_decisions.jsonl`. Consumed only when `route_decision.enabled` (else pure no-op).
- **Session pin** (`gateway/run.py` `_maybe_pin_route_decision` → `_resolve_session_agent_runtime`):
  decides once on a session's first turn, writes `_session_model_overrides`, then the
  override fast-path reuses provider/model/command/args every turn — cache-safe, never
  re-decided mid-session (no-touch #1).
- **Subscription transport** (`agent/copilot_acp_client.py`): `ExternalACPClient`
  (generalized from the Copilot client) drives the official Claude ACP adapter over
  ACP JSON-RPC → consumes the subscription's included weekly quota (OAuth, **never** API key).
  `_AcpConnection` = one subprocess + reader threads + JSON-RPC inbox; `_AcpPool` =
  module-level warm-process pool (key=`(command,args,cwd)`, lazy reap on acquire/release +
  atexit), gated by `route_decision.acp_persistent_process` (default off = spawn-per-call).
- **Adapter generations** — the client speaks both ACP wire shapes (back-compat):
  `@zed-industries/claude-code-acp@0.16.2` (bin `claude-code-acp`, model via
  `result.models.availableModels` + `session/set_model`, **no usage**) and
  `@agentclientprotocol/claude-agent-acp>=0.48` (bin `claude-agent-acp`, model via
  `result.configOptions[id=model]` + `session/set_config_option`, **reports
  `PromptResponse.usage`**). `auth.py` prefers the new bin, falls back to the old;
  `_select_session_model` picks the wire method per session/new shape; Copilot advertises
  neither → no-op. Roll back to the old adapter with `HERMES_CLAUDE_CODE_ACP_COMMAND=claude-code-acp`.
- **Real usage (item 4)** — `_acp_usage_to_namespace` maps the new adapter's
  `PromptResponse.usage` (`inputTokens/outputTokens/cachedReadTokens/cachedWriteTokens`,
  Anthropic semantics) into the OpenAI chat_completions shape `normalize_usage()` reads, so
  `cachedReadTokens` becomes the real cache-hit ruler in `route_decisions.jsonl` (absent →
  zeros → `observe_usage` falls back to the `context_tokens` estimate). NOTE: the new SDK
  (0.3.x) bundles a per-arch `claude` that hangs under an x64-Rosetta node → `_build_subprocess_env`
  pins `CLAUDE_CODE_EXECUTABLE` to the native system `claude` (override `HERMES_CLAUDE_CODE_EXECUTABLE`).
- **Persistent session (T2b)** — `route_decision.acp_persistent_session` (default off): module-level
  `_T2B` registry keyed by `(command,args,cwd, sha1(first system+first user))` holds a dedicated
  connection + ACP sessionId + `sent_hashes`. `_run_persistent` sends only the new *non-assistant*
  turns (`_format_delta_as_prompt`); Claude Code then caches the conversation turn-by-turn (live:
  **99% cache read on a 1.15M context**, vs ~8% for the fresh full-resend the loop does otherwise —
  fresh keeps history in one growing user message so the cache breakpoint moves each turn). Diverges
  safely to a fresh `session/new` + full history on prefix change (compression/edit), dead session, or
  unkeyable conversation — never cross-talk. `_run_on_connection` was split into reusable
  `_drain`/`_ensure_initialized`/`_open_new_session`/`_send_prompt`. ⚠️ MUTUALLY EXCLUSIVE with
  `acp_disable_builtin_tools` (which passes `_meta.disableBuiltInTools`): dropping the engine's tools
  removes the tools+system block prompt caching anchors on → T2b cache 100%→0%. Enable T2b alone.
- **Latency reality** — ACP turn latency is **output-bound** (~50 tok/s; `latency ≈ 13s prompt + out/50`),
  not thinking/context/cache. T2b makes the prompt side ~free but cannot speed up generation; the
  user-facing floor is output length (e.g. `out=12699 → 266s`).
- **ACP detection guards** — three sibling predicates that MUST all match the dispatch in
  `agent_runtime_helpers.create_openai_client` (`provider in (copilot-acp, claude-code-acp)`
  or `base_url` starts `acp://`): command-population + Responses-upgrade exclusion
  (`agent_init.py`), streaming-disable (`conversation_loop.py`). A half-broadened guard
  ships the wrong spawn command (`copilot` instead of `claude-code-acp`) — see commit
  `4bb5c706a`.
  - **Forks must inherit the spawn command (2026-06-23).** The background-review fork
    (`agent/background_review.py`) builds its own `AIAgent` from `_current_main_runtime()`.
    That dict previously carried provider/base_url but NOT `command`/`args`, so a
    `claude-code-acp` review fork fell back to the `copilot` default and died with
    "Could not start Copilot ACP command 'copilot'" (24×/day, thread=bg-review).
    Fix: `_current_main_runtime()` (`run_agent.py`) now exposes `command`/`args` (from
    `self.acp_command`/`acp_args`), and the review fork threads them via
    `acp_command=`/`acp_args=`. Any new fork/auxiliary that rebuilds an ACP agent from
    the parent runtime MUST thread these too.

### Cheat-sheet

| Symptom | Start here |
|---|---|
| Provider not found / wrong profile | `providers/__init__.py` (`get_provider_profile`, scan order) |
| Wrong request shape for a backend | the matching `agent/*_adapter.py` |
| Auth fails / keys not rotating | `agent/credential_pool.py` + `credential_sources.py` |
| Context length wrong → premature compression | `agent/model_metadata.py` / `models_dev.py` |
| Curator/vision using wrong model | `agent/auxiliary_client.py` `_resolve_auto` + `auxiliary:` config |
| Fallback not kicking in | `_fallback_chain` activation in the loop |
| Subscription routing wrong / not pinned | `agent/route_decision.py` (`decide_route`) + `gateway/run.py` (`_maybe_pin_route_decision`) |
| claude-code-acp spawns wrong CLI / streaming breaks | the 3 ACP guards (`agent_init.py` command-pop & Responses-upgrade, `conversation_loop.py` streaming) — must match the `create_openai_client` dispatch |
| ACP per-call cold-start latency / warm pool | `agent/copilot_acp_client.py` `_AcpPool` / `_AcpConnection`; flag `route_decision.acp_persistent_process` |

### Kanban review gate routing

- **Decomposition route** (`hermes_cli/kanban_decompose.py`): after the auxiliary LLM
  proposes a child graph, deterministic review-gate logic may append a final
  `jarvisreview` child. The original implementation/research children keep their
  assignees; the review child depends on graph leaves, so it runs after execution and
  before the root/orchestrator wakes for final delivery.
- **Triggers:** any high-risk/verification keyword in the root or child text (`deploy`,
  `gateway`, `cron`, `permission`, `security`, `e2e`, `test`, `verify`, Chinese
  equivalents, etc.) OR a cheap complexity score >= `kanban.review_gate.complexity_min`
  (default `4`). Single-task decompositions route directly to `jarvisreview` when the
  task itself is verification/review work.
- **Config:** `kanban.review_gate.enabled` (default true),
  `kanban.review_gate.assignee` (default `jarvisreview`), and
  `kanban.review_gate.complexity_min` (default `4`). If the assignee profile is absent,
  no review child is added. This is behavioral config, not a secret; keep it in
  `config.yaml`, never `.env`.

---

## 7. State, Persistence & the `HERMES_HOME` Layout

### `SessionDB` (`hermes_state.py`)

- `class SessionDB` at `hermes_state.py:657`; `SCHEMA_VERSION = 16`. SQLite at
  **`$HERMES_HOME/state.db`** in **WAL mode** (retry-with-jitter on lock contention;
  checkpoint every ~50 writes; auto-repair of malformed schema with backup→rebuild).
- Tables: `sessions` (metadata), `messages` (full history), `state_meta`,
  `compression_locks`, plus **FTS5** full-text indexes (standard + trigram tokenizer
  for CJK / substring search) backing `session_search`.

### Profile-aware paths — the rule that prevents a whole bug class

- **`get_hermes_home()`** (`hermes_constants.py`) is the *only* correct way to get the
  home dir in code. **`display_hermes_home()`** for user-facing strings. **Never**
  hardcode `~/.hermes` or `Path.home()/".hermes"` — that breaks profiles (each
  profile is its own isolated `HERMES_HOME`). `_apply_profile_override()`
  (`hermes_cli/main.py:337`, runs at `:509` *before* argparse and most imports) sets
  `HERMES_HOME`; module-level constants that cache `get_hermes_home()` at import are
  therefore safe. Profile *enumeration* is `HOME`-anchored on purpose
  (`_get_profiles_root()`), not `HERMES_HOME`-anchored.

### Config (`hermes_cli/config.py`)

- `DEFAULT_CONFIG` is the schema source; `load_config()` deep-merges user
  `config.yaml` over it (mtime-cached). Adding a *new key* to an existing section
  needs no `_config_version` bump — only structural migrations do.
- **Three config loaders** — know which one you're in: `load_cli_config()` (CLI),
  `load_config()` (`hermes tools`/`setup`/subcommands), direct YAML (gateway runtime).
  If CLI sees a key but the gateway doesn't, you're on the wrong loader.
- **`.env` is for SECRETS ONLY** (`OPTIONAL_ENV_VARS`). All behavioral settings go in
  `config.yaml`. Top-level config sections: `model`, `providers`, `fallback_providers`,
  `toolsets`, `agent`, `terminal`, `web`, `browser`, `checkpoints`, `compression`,
  `kanban`, `prompt_caching`, `auxiliary`, `display`, `dashboard`, `tts`, `stt`,
  `voice`, `context`, `memory`, `delegation`, `skills`, `curator`, `cron`, `gateway`,
  `logging`, `profiles`, `plugins`, `honcho`, `security`, …

### `HERMES_HOME` runtime layout (`~/.hermes/`)

| Path | What it is | Owned by |
|---|---|---|
| `state.db` (+ `-wal`/`-shm`) | session + message store, FTS5 | `hermes_state.py` |
| `config.yaml` | all behavioral settings | `hermes_cli/config.py` |
| `.env` | **secrets only** (API keys/tokens) | `hermes_cli/config.py` (`OPTIONAL_ENV_VARS`) |
| `auth.json` | provider OAuth/credential state | `agent/credential_persistence.py` |
| `SOUL.md` | persona/tone, loaded fresh each message | `agent/system_prompt.py` |
| `memories/MEMORY.md`, `USER.md` | frozen memory snapshots (system-prompt injected) | `agent/memory_manager.py` |
| `skills/` (+ `.archive/`, `.usage.json`) | agent + installed skills, telemetry | `tools/skills_tool.py`, `skill_usage.py`, curator |
| `sessions/` | per-session artifacts | session layer |
| `cron/` (`jobs.json`, `.tick.lock`) | scheduled jobs + tick lock | `cron/jobs.py` |
| `kanban.db` | multi-agent board | `hermes_cli/kanban.py` |
| `plugins/` | user plugins (general / memory / model-provider) | `hermes_cli/plugins.py`, `providers/` |
| `logs/` | `agent.log` (INFO+), `errors.log` (WARN+), `gateway.log` | `hermes_logging.py` |
| `checkpoints/store/` | content-addressed git checkpoint store | `tools/checkpoint_manager.py` |
| `profiles/<name>/` | fully isolated alternate `HERMES_HOME`s | profiles system |
| `gateway.pid`/`.lock`, `gateway_state.json`, `processes.json` | gateway runtime locks/state | `gateway/status.py` |
| `config.yaml.bak.*` | timestamped config backups | config save path |

### Cheat-sheet

| Symptom | Start here |
|---|---|
| Sessions not saving / corrupt DB | `hermes_state.py` (`SessionDB`, WAL retry, repair) |
| `session_search` returns nothing | FTS5 setup in `hermes_state.py` (trigram for CJK) |
| Config key ignored | which loader? `hermes_cli/config.py` `DEFAULT_CONFIG` coverage |
| Path wrong under a profile | hardcoded `~/.hermes` — replace with `get_hermes_home()` |
| Credentials lost on restart | `agent/credential_persistence.py` + `auth.json` |

---

## 8. Messaging Gateway (`gateway/`)

### Process model & message flow

- `hermes gateway` starts a long-running daemon (`gateway/run.py`, the `GatewayRunner`).
  Each platform adapter (`gateway/platforms/<x>.py`) connects, receives messages, and
  hands them to the runner's `_handle_message()`.
- Flow: **inbound → adapter normalizes → `_handle_message()` (authorize, route
  commands) → build/resume `SessionContext` (`gateway/session.py`) → run `AIAgent` →
  stream/send reply back via the adapter.**

### The two message guards (both must let control commands through)

1. **Adapter guard** (`gateway/platforms/base.py`): when a session is active, inbound
   messages are queued in `_pending_messages` instead of interrupting the running
   agent.
2. **Runner guard** (`gateway/run.py`): intercepts control commands — `/stop`, `/new`,
   `/queue`, `/status`, `/approve`, `/deny` — *before* they reach
   `running_agent.interrupt()`.

   **Rule:** any new command that must reach the runner *while the agent is blocked*
   (e.g. an approval response) MUST bypass **both** guards and dispatch inline — never
   via `_process_message_background()` (which races session lifecycle).

### Platform adapters

- A platform implements the base ABC (`gateway/platforms/base.py`): `connect()`,
  `disconnect()`, `send()`, message normalization. ~20 platforms: telegram, discord,
  slack, whatsapp, signal, matrix, mattermost, email, sms, dingtalk, wecom, weixin,
  feishu, qqbot, bluebubbles, yuanbao, webhook, api_server, homeassistant, …
- **Profile/token safety:** adapters connecting with a unique credential call
  `acquire_scoped_lock()` (`gateway/status.py`) in `connect()`/`start()` and
  `release_scoped_lock()` in `disconnect()` — prevents two profiles using the same
  bot token. Canonical pattern: `gateway/platforms/telegram.py`.
- **Feishu DM task isolation (2026-06-23, gated `feishu.dm_task_mode`, default OFF).**
  Goal "每任务一卡一会话，闲聊秒回": a top-level actionable DM message is spun off
  into its own thread + session and runs **concurrently** with siblings (race-free,
  because each thread is its own session); chit-chat falls through to normal inline
  handling. Pieces: `gateway/dm_task_router.py` (pure heuristic `classify_dm_message`
  → task|chitchat|ambiguous); `feishu.py::_handle_message_with_guards` intercepts top-
  level DM tasks → `_run_dm_task` (open thread via `send(reply_to=msg_id,
  metadata={thread_id})`, then re-enter `handle_message` on a thread-scoped
  `dataclasses.replace` clone with a **fresh message_id** so the dedup cache doesn't
  drop it); the per-chat lock keys on `(chat_id, thread_id)` so sibling threads run in
  parallel; `thread_sessions_per_user` defaults to `dm_task_mode`. Kanban card creation
  is a further sub-flag `dm_task_create_card` (default OFF — avoids the dispatcher
  double-running the card the gateway already executes). Flag off ⇒ strict no-op.
- **Background-process notifications:** `terminal(background=true,
  notify_on_complete=true)` arms a watcher that, on completion, triggers a new agent
  turn. Verbosity: `display.background_process_notifications` (all/result/error/off).
- **Slash commands** derive from the central registry in `hermes_cli/commands.py`
  (`COMMAND_REGISTRY` of `CommandDef`); the gateway uses `GATEWAY_KNOWN_COMMANDS` +
  `resolve_command()`. Adding/aliasing a command in that registry updates CLI,
  gateway help, Telegram menu, Slack mapping and autocomplete automatically.

### Cheat-sheet

| Symptom | Start here |
|---|---|
| Message ignored / not authorized | `gateway/run.py` `_handle_message()` |
| `/stop` or `/approve` not landing | the two guards (`platforms/base.py` + `run.py`) — bypass both |
| Wrong session / context bleed | `gateway/session.py` (session key construction) |
| Two profiles fighting over a token | `gateway/status.py` scoped locks |
| New platform | implement `gateway/platforms/base.py` ABC; see `ADDING_A_PLATFORM.md` |

---

## 9. The Learning Loop (what makes me self-improving)

### A. Context compression  (`agent/context_compressor.py`, `context_engine.py`, `conversation_compression.py`, `trajectory_compressor.py`)

- `should_compress()` checks prompt-token pressure against a threshold (default ~75%
  of the window). `compress()` summarizes middle turns via the **auxiliary** model,
  preserving the system prompt and recent turns, and **rotates the session id**.
- Cache-safe because earlier messages are never mutated in place — compression is the
  *one* sanctioned context rewrite. After it, providers get
  `on_session_switch(parent_session_id=...)`. Tool-call/result pairs are kept balanced
  (`_sanitize_tool_pairs`) so the API never sees an orphaned `tool_call`.

### B. Memory  (`agent/memory_manager.py`, `memory_provider.py`, `tools/memory_tool.py`, `plugins/memory/*`)

- Two frozen text stores — **`memories/MEMORY.md`** + **`USER.md`** — are injected as a
  **snapshot at session start** (volatile tier). The `memory` tool writes durably
  mid-session but **does not** mutate the cached prefix.
- `MemoryProvider` ABC lifecycle: `initialize()`, `prefetch(query)` (current-turn recall),
  `queue_prefetch(query)` (background recall), `sync_turn(turn_messages)` (after each
  turn, on a bg executor), `shutdown()`, optional `post_setup()` /
  `on_session_switch()` / `on_pre_compress()` / `on_memory_write()`. Orchestrated by
  `MemoryManager`. Built-in providers: honcho, mem0, supermemory, byterover,
  hindsight, holographic, openviking, retaindb. **New providers ship as standalone
  plugin repos** — the in-tree set is closed.
- **Honcho latency invariant:** `plugins/memory/honcho.HonchoMemoryProvider.prefetch()`
  is on the pre-LLM critical path and must only consume already-cached base context /
  dialectic results. It must not start or `join()` Honcho `peer.chat()` / dialectic
  work. Agentic Dialectic synthesis runs only in session prewarm or `queue_prefetch()`
  daemon threads, and a running dialectic surfaces on a later turn instead of delaying
  the current response.

### C. Skills  (`tools/skills_tool.py`, `skills_hub.py`, `agent/skill_commands.py`, `tools/skill_usage.py`)

- Skills live in `skills/` (built-in, default-on) and `optional-skills/` (install via
  `hermes skills install official/<cat>/<skill>`). Each is a dir with `SKILL.md`
  (YAML frontmatter + body) + optional `scripts/`/`references/`/`templates/`.
- **Progressive disclosure:** `skills_list` shows name/description only; `skill_view`
  loads the full body or linked files. No `offset/limit` pagination on instructional
  content — the model would skip the rest.
- **Slash invocation is injected as a USER message** (`agent/skill_commands.py`), *not*
  into the system prompt — this preserves the cache. The invocation prefix is
  byte-identical across builders so extractors can recover the user's real instruction
  for memory providers.
- **Autonomous skill creation:** I write skills from experience; usage is tracked in
  `skills/.usage.json` (`tools/skill_usage.py`).

### D. Curator  (`agent/curator.py`, `curator_backup.py`, `hermes_cli/curator.py`)

- Inactivity-triggered background maintenance (no daemon): runs when idle and
  `interval_hours` have passed. Auto-transitions agent-created skills active → stale
  (~30 d unused) → archived (~90 d). **Invariants:** only touches
  `created_by: agent` skills; **never deletes** (max action = archive, restorable from
  `skills/.archive/`); **pinned** skills are exempt from every transition and the LLM
  review. Pre-mutation tar.gz snapshots via `curator_backup.py`. CLI:
  `hermes curator {status,run,pause,resume,pin,unpin,archive,restore,prune,backup,rollback}`.

### E. Delegation  (`tools/delegate_tool.py`, `async_delegation.py`)

- Spawns isolated child `AIAgent`s with fresh session + own terminal. **Single** mode
  blocks the parent until the child summary returns; **batch** runs children
  concurrently (cap `delegation.max_concurrent_children`, default 3). Roles: `leaf`
  (default — cannot delegate/clarify/memory/send_message/execute_code) vs
  `orchestrator` (can spawn workers, depth ≤ `max_spawn_depth`, default 2). The parent
  sees **only the summary**, never the child's intermediate messages.
- **Not durable:** if the parent turn is interrupted, the child is cancelled. For work
  that must outlive the turn, use `cronjob` or `terminal(background=True,
  notify_on_complete=True)`.
- **External-ACP children inherit memory + profile.** Children are built with
  `skip_memory=True`, so a native subagent runs memory-free by design. But a child that
  runs over an **external ACP transport** (`effective_acp_command` set — e.g. Claude Code
  via `claude-code-acp`) builds no volatile block of its own, so
  `_maybe_inject_parent_memory` snapshots the parent's memory + USER profile +
  external-memory block (via `system_prompt.build_memory_profile_block`, timestamp
  excluded) into the child's `ephemeral_system_prompt` at spawn time. Delegated Claude
  Code agents thus carry the same memory/profile context as the main session; native
  (non-ACP) children are unaffected.

### F. Cron & Kanban  (`cron/scheduler.py`, `cron/jobs.py`; `hermes_cli/kanban.py`, `tools/kanban_tools.py`)

- **Cron:** `scheduler.tick()` (every ~60 s, runs inside the gateway) fires due jobs.
  Hardening: **3-minute hard interrupt** per cron session, catchup/grace windows, a
  `cron/.tick.lock` file lock against duplicate ticks, and `skip_memory=True` by
  default. Cron output lands in its own session (framed header/footer) so the main
  conversation's role alternation stays intact.
  - **Cron routing (token-maximization, 2026-06-23):** cron jobs do NOT go through
    the gateway's `_maybe_pin_route_decision`, so by default they ran on the base
    `config.model.default` (gpt-5.5/Codex) and failed on Codex `usage_limit`. The
    scheduler now repoints them through the Claude subscription: when
    `route_decision.enabled` + `route_decision.cron_via_acp` (default true) and the
    job pins no provider/model, it resolves `claude-code-acp` creds and runs on
    `route_decision.cron_model` (default `claude-sonnet-4-6`). Fail-silent — any
    error keeps the originally-resolved runtime (no regression). See the block right
    after `resolve_runtime_provider()` in `scheduler.py`.
- **Kanban:** durable SQLite board (`hermes_cli/kanban_db.py`) for multi-profile,
  multi-board collaboration. The default board remains `<root>/kanban.db` for
  back-compat; additional boards live under `<root>/kanban/boards/<slug>/` with
  isolated DB/workspaces/logs. Board resolution is explicit `--board` / function arg →
  `HERMES_KANBAN_BOARD` → legacy `HERMES_KANBAN_DB` → `<root>/kanban/current` →
  `default`. The dispatcher injects `HERMES_KANBAN_DB`,
  `HERMES_KANBAN_WORKSPACES_ROOT`, and `HERMES_KANBAN_BOARD` into workers so worker
  tools cannot accidentally see the wrong board.
- **Kanban dispatcher hardening:** the dispatcher loop (default in-gateway) reclaims
  stale claims, promotes ready tasks, atomically claims, and spawns assigned profiles.
  SQLite WAL + `BEGIN IMMEDIATE` + CAS on `tasks.status`/`claim_lock` make claiming
  per-board atomic. `last_heartbeat_at` detects wedged-but-still-alive workers;
  host-local still-alive workers get a short reclaim deferral to avoid duplicate
  spawns. Non-success outcomes now funnel through `_record_task_failure()` so
  spawn-failed, crashed, and timed-out runs share `consecutive_failures`; per-task
  `max_retries` overrides `kanban.failure_limit` / `DEFAULT_FAILURE_LIMIT`. Worker exit
  code `75` (`KANBAN_RATE_LIMIT_EXIT_CODE`) is classified as `rate_limited`, requeued
  without incrementing the failure counter.
  Site-local watchdogs can live under `$HERMES_HOME/scripts/` rather than this repo:
  e.g. `jarvis_kanban_completion_idle_retry.py` is a cron `no_agent` notifier/retry
  script. For bad completion notification titles, inspect both `tasks.title/body` and
  that script's `notification_title()` command-string summarizer before changing core
  cron or gateway code.
  The local Jarvis dashboard lives at `$HERMES_HOME/scripts/jarvis_kanban_dashboard.py`
  (symlinked to `~/Jarvis/jarvis-ops/dashboards/`). For "no data" / date-filter bugs,
  verify four layers before declaring success: `/healthz`, `/api/state`,
  `/api/state?date=YYYY-MM-DD`, and a browser-visible page. Use
  `/?date=YYYY-MM-DD&noevents=1` for automated page checks so the SSE `/events`
  stream does not keep browser tools waiting forever; normal pages still use live SSE.

### Cheat-sheet

| Symptom | Start here |
|---|---|
| Compression too aggressive / context lost | `agent/context_compressor.py` (`should_compress`, threshold) |
| Memory not persisting / not recalled | `agent/memory_manager.py` (`sync_all`, `prefetch_all`) |
| Skill missing from list / palette | `tools/skills_tool.py` discovery + `agent/skill_commands.py` |
| Curator archived something it shouldn't | `agent/curator.py` transitions + `is_agent_created`/pinned guards |
| Subagent won't spawn / wrong toolset | `tools/delegate_tool.py` (`_build_child_agent`) |
| Cron job missed / runs twice | `cron/scheduler.py` `tick()` + `cron/.tick.lock` |
| Kanban task stuck / re-spawning | `hermes_cli/kanban_db.py` reclaim/heartbeat + `_record_task_failure()` + dispatcher `failure_limit`/`max_retries` |
| Kanban worker rate-limited but task blocked | exit classifier around `KANBAN_RATE_LIMIT_EXIT_CODE` and `detect_crashed_workers._last_rate_limited` |

---

## 10. Incident Taxonomy & Self-Repair Strategy

This section is grounded in cross-session evidence from `state.db` and prior incident
write-ups, not a theoretical taxonomy. When searching history on 2026-06-24, the
same families appeared across multiple sessions: `Processing stopped after using
tools but produced no reply` (8 recent sessions), `Processing completed but no
response was generated` (8), `provider timeout` (8), exhausted fallback chains (4),
`stream_read_error` (3), `JSONDecodeError` (8), old custom-provider 429 daily-limit
failures (7), HTTP 502 upstream failures (2), pending-tool turn endings (8), and
`token_budget_exhausted` (8). Treat these as recurring classes until the monitoring
history proves otherwise.

### A. Tool-after-empty / abnormal turn endings

- **Observed signatures:** gateway warnings like `Processing stopped after using tools
  but produced no reply`, generic `Processing completed but no response was generated`,
  logs with `last_msg_role=tool response_len=0`, `Turn ended with pending tool result`,
  or `token_budget_exhausted:*` after tool execution.
- **Primary repair path:** start in `agent/conversation_loop.py` for the token-budget
  no-tool final-answer pass, then `agent/turn_finalizer.py` for belt-and-braces
  synthetic assistant replies, then `gateway/run.py` `_run_agent()` and
  `_normalize_empty_agent_response()` for user-visible normalization.
- **Invariant:** never append a synthetic user message to escape the `tool` tail;
  preserve role alternation and prompt-cache stability. If finalization needs a nudge,
  make it API-call-scoped or append a real assistant fallback in finalization.
- **Verification:** add/extend regression anchors under `tests/run_agent/` and
  `tests/gateway/` that assert both the result dict evidence (`failed`, `partial`,
  `completed`, `turn_exit_reason`, `last_msg_role`) and the exact gateway text.

### B. Provider / transport instability

- **Observed signatures:** `provider timeout`, `Fallback chain was exhausted or
  unavailable`, old custom-provider `DAILY_LIMIT_EXCEEDED`, HTTP 502 upstream errors,
  connection errors, and clusters tied to `sessions.billing_base_url`.
- **Primary repair path:** inspect `agent/error_classifier.py`, provider adapters under
  `agent/*_adapter.py`, `agent/credential_pool.py`, and fallback activation in
  `agent/conversation_loop.py`. Use `sessions.billing_provider` and
  `sessions.billing_base_url`, not current config alone, to attribute historical
  incidents.
- **Strategy:** distinguish three layers before changing code: provider returned an
  error, provider returned no final assistant text, or Hermes normalized a valid final
  text incorrectly. For custom OpenAI-compatible services, request service-side
  request ids / stream close reason / upstream `finish_reason`; Hermes local logs are
  API summaries, not raw HTTP-body proof.
- **Verification:** reproduce against a temp `HERMES_HOME` or a controlled fake provider
  that emits timeout, empty content, tool calls without final text, and retryable vs
  permanent errors.

### C. Cron and document-update failures

- **Observed signatures:** cron sessions with `provider timeout`, fallback exhaustion,
  `stream_read_error`, and Lark/doc CLI parse issues surfaced after the job had already
  mutated or partially updated a document.
- **Primary repair path:** start in `cron/scheduler.py` for runtime/fallback/interrupt
  behavior and `cron/jobs.py` for persisted job state; for Lark/Feishu document jobs,
  verify with `docs +fetch` after any `docs +update` / media insert before claiming the
  cron succeeded.
- **Strategy:** scheduled jobs need watchdog semantics: script/no-agent jobs should stay
  silent on empty stdout, alert on non-zero exit, and include enough artifact paths for
  triage. Never blindly re-run a document cron after an ambiguous failure; fetch the
  target document, identify the exact missing/partial block, and repair only that block.
- **Verification:** check `last_run_at`, `last_status`, newest `cron/output/<job_id>/`,
  the cron session in `state.db`, and the target artifact/document itself.

### D. Tool-output parsing and verification traps

- **Observed signatures:** `JSONDecodeError` after commands whose stdout is progress
  text rather than JSON, especially Lark media/document operations; retries can duplicate
  side effects.
- **Primary repair path:** inspect the tool or CLI contract first. Do not assume every
  successful command emits JSON. For Lark CLI, `docs +media-insert` success evidence is
  progress lines such as `Block created:` / `File uploaded:` plus fetch verification.
- **Strategy:** parse only documented JSON outputs; otherwise capture stdout as text,
  extract stable success markers, then verify via a readback command. If a pipe-to-Python
  or parser fails, treat that as a verification-method failure until a safer fetch proves
  the write failed.
- **Verification:** read back the target section/count/marker and compare rendered Docx
  semantics, not source Markdown or CLI `ok:true` alone.

### E. State, path, and surface attribution errors

- **Observed signatures:** old `~/.hermes` path assumptions after the migration to
  `~/Jarvis/.hermes`, confusion between current config and historical session runtime,
  Dashboard/API/UI layer mismatches, and user follow-ups like `？` after a turn ends
  without delivery.
- **Primary repair path:** use `get_hermes_home()` in code and `$HERMES_HOME` in scripts;
  for historical evidence use `state.db` session metadata and message chronology; for
  Dashboard or external surfaces verify layer-by-layer: config → API/data → rendered UI
  → end-to-end user-visible behavior.
- **Strategy:** when a task combines a business question and a Hermes repair, report the
  two scopes separately: business artifact inspected, Hermes/Jarvis files changed, and
  which repo was not modified. This prevents ambiguity like “why did you change Hermes
  while analyzing a business project?”
- **Verification:** every final report must name the verified layer and include real
  command/test/fetch output. If the user had to send `？`, treat that as a delivery
  incident and inspect whether the previous turn ended on a tool call, token budget,
  provider timeout, or gateway normalization.

### Incident response loop

1. **Classify by evidence, not wording.** User-visible text is a symptom; collect
   `state.db` session metadata/messages and `logs/agent.log` / `errors.log` first.
2. **Bind to the actual runtime.** Use `sessions.model`, `billing_provider`,
   `billing_base_url`, source (`feishu`, `cron`, etc.), and last message role.
3. **Choose the owning layer.** Agent loop/finalizer, gateway normalization, provider
   transport, cron scheduler, CLI/tool contract, or surface/UI delivery.
4. **Repair the smallest durable seam.** Prefer finalizer/gateway/tool-contract fixes
   and site-local watchdog scripts over widening core schema or mutating prompt context.
5. **Add regression or watchdog coverage.** Unit tests for code seams; no-agent cron
   watchdogs for production recurrence; incident exports under
   `$HERMES_HOME/docs/incident-logs/` for evidence.
6. **Update this map and skills.** If the fix changes where future agents should look,
   update `SELF_ARCHITECTURE.md`; if it creates a reusable workflow, create or patch a
   skill.

---

## 11. Diagnostic Surfaces (run these before guessing)

| Need | Command / file |
|---|---|
| Health of config + deps | `hermes doctor` |
| Component status (gateway, providers, locks) | `hermes status` |
| Browse my own logs | `hermes logs [--follow] [--level …] [--session …]` → `~/.hermes/logs/` |
| Setup summary for debugging | `hermes dump` |
| Upload logs/system info for support | `hermes debug` |
| Usage / cost insights | `hermes insights [--days N]` or `/usage`, `/insights` |
| Inspect/prune checkpoints | `hermes checkpoints` → `~/.hermes/checkpoints/` |
| Supply-chain audit (OSV) | `hermes security` |
| What's wired to Nous Portal | `hermes portal info` |
| Live config edit | `hermes config` |

---

## 12. Self-Maintenance Playbook (cross-cutting "I need to…")

| I need to… | Go to |
|---|---|
| Fix a bug in how I run the conversation | §4 — `agent/conversation_loop.py` |
| Add/repair a tool | §5 — `tools/<name>.py` + `toolsets.py` (or a plugin) |
| Make a tool appear only when configured | §5 — `check_fn` / `requires_env` |
| Support a new model backend | §6 — `plugins/model-providers/<name>/` + (maybe) an adapter |
| Fix auth / rate-limit / rotation | §6 — `agent/credential_pool.py`, `nous_rate_guard.py` |
| Fix session save/search/corruption | §7 — `hermes_state.py` |
| Add a config option | §7 — `DEFAULT_CONFIG` (config.yaml, **not** `.env`) |
| Fix a path bug under profiles | §7 — replace hardcoded `~/.hermes` with `get_hermes_home()` |
| Fix a messaging-platform bug | §8 — `gateway/run.py`, `gateway/platforms/<x>.py` |
| Add a slash command | §8 — `hermes_cli/commands.py` `COMMAND_REGISTRY` (+ handlers) |
| Tune context compression / memory / skills / curator | §9 |
| Spawn/parallelize work | §9E — `delegate_task`; durable → `cronjob` |
| Add a built-in skill | `skills/<cat>/<name>/SKILL.md` — see `AGENTS.md` HARDLINE standards |
| Add a plugin (no core edit) | `~/.hermes/plugins/<name>/` — plugins MUST NOT touch core files |

---

## 13. Hard Invariants & Gotchas (don't relearn these the hard way)

1. **Don't break prompt caching.** No mutating past context, swapping toolsets, or
   rebuilding the system prompt mid-conversation. Cache-aware slash commands default to
   *deferred* invalidation with an opt-in `--now` (pattern: `/skills install --now`).
2. **Preserve role alternation.** Never two same-role messages in a row; never a
   synthetic user message injected mid-loop. Keep every `tool_call` paired with a
   `tool` result.
3. **`config.yaml` for behavior, `.env` for secrets.** Reject any "set `HERMES_*` in
   your `.env`" for non-credentials.
4. **Profile-safe paths only** — `get_hermes_home()` / `display_hermes_home()`, never
   hardcoded `~/.hermes` (source of a 5-bug cluster). This local install currently uses
   `~/Jarvis/.hermes`; old `~/.hermes` references are stale unless explicitly talking
   about default upstream docs.
5. **Plugins must not modify core files** (`run_agent.py`, `cli.py`,
   `gateway/run.py`, `hermes_cli/main.py`). If a plugin needs more, widen the generic
   plugin surface.
6. **Verify the premise before "fixing".** A limitation that looks like a gap is
   often deliberate (profiles are isolated *on purpose*; the rate breaker only trips on
   a confirmed-empty bucket). Point to the exact line where the bug manifests AND show
   the fix changes that line. `git log -p -S "<symbol>"` to read original intent.
7. **No change-detector tests** — assert invariants (relationships), not snapshots
   (model lists, version literals, counts).
8. **No `\033[K`** in spinner/display code (leaks as `?[K` under `prompt_toolkit`);
   space-pad instead. No new `simple_term_menu` (use `hermes_cli/curses_ui.py`).
9. **Tool schema descriptions must not name tools from other toolsets** (they may be
   disabled → hallucinated calls). Add cross-refs dynamically in
   `get_tool_definitions()`.
10. **`delegate_task` is not durable** — interrupted parent cancels the child.
11. **Don't debug abnormal turn endings in the wrong layer.** Empty/partial/stopped
   user-visible output is usually `agent/turn_finalizer.py` + gateway normalization;
   tool completion/notification issues are usually `agent/tool_executor.py` or
   `tools/terminal_tool.py`/`tools/process_tool.py`, not the core API loop.

---

## 14. Verifying a change

```bash
# ALWAYS use the wrapper — it enforces CI-parity (hermetic env, UTC, unset keys,
# xdist workers, per-test subprocess isolation). Never call pytest directly.
scripts/run_tests.sh                                   # full suite
scripts/run_tests.sh tests/agent/test_foo.py::test_x   # one test
scripts/run_tests.sh --no-isolate tests/foo/           # faster, for debugging
```

Tests must not write to `~/.hermes/` — the `_isolate_hermes_home` autouse fixture
(`tests/conftest.py`) redirects `HERMES_HOME` to a temp dir. For
resolution-chain/config/security/remote/IO changes, do **E2E validation against a temp
`HERMES_HOME`**, not just green unit mocks.

---

## 15. Keeping this map honest

- **Line numbers are a 2026-06-21 snapshot and will drift.** Navigate by the named
  symbols; re-grep when a pointer misses (`grep -n "def <name>\|class <name>"`).
- This is a *map*, not the territory — the filesystem is canonical. When a subsystem is
  refactored (especially the god-files `cli.py` / `run_agent.py` / `gateway/run.py`
  being split into modules — which is encouraged), update the relevant section's
  anchors here.
- For *policy* questions (is this change allowed? where on the Footprint Ladder?), the
  source of truth is `AGENTS.md`, not this file.
