# Hermes/Jarvis Output Limit & Truncation Analysis Report

Date: 2026-06-25
Scope: `⚠️ Processing stopped: Response truncated due to output length limit. Try again.` in Feishu/gateway sessions
Principle: do not break normal complex tasks or long-running conversations; keep safety guardrails only where incomplete output could cause side effects.

## 1. Incident summary

A Lotus AI Pricing development task surfaced this gateway message:

```text
⚠️ Processing stopped: Response truncated due to output length limit. Try again.
```

The user-visible text is emitted by the gateway, but the owning layer is the agent conversation loop:

- `agent/conversation_loop.py` detects a provider response that ended due to output-length truncation or a truncated tool call.
- The loop returns an incomplete/partial result.
- `gateway/run.py::_normalize_empty_agent_response()` turns that incomplete result into the Feishu-visible warning.

This is not caused by Feishu's per-message length limit. Feishu outbound messages are split into chunks:

- `gateway/platforms/feishu.py`: `MAX_MESSAGE_LENGTH = 8000`
- `FeishuAdapter.send()` calls `truncate_message()` before sending.

## 2. Evidence from current runtime/config

Current config inspection showed:

```text
model.default = gpt-5.5
model.provider = custom
model.max_tokens = <missing>
model.max_output_tokens = <missing>
custom_providers[0] = Tocodex.space / gpt-5.5 / https://tocodex.space/v1
custom_providers[1] = 154.44.10.104:8080 / gpt-5.5 / http://154.44.10.104:8080/v1
custom provider max_output_tokens = not configured
```

So this incident was not caused by a locally configured low `model.max_tokens`. The most likely source is a provider/model output cap or stream/tool-call truncation being normalized by Hermes.

## 3. Restriction layers found

### 3.1 Provider/model output cap

Purpose: limit single model response generation.

Status: necessary.

Reason: Every provider has finite output capacity. Hermes must detect `finish_reason=length` or equivalent and avoid treating incomplete content as a complete answer.

Risk if removed: incomplete answers falsely reported as successful; broken tool-call JSON could be executed.

### 3.2 Tool-call truncation guard

Purpose: prevent execution of partial tool arguments.

Status: necessary, must remain a hard stop after retries.

Example risk:

```json
{"path":"report.md","content":"partial
```

Executing a partial `write_file`, `patch`, `terminal`, external-send, or permission-changing tool call would be unsafe. The correct behavior is retry, then stop with a clear statement that no partial tool action was executed.

### 3.3 Final-text truncation handling

Purpose: handle long assistant prose after model output cap.

Previous behavior: could degrade to a fatal-looking `Processing stopped` warning.

Status: the hard-stop behavior is not reasonable for normal complex tasks. Final text truncation is usually recoverable: continue, summarize, split, or write the long content to an artifact.

Current code already has a continuation path for normal `finish_reason=length` final text:

- collects partial text into `truncated_response_parts`
- appends a continuation prompt
- retries up to 3 times
- stitches the partial responses when a clean final response arrives

Remaining issue fixed in this pass: when all continuation attempts still hit the limit, return structured `error_code=final_text_truncated` so gateway/API consumers can distinguish this recoverable text failure from unsafe tool-call truncation.

### 3.4 Gateway user-visible normalization

Purpose: convert empty/partial agent results into chat-visible diagnostics.

Previous behavior: most partial results rendered as:

```text
⚠️ Processing stopped: <error>. Try again.
```

Status: too generic. It hides whether the system stopped for safety or hit a recoverable text limit.

Fix in this pass:

- `tool_call_truncated` / `tool_call_stream_interrupted`: explain that a tool call was cut off and no partial tool action was executed.
- `final_text_truncated`: explain that final text kept hitting the output limit and no unsafe tool action was executed.

### 3.5 Platform message length limits

Purpose: satisfy Feishu/Slack/Telegram/etc. message-size constraints.

Status: necessary, but should only affect delivery formatting.

Feishu already splits outbound text; it should not determine whether the agent task is complete.

### 3.6 Tool output/context truncation

Purpose: keep long logs/search results/tool outputs from flooding the model context.

Status: mostly necessary, but should preserve artifact paths/evidence where possible.

Good pattern:

- full output saved to artifact/session/log
- model sees summary + path + relevant excerpts
- user sees concise verification output

Bad pattern:

- only inject `[truncated]`
- lose the location of full evidence
- task cannot continue or verify

### 3.7 Cron/context injection truncation

Observed example:

- `cron/scheduler.py`: context_from latest output capped at 8000 chars.

Status: acceptable as a default safety cap, but not sufficient for complex chained jobs. Chained jobs should pass artifact paths and let downstream jobs read the full source when needed.

### 3.8 Agent turn/token limits

Purpose: protect against runaway loops and unbounded cost.

Status: necessary, but should not be based on cumulative session prompt tokens. Cumulative prompt tokens are telemetry/cost data, not context health.

Related current direction:

- use per-turn runaway breakers for safety
- use context compression for long conversations
- do not hard-stop valid long sessions purely because cumulative usage grew

## 4. Judgment under the user's principle

Principle: do not affect normal complex tasks or long-running conversations.

| Layer | Keep? | Change needed? | Reason |
|---|---:|---:|---|
| Provider output cap detection | Yes | Yes, classify better | Must detect incomplete output, but should recover for text. |
| Tool-call truncation stop | Yes | Yes, clearer diagnostics | Incomplete tool args are unsafe. |
| Final-text truncation hard stop | No | Yes | Long prose is recoverable; should continue/summarize/artifact. |
| Feishu/platform message limits | Yes | No major issue | Delivery should split, not block task. |
| Tool output truncation | Yes | Improve artifact preservation | Needed for context safety; must not lose evidence. |
| Cron context cap | Yes as default | Improve artifact chaining | 8000-char injection is fine, but not a full-context mechanism. |
| Long-session compression | Yes | Ensure compression truly succeeds | Required for long conversations. |
| Cumulative session token hard stop | No | Avoid | Would break long conversations. |

## 5. Implemented changes in this pass

### 5.1 Structured agent error codes

Added structured error codes in `agent/conversation_loop.py`:

- `final_text_truncated`
- `tool_call_truncated`
- `tool_call_stream_interrupted`
- `response_truncated`

This preserves safety behavior while allowing gateway/API layers to present accurate recovery guidance.

### 5.2 Gateway diagnostics

Updated `gateway/run.py::_normalize_empty_agent_response()`:

- `final_text_truncated`: tells the user final text hit output limit, no unsafe tool action was executed, and suggests continuing with a shorter summary/focused turn.
- `tool_call_truncated` / `tool_call_stream_interrupted`: tells the user a tool call was cut off before complete arguments and no partial tool action was executed.

This replaces a generic `Try again` with a cause-aware message.

### 5.3 API server error propagation

Updated `gateway/platforms/api_server.py` to include `result["error_code"]` in Hermes extras for both:

- soft partial 200 responses
- hard 502 incomplete error envelopes

This makes OpenAI-compatible clients able to distinguish recoverable final-text truncation from unsafe tool-call truncation.

### 5.4 Regression tests

Updated/added tests:

- `tests/run_agent/test_run_agent.py::TestRunConversation::test_length_with_tool_calls_returns_partial_without_executing_tools`
- `tests/test_lazy_session_regressions.py::TestGatewaySurfacesNullResponse::test_partial_tool_call_truncation_gets_safe_actionable_message`
- `tests/test_lazy_session_regressions.py::TestGatewaySurfacesNullResponse::test_partial_final_text_truncation_gets_recovery_message`
- `tests/gateway/test_api_server.py::TestChatCompletionsAgentIncomplete::test_truncation_with_partial_text_uses_length_finish_reason`
- `tests/gateway/test_api_server.py::TestChatCompletionsAgentIncomplete::test_failure_with_no_text_returns_502_error_envelope`

Verification command:

```bash
python -m pytest \
  tests/run_agent/test_run_agent.py::TestRunConversation::test_length_with_tool_calls_returns_partial_without_executing_tools \
  tests/run_agent/test_run_agent.py::TestRunConversation::test_length_finish_reason_requests_continuation \
  tests/test_lazy_session_regressions.py::TestGatewaySurfacesNullResponse::test_partial_tool_call_truncation_gets_safe_actionable_message \
  tests/test_lazy_session_regressions.py::TestGatewaySurfacesNullResponse::test_partial_final_text_truncation_gets_recovery_message \
  tests/gateway/test_api_server.py::TestChatCompletionsAgentIncomplete::test_truncation_with_partial_text_uses_length_finish_reason \
  tests/gateway/test_api_server.py::TestChatCompletionsAgentIncomplete::test_failure_with_no_text_returns_502_error_envelope \
  -q -o 'addopts='
```

Result:

```text
6 passed, 2 warnings in 4.85s
```

Syntax verification:

```bash
python -m py_compile \
  agent/conversation_loop.py \
  gateway/run.py \
  gateway/platforms/api_server.py \
  tests/test_lazy_session_regressions.py \
  tests/run_agent/test_run_agent.py \
  tests/gateway/test_api_server.py
```

Result: exit code 0.

## 6. Remaining recommended improvements

### P0/P1: Artifact-first long output contract

For coding and document-generation tasks, final responses should not contain huge code/log/report bodies. The agent should:

1. write code/report/log to files or target documents
2. run verification
3. send a short summary with file paths and key evidence

This avoids provider output caps and platform message splitting while preserving task completion.

### P1: Full evidence preservation for long tool outputs

When tool output is truncated before entering context, Hermes should consistently store the full output in a retrievable artifact and inject:

- summary
- artifact path
- command/tool metadata
- tail/head excerpts

### P1: Session incident metadata

Persist `error_code`, `finish_reason`, retry count, and whether any side-effecting tool executed into session metadata/logs. This would make post-incident triage faster than searching raw logs.

### P2: Cron chained-output artifact support

For `context_from`, inject summaries and artifact pointers rather than only the newest 8000 chars. Downstream jobs can then read the full source when necessary.

### P2: UX copy localization

The new gateway messages are English because this codebase's surrounding strings are English. If desired, move them into locale files and provide zh text for Feishu Chinese environments.

## 7. Final recommendation

Keep hard safety stops only for incomplete tool calls or side-effecting operations. Convert final-text/output-size failures into recovery paths: continuation, summarization, splitting, or artifact creation.

In short:

- Safety limits: hard guardrail.
- Display limits: split/format only.
- Long final text: recover or artifact, do not kill the task.
- Long session: compress and continue; cumulative telemetry should not stop work.
