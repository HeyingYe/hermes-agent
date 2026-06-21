"""Tests for the warm ACP subprocess pool (token-maximization T2a).

Persistence reuses one claude-code-acp/copilot subprocess across API calls
instead of paying the ~10-14s cold spawn every time. These lock the pool's
lifecycle INVARIANTS (reuse-when-alive, respawn-when-dead, idle reap, size cap,
key isolation) with a fake process so no real CLI is spawned. The prompt
round-trip itself is verified live (latency drop) after a gateway restart.
"""

import agent.copilot_acp_client as acp
from agent.copilot_acp_client import (
    _AcpConnection,
    _AcpPool,
    _acp_persistence_settings,
)


class _FakePopen:
    def __init__(self) -> None:
        self._alive = True
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self._alive = False

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        self._alive = False


def _fake_start(self) -> None:
    """Stand-in for _AcpConnection.start: attach a fake live process, no real
    subprocess or reader threads."""
    self.proc = _FakePopen()


def _make_pool(monkeypatch) -> _AcpPool:
    monkeypatch.setattr(_AcpConnection, "start", _fake_start)
    return _AcpPool()


def test_acquire_then_release_keeps_connection_warm(monkeypatch):
    pool = _make_pool(monkeypatch)
    c1 = pool.acquire("cmd", [], "/cwd", idle_timeout=120)
    assert c1.alive()
    pool.release("cmd", [], "/cwd", c1, pool_size=5, idle_timeout=120)
    # Reuse the SAME warm connection — no respawn (this is the whole point).
    c2 = pool.acquire("cmd", [], "/cwd", idle_timeout=120)
    assert c2 is c1


def test_dead_connection_is_not_reused(monkeypatch):
    pool = _make_pool(monkeypatch)
    c1 = pool.acquire("cmd", [], "/cwd", idle_timeout=120)
    pool.release("cmd", [], "/cwd", c1, pool_size=5, idle_timeout=120)
    c1.proc._alive = False  # process died while idle in the pool
    c2 = pool.acquire("cmd", [], "/cwd", idle_timeout=120)
    assert c2 is not c1
    assert c2.alive()


def test_idle_expired_connection_is_reaped(monkeypatch):
    pool = _make_pool(monkeypatch)
    clock = {"now": 1000.0}
    monkeypatch.setattr(acp.time, "monotonic", lambda: clock["now"])
    c1 = pool.acquire("cmd", [], "/cwd", idle_timeout=10)
    pool.release("cmd", [], "/cwd", c1, pool_size=5, idle_timeout=10)
    clock["now"] += 100  # well past idle_timeout
    c2 = pool.acquire("cmd", [], "/cwd", idle_timeout=10)
    assert c2 is not c1
    assert not c1.alive()  # stale conn was closed during acquire sweep


def test_release_caps_pool_size(monkeypatch):
    pool = _make_pool(monkeypatch)
    conns = [pool.acquire("cmd", [], "/cwd", idle_timeout=120) for _ in range(4)]
    for c in conns:
        pool.release("cmd", [], "/cwd", c, pool_size=2, idle_timeout=120)
    bucket = pool._idle[_AcpPool._key("cmd", [], "/cwd")]
    assert len(bucket) == 2  # capped
    assert sum(1 for c in conns if not c.alive()) == 2  # the 2 over-cap were closed


def test_distinct_keys_do_not_share_process(monkeypatch):
    pool = _make_pool(monkeypatch)
    a = pool.acquire("cmd", [], "/cwdA", idle_timeout=120)
    pool.release("cmd", [], "/cwdA", a, pool_size=5, idle_timeout=120)
    b = pool.acquire("cmd", [], "/cwdB", idle_timeout=120)
    assert b is not a  # different cwd → different pooled process


def test_close_all_closes_everything(monkeypatch):
    pool = _make_pool(monkeypatch)
    c = pool.acquire("cmd", [], "/cwd", idle_timeout=120)
    pool.release("cmd", [], "/cwd", c, pool_size=5, idle_timeout=120)
    pool.close_all()
    assert not c.alive()
    assert pool._idle == {}


def test_persistence_disabled_by_default():
    # Default config keeps persistence OFF → per-call spawn (Copilot path
    # unchanged). Only an explicit route_decision.acp_persistent_process:true
    # turns the pool on.
    persistent, pool_size, idle = _acp_persistence_settings()
    assert persistent is False
    assert pool_size >= 1
    assert idle >= 1.0
