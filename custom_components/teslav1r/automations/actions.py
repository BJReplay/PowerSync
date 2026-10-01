"""
Action execution logic for HA automations.

Supported actions:
- set_backup_reserve: Set battery backup reserve percentage (Tesla)
- preserve_charge: Prevent battery discharge (Tesla: hold current SOC via backup reserve)
- set_operation_mode: Set Powerwall operation mode (Tesla only)
- force_discharge: Force battery discharge for a duration (Tesla)
- force_charge: Force battery charge for a duration (Tesla)
- set_grid_export: Set grid export rule (Tesla only)
- set_grid_charging: Enable/disable grid charging (Tesla only)
- restore_normal: Restore normal battery operation
"""

import asyncio
import logging
import math
from collections.abc import Mapping
from datetime import datetime
from datetime import time as dt_time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    DOMAIN,
)
from ..sensitive_logging import install_vin_log_filter

_LOGGER = logging.getLogger(__name__)
install_vin_log_filter(_LOGGER)

def _is_number_range_error(error_message: str) -> bool:
    """Return true when HA rejected a number.set_value write as out of range."""
    error_lower = str(error_message).lower()
    return any(
        pattern in error_lower
        for pattern in (
            "out_of_range",
            "out of range",
            "outside valid range",
        )
    )


def _tesla_entity_max_fallback_amps(
    requested_amps: int,
    entity_min: int,
    entity_max: int,
    *,
    allow_stale_entity_max_override: bool,
    configured_max_amps: int | None,
) -> int | None:
    """Return a lower entity-max fallback after a stale-max override is rejected."""
    if (
        not allow_stale_entity_max_override
        or configured_max_amps is None
        or entity_max <= 0
        or configured_max_amps <= entity_max
    ):
        return None

    fallback_amps = max(entity_min, entity_max)
    if requested_amps <= fallback_amps:
        return None

    return fallback_amps

def _coerce_positive_int(value: Any, default: int | None = None) -> int | None:
    """Return a positive integer from user/config input, or default when invalid."""
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return result if result > 0 else default


def _coerce_positive_float(value: Any) -> float | None:
    """Return a positive float from user/config input, or None when invalid."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _kw_from_power_state(state: Any) -> float:
    """Return a power state as kW, accepting W or kW entities."""
    power_kw, _available = _power_state_kw_reading(state)
    return power_kw


def _power_state_kw_reading(state: Any) -> tuple[float, bool]:
    """Return ``(kW, available)`` without conflating numeric zero and missing."""
    raw_state = getattr(state, "state", None) if state else None
    if raw_state is None or raw_state in ("unknown", "unavailable", ""):
        return 0.0, False
    try:
        power = float(raw_state)
    except (TypeError, ValueError):
        return 0.0, False
    if not math.isfinite(power):
        return 0.0, False

    unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).strip().lower()
    if unit in ("w", "watt", "watts"):
        return max(0.0, power / 1000.0), True
    if unit in ("kw", "kilowatt", "kilowatts"):
        return max(0.0, power), True
    return max(0.0, power / 1000.0 if abs(power) > 100 else power), True


def _is_tesla_battery(config_entry: ConfigEntry) -> bool:
    """Return whether the home battery is Tesla, including legacy entries."""
    from ..const import BATTERY_SYSTEM_TESLA, CONF_BATTERY_SYSTEM

    data = getattr(config_entry, "data", {}) or {}
    options = getattr(config_entry, "options", {}) or {}
    return (
        options.get(
            CONF_BATTERY_SYSTEM,
            data.get(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA),
        )
        == BATTERY_SYSTEM_TESLA
    )


def _coerce_percent(value: Any) -> int | None:
    """Return a 0-100 percent integer from coordinator/entity values."""
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return None
    if 0 < percent <= 1:
        percent *= 100
    return max(0, min(100, round(percent)))


def _tesla_preserve_reserve_percent(current_soc: int) -> int:
    """Map current SOC to a Tesla-valid reserve value for preserve-charge."""
    if current_soc >= 99:
        return 100
    if current_soc > 80:
        return 80
    return max(0, current_soc)


def _get_current_home_battery_soc(hass: HomeAssistant, config_entry: ConfigEntry) -> int | None:
    """Resolve the current home battery SOC from runtime coordinators or HA states."""
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})

    for key in (
        "tesla_coordinator",
        "powerwall_local_coordinator",
    ):
        coord = entry_data.get(key)
        data = getattr(coord, "data", None)
        if isinstance(data, dict):
            for data_key in ("battery_level", "percentage_charged", "battery_soc"):
                percent = _coerce_percent(data.get(data_key))
                if percent is not None:
                    return percent

    local_runtime = entry_data.get("powerwall_local")
    local_coord = local_runtime.get("coordinator") if isinstance(local_runtime, dict) else None
    local_data = getattr(local_coord, "data", None)
    if isinstance(local_data, dict):
        for data_key in ("battery_level", "percentage_charged", "battery_soc"):
            percent = _coerce_percent(local_data.get(data_key))
            if percent is not None:
                return percent

    states = getattr(hass, "states", None)
    if states is not None:
        for entity_id in (
            "sensor.power_sync_battery_level",
            "sensor.power_sync_tesla_battery_level",
        ):
            state = states.get(entity_id)
            percent = _coerce_percent(getattr(state, "state", None))
            if percent is not None:
                return percent

    return None



def _datetime_is_after(candidate: datetime | None, reference: Any) -> bool:
    """Compare HA timestamps without allowing mixed-awareness errors."""
    if not isinstance(candidate, datetime) or not isinstance(reference, datetime):
        return False
    if (candidate.tzinfo is None) != (reference.tzinfo is None):
        return False
    return candidate > reference



async def execute_actions(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    actions: list[dict[str, Any]],
    context: dict[str, Any] | None = None
) -> bool:
    """
    Execute a list of automation actions.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry for this integration
        actions: list of action dicts to execute
        context: Optional context with time_window_start, time_window_end, timezone

    Returns:
        True if at least one action executed successfully
    """
    success_count = 0

    for action in actions:
        try:
            action_type = action.get("action_type")
            params = action.get("parameters", {})
            if isinstance(params, str):
                import json
                params = json.loads(params) if params else {}

            result = await _execute_single_action(hass, config_entry, action_type, params, context)
            if result:
                success_count += 1
                _LOGGER.info(f"Executed action '{action_type}'")
            elif result is None:
                _LOGGER.debug(f"Action '{action_type}' skipped (not applicable for this system)")
            else:
                _LOGGER.warning(f"Action '{action_type}' returned False")
        except Exception as e:
            _LOGGER.error(f"Error executing action '{action.get('action_type')}': {e}")

    return success_count > 0


def _get_hass_state(hass: HomeAssistant | None, entity_id: str | None) -> Any:
    """Return a HA state object when available."""
    if not hass or not entity_id:
        return None
    try:
        return hass.states.get(entity_id)
    except Exception:
        return None


async def _execute_single_action(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    action_type: str,
    params: dict[str, Any],
    context: dict[str, Any] | None = None
) -> bool:
    """
    Execute a single action.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        action_type: Type of action to execute
        params: Action parameters
        context: Optional context with time_window_start, time_window_end, timezone

    Returns:
        True if action executed successfully
    """
    if action_type == "set_backup_reserve":
        return await _action_set_backup_reserve(hass, config_entry, params)
    elif action_type == "preserve_charge":
        return await _action_preserve_charge(hass, config_entry)
    elif action_type == "set_operation_mode":
        return await _action_set_operation_mode(hass, config_entry, params)
    elif action_type == "force_discharge":
        return await _action_force_discharge(hass, config_entry, params)
    elif action_type == "force_charge":
        return await _action_force_charge(hass, config_entry, params)
    elif action_type == "restore_inverter":
        return await _action_restore_inverter(hass, config_entry)
    elif action_type == "set_grid_export":
        return await _action_set_grid_export(hass, config_entry, params)
    elif action_type == "set_grid_charging":
        return await _action_set_grid_charging(hass, config_entry, params)
    elif action_type == "set_storm_watch":
        return await _action_set_storm_watch(hass, config_entry, params)
    elif action_type == "set_vpp_enrollment":
        return await _action_set_vpp_enrollment(hass, config_entry, params)
    elif action_type == "restore_normal":
        return await _action_restore_normal(hass, config_entry)
    elif action_type == "powerwall_go_off_grid":
        return await _action_powerwall_off_grid(hass, config_entry, "go_off_grid")
    elif action_type == "powerwall_reconnect_grid":
        return await _action_powerwall_off_grid(hass, config_entry, "reconnect")
    else:
        _LOGGER.warning(f"Unknown action type: {action_type}")
        return False


async def _action_powerwall_off_grid(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    action: str,
) -> bool:
    """Dispatch an automation into the Powerwall local off-grid service.

    Wraps ``power_sync.powerwall_go_off_grid`` / ``powerwall_reconnect_grid``
    with a single retry. Raises no exceptions — returns False on failure so
    the automation engine can surface a notification.
    """
    from ..const import DOMAIN
    service = (
        "powerwall_go_off_grid" if action == "go_off_grid" else "powerwall_reconnect_grid"
    )
    try:
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)
        return True
    except Exception as e:
        _LOGGER.error(f"powerwall local {action} failed: {e}")
        return False


async def _action_set_backup_reserve(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any]
) -> bool:
    """Set battery backup reserve percentage.

    Supports both Tesla Powerwall and SigEnergy systems.
    """
    from ..const import DOMAIN, SERVICE_SET_BACKUP_RESERVE

    # Accept both "percent" and "reserve_percent" for flexibility
    reserve_percent = params.get("percent") or params.get("reserve_percent")
    if reserve_percent is None:
        _LOGGER.error("set_backup_reserve: missing percent parameter")
        return False

    # Clamp to valid range
    reserve_percent = max(0, min(100, int(reserve_percent)))

    for attempt in range(10):
        try:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_BACKUP_RESERVE,
                {"percent": reserve_percent, "source": "automation"},
                blocking=True,
            )
            if attempt > 0:
                _LOGGER.info(f"set_backup_reserve succeeded on attempt {attempt + 1}")
            return True
        except Exception as e:
            delay = min(60, 5 * (2 ** attempt))  # 5, 10, 20, 40, 60, 60...
            _LOGGER.warning(f"set_backup_reserve attempt {attempt + 1}/10 failed: {e} — retrying in {delay}s")
            await asyncio.sleep(delay)
    _LOGGER.error(f"Failed to set backup reserve to {reserve_percent}% after 10 attempts")
    return False


async def _action_preserve_charge(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Prevent battery discharge."""
    from ..const import DOMAIN, SERVICE_SET_BACKUP_RESERVE
    if not _is_tesla_battery(config_entry):
        _LOGGER.debug("preserve_charge not supported for this non-Tesla system")
        return None

    current_soc = _get_current_home_battery_soc(hass, config_entry)
    if current_soc is None:
        _LOGGER.error("preserve_charge: could not determine current home battery SOC")
        return False

    reserve_percent = _tesla_preserve_reserve_percent(current_soc)
    if current_soc > 80 and reserve_percent == 80:
        _LOGGER.info(
            "preserve_charge: Tesla rejects backup reserve values 81-99%%; "
            "holding at 80%% for current SOC %d%%",
            current_soc,
        )

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_BACKUP_RESERVE,
            {
                "percent": reserve_percent,
                "source": "automation_preserve_charge",
            },
            blocking=True,
        )
        _LOGGER.info(
            "preserve_charge: set Tesla backup reserve to %d%% (current SOC %d%%)",
            reserve_percent,
            current_soc,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to preserve charge: {e}")
        return False


async def _action_set_operation_mode(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any]
) -> bool:
    """Set battery operation mode (Tesla only)."""
    from ..const import DOMAIN, SERVICE_SET_OPERATION_MODE

    mode = params.get("mode")
    if not mode:
        _LOGGER.error("set_operation_mode: missing mode parameter")
        return False

    valid_modes = ["self_consumption", "autonomous", "backup"]
    if mode not in valid_modes:
        _LOGGER.error(f"set_operation_mode: invalid mode '{mode}'")
        return False

    for attempt in range(10):
        try:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_OPERATION_MODE,
                {"mode": mode, "source": "automation"},
                blocking=True,
            )
            if attempt > 0:
                _LOGGER.info(f"set_operation_mode succeeded on attempt {attempt + 1}")
            return True
        except Exception as e:
            delay = min(60, 5 * (2 ** attempt))  # 5, 10, 20, 40, 60, 60...
            _LOGGER.warning(f"set_operation_mode attempt {attempt + 1}/10 failed: {e} — retrying in {delay}s")
            await asyncio.sleep(delay)
    _LOGGER.error(f"Failed to set operation mode to {mode} after 10 attempts")
    return False


async def _action_force_discharge(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any]
) -> bool:
    """Force battery discharge for a specified duration."""
    # Web app stores as "minutes", mobile app as "duration_minutes", HA automations as "duration"
    duration = params.get("duration") or params.get("duration_minutes") or params.get("minutes", 30)

    from ..const import DOMAIN, SERVICE_FORCE_DISCHARGE

    try:
        service_data: dict[str, Any] = {"duration": duration, "source": "automation"}
        power_w = params.get("power_w")
        if power_w is not None:
            service_data["power_w"] = int(power_w)
        if _is_tesla_battery(config_entry):
            response = await hass.services.async_call(
                DOMAIN,
                SERVICE_FORCE_DISCHARGE,
                service_data,
                blocking=True,
                return_response=True,
            )
            if not isinstance(response, Mapping) or response.get("success") is not True:
                error = response.get("error") if isinstance(response, Mapping) else None
                _LOGGER.warning(
                    "Tesla force discharge automation was not confirmed%s",
                    f": {error}" if error else "",
                )
                return False
        else:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_FORCE_DISCHARGE,
                service_data,
                blocking=True,
            )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to activate force discharge: {e}")
        return False


async def _action_force_charge(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any]
) -> bool:
    """Force battery charge for a specified duration."""
    # Web app stores as "minutes", mobile app as "duration_minutes", HA automations as "duration"
    duration = params.get("duration") or params.get("duration_minutes") or params.get("minutes", 60)

    from ..const import DOMAIN, SERVICE_FORCE_CHARGE

    try:
        service_data: dict[str, Any] = {"duration": duration, "source": "automation"}
        power_w = params.get("power_w")
        if power_w is not None:
            service_data["power_w"] = int(power_w)
        if _is_tesla_battery(config_entry):
            response = await hass.services.async_call(
                DOMAIN,
                SERVICE_FORCE_CHARGE,
                service_data,
                blocking=True,
                return_response=True,
            )
            if not isinstance(response, Mapping) or response.get("success") is not True:
                error = response.get("error") if isinstance(response, Mapping) else None
                _LOGGER.warning(
                    "Tesla force charge automation was not confirmed%s",
                    f": {error}" if error else "",
                )
                return False
        else:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_FORCE_CHARGE,
                service_data,
                blocking=True,
            )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to activate force charge: {e}")
        return False


async def _action_set_grid_export(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any]
) -> bool:
    """Set grid export rule (Tesla only)."""
    from ..const import DOMAIN, SERVICE_SET_GRID_EXPORT
    if not _is_tesla_battery(config_entry):
        _LOGGER.debug("set_grid_export not supported for non-Tesla systems")
        return None

    # Accept both "rule" and "grid_export_rule" for flexibility
    rule = params.get("rule") or params.get("grid_export_rule")
    if not rule:
        _LOGGER.error("set_grid_export: missing rule parameter")
        return False

    valid_rules = ["never", "pv_only", "battery_ok"]
    if rule not in valid_rules:
        _LOGGER.error(f"set_grid_export: invalid rule '{rule}'")
        return False

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_GRID_EXPORT,
            {"rule": rule, "source": "automation"},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set grid export: {e}")
        return False


async def _action_set_grid_charging(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any]
) -> bool:
    """Enable or disable grid charging (Tesla only)."""
    from ..const import DOMAIN, SERVICE_SET_GRID_CHARGING

    enabled = params.get("enabled", True)

    try:
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_GRID_CHARGING,
            {"enabled": enabled, "source": "automation"},
            blocking=True,
            return_response=True,
        )
        return isinstance(response, dict) and response.get("success") is True
    except Exception as e:
        _LOGGER.error(f"Failed to set grid charging: {e}")
        return False


async def _action_set_storm_watch(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any],
) -> bool:
    """Enable or disable Tesla Storm Watch via the set_storm_watch service."""
    from ..const import DOMAIN

    enabled = bool(params.get("enabled", True))
    try:
        await hass.services.async_call(
            DOMAIN, "set_storm_watch", {"enabled": enabled}, blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set Storm Watch: {e}")
        return False


async def _action_set_vpp_enrollment(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    params: dict[str, Any],
) -> bool:
    """Enroll or unenroll the site in a Tesla VPP / grid-services program."""
    from ..const import DOMAIN

    program_id = params.get("vpp_program_id") or params.get("program_id")
    if not program_id:
        _LOGGER.error("set_vpp_enrollment: missing program_id parameter")
        return False
    # Automation UI reuses the `enabled` key for clarity alongside storm_watch/grid_charging
    enrolled = params.get("enrolled")
    if enrolled is None:
        enrolled = params.get("enabled", True)
    enrolled = bool(enrolled)

    try:
        await hass.services.async_call(
            DOMAIN,
            "set_vpp_enrollment",
            {"program_id": str(program_id), "enrolled": enrolled},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to set VPP enrollment: {e}")
        return False


async def _action_restore_normal(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Restore normal battery operation (cancel force charge/discharge)."""
    from ..const import DOMAIN, SERVICE_RESTORE_NORMAL

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_NORMAL,
            {"source": "automation"},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to restore normal: {e}")
        return False


async def _action_restore_inverter(
    hass: HomeAssistant,
    config_entry: ConfigEntry
) -> bool:
    """Restore inverter to normal operation."""
    from ..const import DOMAIN, SERVICE_RESTORE_INVERTER

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_INVERTER,
            {},
            blocking=True,
        )
        return True
    except Exception as e:
        _LOGGER.error(f"Failed to restore inverter: {e}")
        return False


def _parse_time_window(time_str: str) -> dt_time | None:
    """Parse time string (HH:MM) to time object."""
    if not time_str:
        return None
    try:
        parts = time_str.split(":")
        return dt_time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def _get_window_end_datetime(
    window_end_str: str,
    window_start_str: str,
    timezone_str: str
) -> datetime | None:
    """Calculate the datetime when the time window ends.

    Handles windows that cross midnight (e.g., 22:00 - 06:00).
    Returns None if parsing fails.
    """
    from zoneinfo import ZoneInfo

    window_end = _parse_time_window(window_end_str)
    window_start = _parse_time_window(window_start_str)

    if not window_end:
        return None

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    today = now.date()

    # Create end datetime for today
    end_datetime = datetime.combine(today, window_end, tzinfo=tz)

    # Handle cross-midnight windows
    if window_start and window_end < window_start and now.time() >= window_start:
        # Window crosses midnight (e.g., 22:00 - 06:00)
        # If we're past midnight (current time < start time), end is today
        # If we're before midnight (current time >= start time), end is tomorrow
            # We're in the first part of the window (before midnight)
            # End is tomorrow
            from datetime import timedelta as td
            end_datetime = end_datetime + td(days=1)

    # If end time has already passed today, move to tomorrow
    if end_datetime <= now:
        from datetime import timedelta as td
        end_datetime = end_datetime + td(days=1)

    return end_datetime


def _is_inside_time_window(
    window_start_str: str,
    window_end_str: str,
    timezone_str: str
) -> bool:
    """Check if current time is inside the specified time window."""
    from zoneinfo import ZoneInfo

    window_start = _parse_time_window(window_start_str)
    window_end = _parse_time_window(window_end_str)

    if not window_start or not window_end:
        return True  # No window defined, always inside

    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz).time()

    # Handle cross-midnight windows
    if window_end < window_start:
        # Window crosses midnight (e.g., 22:00 - 06:00)
        return now >= window_start or now < window_end
    else:
        # Normal window (e.g., 06:00 - 22:00)
        return window_start <= now < window_end

def _live_status_power_kw(live_status: dict, key: str) -> float:
    """Return a live_status power value in kW."""
    try:
        return (float(live_status.get(key) or 0.0)) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _non_ev_home_load_kw(live_status: dict, current_ev_power_kw: float) -> float:
    """Derive non-EV home load from site balance when possible."""
    ev_kw = max(0.0, current_ev_power_kw)
    balance_keys = ("solar_power", "grid_power", "battery_power")
    if all(key in live_status and live_status.get(key) is not None for key in balance_keys):
        balanced_total_kw = (
            _live_status_power_kw(live_status, "solar_power")
            + _live_status_power_kw(live_status, "grid_power")
            + _live_status_power_kw(live_status, "battery_power")
        )
        return max(0.0, balanced_total_kw - ev_kw)

    load_power_kw = _live_status_power_kw(live_status, "load_power")
    if live_status.get("home_load_basis") == "excludes_ev":
        return max(0.0, load_power_kw)
    return max(0.0, load_power_kw - ev_kw)

def _optimizer_ev_ceiling_amps(
    planned_kw: float | None,
    *,
    voltage: float,
    phases: int,
    min_amps: int,
    max_amps: int,
) -> int | None:
    """Convert an optimizer power ceiling to an executable current ceiling."""
    if planned_kw is None:
        return None
    try:
        unit_w = max(1.0, float(voltage) * max(1, int(phases)))
        ceiling_amps = math.floor(float(planned_kw) * 1000.0 / unit_w + 1e-9)
    except (TypeError, ValueError, OverflowError):
        return None
    if ceiling_amps < max(1, int(min_amps)):
        return 0
    return min(max(0, int(max_amps)), ceiling_amps)




def _get_home_power_settings(hass, config_entry) -> dict:
    """Return app-managed home power settings from automation storage."""
    try:
        from ..const import DOMAIN
        entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
        store = entry_data.get("automation_store")
        if store:
            stored = getattr(store, '_data', {}) or {}
            settings = stored.get("home_power_settings", {})
            if isinstance(settings, dict):
                return settings
    except Exception:
        pass
    return {}


def _extract_tesla_site_max_meter_power_kw(data: Any) -> float | None:
    """Return Tesla's max site import limit from site_info/config payloads."""
    if not isinstance(data, dict):
        return None

    for key in (
        "max_site_meter_power_ac",
        "max_site_meter_power",
        "maxSiteMeterPowerAc",
        "MaxSiteMeterPowerAc",
    ):
        value_kw = _coerce_positive_float(data.get(key))
        if value_kw is not None:
            # Tesla cloud site_info normally reports kW here; local config
            # variants may report watts, so normalize large values.
            return round(value_kw / 1000.0 if value_kw > 1000 else value_kw, 3)

    for nested_key in ("site_info", "components", "config"):
        nested_value = _extract_tesla_site_max_meter_power_kw(data.get(nested_key))
        if nested_value is not None:
            return nested_value

    return None


def _get_cached_tesla_max_site_meter_power_kw(hass, config_entry) -> float | None:
    """Return Tesla's cached max site import limit when available."""
    try:
        from ..const import DOMAIN
        entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
    except Exception:
        return None

    for coord_key in ("tesla_coordinator", "coordinator"):
        coordinator = entry_data.get(coord_key)
        site_info = getattr(coordinator, "_site_info_cache", None) if coordinator else None
        value_kw = _extract_tesla_site_max_meter_power_kw(site_info)
        if value_kw is not None:
            return value_kw

    local_runtime = entry_data.get("powerwall_local") or {}
    local_coordinator = local_runtime.get("coordinator")
    local_snapshot = getattr(local_coordinator, "data", None) if local_coordinator else None
    raw_snapshot = getattr(local_snapshot, "raw", None) if local_snapshot else None
    value_kw = _extract_tesla_site_max_meter_power_kw(raw_snapshot)
    if value_kw is not None:
        return value_kw

    return _extract_tesla_site_max_meter_power_kw(entry_data.get("site_info"))


async def _get_tesla_max_site_meter_power_kw(hass, config_entry) -> float | None:
    """Return Tesla's max site import limit, preferring cached data."""
    cached = _get_cached_tesla_max_site_meter_power_kw(hass, config_entry)
    if cached is not None:
        return cached

    try:
        from ..const import DOMAIN
        entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
    except Exception:
        return None

    for coord_key in ("tesla_coordinator", "coordinator"):
        coordinator = entry_data.get(coord_key)
        if not coordinator or not hasattr(coordinator, "async_get_site_info"):
            continue
        try:
            site_info = await coordinator.async_get_site_info()
        except Exception as err:
            _LOGGER.debug("Could not fetch Tesla site_info for max site import: %s", err)
            continue
        value_kw = _extract_tesla_site_max_meter_power_kw(site_info)
        if value_kw is not None:
            return value_kw

    token_getter = entry_data.get("token_getter")
    site_id = entry_data.get("site_id")
    if not token_getter or not site_id:
        return None

    try:
        current_token, _current_provider = token_getter()
        if not current_token:
            return None

        import aiohttp

        url = f"https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/energy_sites/{site_id}/site_info"

        headers = {"Authorization": f"Bearer {current_token}"}
        async with aiohttp.ClientSession() as session, session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status != 200:
                _LOGGER.debug("Failed to get Tesla site_info for max site import: %s", response.status)
                return None
            data = await response.json()
            return _extract_tesla_site_max_meter_power_kw(data.get("response", {}))
    except Exception as err:
        _LOGGER.debug("Error getting Tesla max site import from site_info: %s", err)

    return None

def _tesla_connection_observation_timestamp(state: Any) -> float | None:
    """Return a comparable HA observation timestamp when one is available."""
    observed_at = getattr(state, "last_updated", None) or getattr(
        state,
        "last_changed",
        None,
    )
    if observed_at is None:
        return None
    try:
        return float(observed_at.timestamp())
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None




