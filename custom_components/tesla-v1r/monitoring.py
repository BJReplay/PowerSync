"""Monitoring-mode transition helpers."""

from __future__ import annotations

import logging
from typing import Any

from .const import DOMAIN, SERVICE_RESTORE_NORMAL

_HANDOFF_ACTIVE = "_monitoring_handoff_active"
_LOGGER = logging.getLogger(__name__)


async def async_prepare_monitoring_handoff(hass: Any, entry: Any) -> None:
    """Release PowerSync control before persisting monitoring mode.

    Call this only for a verified disabled-to-enabled transition. Ordinary
    saves and lifecycle reloads while monitoring is already active must remain
    write-free.
    """
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data[_HANDOFF_ACTIVE] = True
    coordinator = entry_data.get("optimization_coordinator")
    execute_lock = getattr(coordinator, "_execute_lock", None)
    lock_acquired = False

    if execute_lock is not None:
        await execute_lock.acquire()
        lock_acquired = True

    active_state_keys = ("force_charge_state", "force_discharge_state")
    active_before = {
        key
        for key in active_state_keys
        if isinstance(entry_data.get(key), dict)
        and entry_data[key].get("active", False)
    }

    try:
        solaredge = entry_data.get("solaredge_coordinator")
        if solaredge is not None and solaredge.control_health != "ready":
            # Monitoring must remain available when device writes are unsafe.
            # Keep the existing state for supervised recovery.
            return

        has_service = getattr(hass.services, "has_service", None)
        if callable(has_service) and not has_service(DOMAIN, SERVICE_RESTORE_NORMAL):
            reserve_restore_pending = bool(
                coordinator
                and getattr(coordinator, "_pre_idle_backup_reserve", None) is not None
                and getattr(coordinator, "battery_controller", None)
            )
            if active_before or reserve_restore_pending:
                raise RuntimeError(
                    "restore normal service unavailable while PowerSync control "
                    "state still requires cleanup"
                )
            _LOGGER.warning(
                "Restore normal service is unavailable; continuing the connection "
                "handoff because the failed/unloaded entry has no active PowerSync "
                "control state"
            )
            return

        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_NORMAL,
            {"source": "manual", "_force_restore": True},
            blocking=True,
        )

        still_active = {
            key
            for key in active_before
            if isinstance(entry_data.get(key), dict)
            and entry_data[key].get("active", False)
        }
        if still_active:
            raise RuntimeError(
                "restore normal left active control state: "
                + ", ".join(sorted(still_active))
            )

        if (
            coordinator
            and getattr(coordinator, "_pre_idle_backup_reserve", None) is not None
            and getattr(coordinator, "battery_controller", None)
        ):
            restored = await coordinator._restore_pre_idle_backup_reserve(
                coordinator.battery_controller,
                "monitoring enabled",
                bypass_monitoring=True,
            )
            if not restored:
                raise RuntimeError("pre-IDLE backup reserve restore failed")
    finally:
        if lock_acquired:
            execute_lock.release()


def finish_monitoring_handoff(hass: Any, entry: Any) -> None:
    """Clear the temporary write-block after monitoring persistence."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if isinstance(entry_data, dict):
        entry_data.pop(_HANDOFF_ACTIVE, None)
