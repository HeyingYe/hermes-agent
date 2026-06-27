"""Optional gateway background service registry.

Gateway core owns the lifecycle seam; feature modules register background
watchers here instead of being hard-inherited by ``GatewayRunner``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

GatewayBackgroundServiceFactory = Callable[[Any], Coroutine[Any, Any, None]]


@dataclass(frozen=True)
class GatewayBackgroundService:
    """A long-running optional gateway background service."""

    name: str
    factory: GatewayBackgroundServiceFactory


_gateway_background_services: list[GatewayBackgroundService] = []


def register_gateway_background_service(
    name: str,
    factory: GatewayBackgroundServiceFactory,
) -> None:
    """Register an optional gateway background service.

    Registration is idempotent by service name so modules can safely call it
    during repeated startup/plugin-discovery paths.
    """

    if any(service.name == name for service in _gateway_background_services):
        return
    _gateway_background_services.append(GatewayBackgroundService(name=name, factory=factory))


def list_gateway_background_services() -> tuple[GatewayBackgroundService, ...]:
    """Return registered optional gateway background services."""

    return tuple(_gateway_background_services)


def clear_gateway_background_services_for_tests() -> None:
    """Clear registered services for isolated unit tests."""

    _gateway_background_services.clear()


def _kanban_gateway_services_enabled() -> bool:
    """Return whether built-in Kanban gateway services should be imported."""

    import os

    env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
    if env_override in {"0", "false", "no", "off"}:
        return False
    if env_override in {"1", "true", "yes", "on"}:
        return True
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as exc:
        logger.warning("kanban gateway service discovery: cannot load config (%s); disabled", exc)
        return False
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    return bool(kanban_cfg.get("dispatch_in_gateway", False))


def register_optional_gateway_background_services() -> None:
    """Discover/register optional built-in gateway background services.

    Product-layer modules are imported only after their config gate opts in.
    """

    if not _kanban_gateway_services_enabled():
        logger.info("kanban gateway services: disabled")
        return
    try:
        from gateway.kanban_watchers import register_kanban_gateway_background_services
        register_kanban_gateway_background_services()
    except Exception:
        logger.warning("kanban gateway service registration failed", exc_info=True)


def start_gateway_background_services(runner: Any) -> list[asyncio.Task]:
    """Start all registered optional background services for *runner*.

    A failing factory is best-effort: log it and continue so optional feature
    services cannot prevent gateway startup.
    """

    tasks: list[asyncio.Task] = []
    for service in list_gateway_background_services():
        try:
            task = asyncio.create_task(
                service.factory(runner),
                name=f"gateway-background:{service.name}",
            )
        except Exception:
            logger.warning(
                "gateway background service %s failed to start",
                service.name,
                exc_info=True,
            )
            continue
        tasks.append(task)
    return tasks
