"""Tests for optional Kanban gateway background services.

Kanban watcher loops live outside GatewayRunner and register through the generic
background-service seam only when explicitly enabled.
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import (
    GatewayKanbanWatchersService,
    _RunnerBackedKanbanWatchers,
    register_kanban_gateway_background_services,
)

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_service_defines_kanban_methods():
    for method in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersService, method), f"service missing {method}"


def test_gateway_runner_does_not_inherit_kanban_service():
    from gateway.run import GatewayRunner

    assert not issubclass(GatewayRunner, GatewayKanbanWatchersService)
    for method in KANBAN_METHODS:
        assert not hasattr(GatewayRunner, method), f"GatewayRunner hard-wires {method}"


def test_watcher_loops_are_coroutines():
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersService._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersService._kanban_dispatcher_watcher)


def test_register_kanban_services_is_config_gated(monkeypatch):
    from gateway.background_services import (
        clear_gateway_background_services_for_tests,
        list_gateway_background_services,
    )
    import hermes_cli.config as _cfg_mod

    clear_gateway_background_services_for_tests()
    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"kanban": {"dispatch_in_gateway": False}})
    assert register_kanban_gateway_background_services() is False
    assert list_gateway_background_services() == ()

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"kanban": {"dispatch_in_gateway": True}})
    assert register_kanban_gateway_background_services() is True
    assert {service.name for service in list_gateway_background_services()} == {
        "kanban-notifier",
        "kanban-dispatcher",
    }
    clear_gateway_background_services_for_tests()


def test_optional_service_discovery_imports_kanban_only_when_enabled(monkeypatch):
    from gateway.background_services import (
        clear_gateway_background_services_for_tests,
        list_gateway_background_services,
        register_optional_gateway_background_services,
    )
    import hermes_cli.config as _cfg_mod

    clear_gateway_background_services_for_tests()
    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"kanban": {"dispatch_in_gateway": False}})
    register_optional_gateway_background_services()
    assert list_gateway_background_services() == ()

    monkeypatch.setattr(_cfg_mod, "load_config", lambda: {"kanban": {"dispatch_in_gateway": True}})
    register_optional_gateway_background_services()
    assert {service.name for service in list_gateway_background_services()} == {
        "kanban-notifier",
        "kanban-dispatcher",
    }
    clear_gateway_background_services_for_tests()


def test_runner_backed_service_proxies_state():
    class Runner:
        _running = True

        def _active_profile_name(self):
            return "default"

    runner = Runner()
    service = _RunnerBackedKanbanWatchers(runner)

    assert service._running is True
    assert service._active_profile_name() == "default"
    service._kanban_dispatcher_lock_handle = "held"
    assert runner._kanban_dispatcher_lock_handle == "held"


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)
