"""Tests for T2b persistent ACP session + disableBuiltInTools (token-maximization).

T2b keeps one ACP session per Hermes conversation (keyed by a fingerprint of its
opening) and sends only the new non-assistant turns; it must fall back to a fresh
full-history session whenever the prior-sent prefix diverges (compression/edit) or
the session is dead — never cross-talk. These tests lock the keying, the
incremental-vs-fresh decision, and the disableBuiltInTools wiring. Invariants, not
snapshots; no real subprocess is spawned.
"""

import agent.copilot_acp_client as m
from agent.copilot_acp_client import (
    ExternalACPClient,
    _conversation_fingerprint,
    _nonassistant_signature,
    _format_delta_as_prompt,
    _is_prefix,
)


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_fingerprint_stable_and_distinct():
    a = [{"role": "system", "content": "You are X"}, {"role": "user", "content": "hi"}]
    b = [{"role": "system", "content": "You are X"}, {"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    c = [{"role": "system", "content": "You are X"}, {"role": "user", "content": "DIFFERENT"}]
    assert _conversation_fingerprint(a) == _conversation_fingerprint(b)  # same opening
    assert _conversation_fingerprint(a) != _conversation_fingerprint(c)  # different opening


def test_fingerprint_none_without_user():
    assert _conversation_fingerprint([{"role": "system", "content": "X"}]) is None
    assert _conversation_fingerprint([]) is None


def test_signature_skips_assistant():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "tool", "content": "T1"},
        {"role": "user", "content": "U2"},
    ]
    sig = _nonassistant_signature(msgs)
    # indices of non-assistant messages, in order
    assert [i for i, _ in sig] == [0, 1, 3, 4]


def test_is_prefix():
    assert _is_prefix(["a", "b"], ["a", "b", "c"])
    assert _is_prefix(["a", "b"], ["a", "b"])
    assert not _is_prefix(["a", "x"], ["a", "b", "c"])
    assert not _is_prefix(["a", "b", "c"], ["a", "b"])


def test_delta_has_no_system_preamble():
    out = _format_delta_as_prompt([{"role": "user", "content": "just this"}])
    assert "just this" in out
    assert "ACP agent backend" not in out  # no system preamble
    assert "Available tools" not in out    # no tool re-listing


# ── T2b incremental-vs-fresh decision (no real subprocess) ────────────────────

class _FakeConn:
    def __init__(self, command, args, cwd):
        self.command, self.args, self.cwd = command, list(args), cwd
        self.proc = object()
        self.initialized = False
        self._alive = True
    def start(self):
        self.proc = object()
    def alive(self):
        return self._alive and self.proc is not None
    def close(self):
        self._alive = False
        self.proc = None


def _make_client(monkeypatch):
    m._T2B.close_all()  # clear registry between tests
    monkeypatch.setattr(m, "_AcpConnection", _FakeConn)
    monkeypatch.setattr(m, "_acp_persistence_settings", lambda: (True, 5, 120.0))
    c = ExternalACPClient(command="/bin/echo", args=[])
    calls = {"open": 0, "send": []}
    monkeypatch.setattr(c, "_ensure_initialized", lambda conn, t: None)
    monkeypatch.setattr(c, "_drain", lambda conn: None)
    def fake_open(conn, model, t, disable_tools=False):
        calls["open"] += 1
        return "sess-%d" % calls["open"]
    def fake_send(conn, session_id, prompt_text, t, disable_tools=False):
        calls["send"].append((session_id, prompt_text))
        return ("ok", "", {"inputTokens": 1, "outputTokens": 1, "cachedReadTokens": 0, "cachedWriteTokens": 0, "totalTokens": 2})
    monkeypatch.setattr(c, "_open_new_session", fake_open)
    monkeypatch.setattr(c, "_send_prompt", fake_send)
    return c, calls


def test_first_turn_is_fresh_full_send(monkeypatch):
    c, calls = _make_client(monkeypatch)
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"}]
    c._run_persistent(msgs, "claude-sonnet-4-6", None, None, 60.0, False)
    assert calls["open"] == 1                       # fresh session opened
    assert "U1" in calls["send"][0][1]             # full transcript sent
    assert "ACP agent backend" in calls["send"][0][1]  # full preamble present


def test_second_turn_is_incremental(monkeypatch):
    c, calls = _make_client(monkeypatch)
    t1 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"}]
    c._run_persistent(t1, "claude-sonnet-4-6", None, None, 60.0, False)
    t2 = t1 + [{"role": "assistant", "content": "A1"}, {"role": "user", "content": "U2"}]
    c._run_persistent(t2, "claude-sonnet-4-6", None, None, 60.0, False)
    assert calls["open"] == 1                       # NO new session — reused
    assert calls["send"][1][0] == "sess-1"         # same session id
    delta = calls["send"][1][1]
    assert "U2" in delta and "U1" not in delta     # only the new turn
    assert "ACP agent backend" not in delta        # no preamble on delta


def test_divergence_falls_back_to_fresh(monkeypatch):
    c, calls = _make_client(monkeypatch)
    t1 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"}]
    c._run_persistent(t1, "claude-sonnet-4-6", None, None, 60.0, False)
    # Compression rewrote the first user message -> prefix diverges -> fresh.
    t2 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1-COMPRESSED"},
          {"role": "assistant", "content": "A1"}, {"role": "user", "content": "U2"}]
    c._run_persistent(t2, "claude-sonnet-4-6", None, None, 60.0, False)
    assert calls["open"] == 2                       # new session opened (diverged)
    assert "ACP agent backend" in calls["send"][1][1]  # full resend


def test_dead_session_respawns(monkeypatch):
    c, calls = _make_client(monkeypatch)
    t1 = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"}]
    c._run_persistent(t1, "claude-sonnet-4-6", None, None, 60.0, False)
    # Kill the live connection in the registry.
    entry = m._T2B.acquire("/bin/echo", [], c._acp_cwd, _conversation_fingerprint(t1), 120.0)
    entry.conn.close()
    t2 = t1 + [{"role": "assistant", "content": "A1"}, {"role": "user", "content": "U2"}]
    c._run_persistent(t2, "claude-sonnet-4-6", None, None, 60.0, False)
    assert calls["open"] == 2                       # respawned after death


def test_unkeyable_conversation_uses_full_send(monkeypatch):
    c, calls = _make_client(monkeypatch)
    # No user message -> not keyable -> _run_prompt (full send). Stub _run_prompt.
    fell_back = {"v": False}
    def fake_run_prompt(prompt_text, *, timeout_seconds, model=None, disable_tools=False):
        fell_back["v"] = True
        return ("ok", "", None)
    monkeypatch.setattr(c, "_run_prompt", fake_run_prompt)
    c._run_persistent([{"role": "system", "content": "S"}], "claude-sonnet-4-6", None, None, 60.0, False)
    assert fell_back["v"] is True
    assert calls["open"] == 0


# ── disableBuiltInTools wiring ────────────────────────────────────────────────

def test_disable_builtin_tools_adds_meta(monkeypatch):
    c = ExternalACPClient(command="/bin/echo", args=[])
    conn = _FakeConn("/bin/echo", [], "/tmp")
    seen = []
    def fake_req(conn, method, params, *, timeout_seconds, **kw):
        seen.append((method, params))
        if method == "session/new":
            return {"sessionId": "s1", "configOptions": []}
        return {"usage": {"inputTokens": 1, "outputTokens": 1, "cachedReadTokens": 0, "cachedWriteTokens": 0, "totalTokens": 2}}
    monkeypatch.setattr(c, "_acp_request", fake_req)
    # model=None so _select_session_model is a no-op
    sid = c._open_new_session(conn=conn, model=None, timeout_seconds=60.0, disable_tools=True)
    c._send_prompt(conn=conn, session_id=sid, prompt_text="hi", timeout_seconds=60.0, disable_tools=True)
    new_params = dict(seen)["session/new"]
    prompt_params = dict(seen)["session/prompt"]
    assert new_params.get("_meta") == {"disableBuiltInTools": True}
    assert prompt_params.get("_meta") == {"disableBuiltInTools": True}


def test_disable_builtin_tools_off_sends_no_meta(monkeypatch):
    c = ExternalACPClient(command="/bin/echo", args=[])
    conn = _FakeConn("/bin/echo", [], "/tmp")
    seen = []
    def fake_req(conn, method, params, *, timeout_seconds, **kw):
        seen.append((method, params))
        if method == "session/new":
            return {"sessionId": "s1", "configOptions": []}
        return {}
    monkeypatch.setattr(c, "_acp_request", fake_req)
    sid = c._open_new_session(conn=conn, model=None, timeout_seconds=60.0, disable_tools=False)
    c._send_prompt(conn=conn, session_id=sid, prompt_text="hi", timeout_seconds=60.0, disable_tools=False)
    assert "_meta" not in dict(seen)["session/new"]
    assert "_meta" not in dict(seen)["session/prompt"]
