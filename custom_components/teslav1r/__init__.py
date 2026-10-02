"""The Tesla v1r integration."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .automations.ev_phase_allocator import (
    normalize_home_power_settings as normalize_home_power_settings,
)
from .settings_metadata import optimizer_settings_groups
from .tesla_calibration import (
    CALIBRATION_SOURCE_LOCAL_ALERT as CALIBRATION_SOURCE_LOCAL_ALERT,
)
from .tesla_calibration import (
    clear_calibration_sources,
    dispatch_calibration_state,
)
from .tesla_force_tariff import configured_force_discharge_prices

# Module-level state for alert cooldowns (keyed by entry_id)
_last_discrepancy_alert: dict[str, datetime] = {}
_discrepancy_alert_count: dict[str, int] = {}
_discrepancy_alert_date: dict[str, str] = {}
DISCREPANCY_ALERT_COOLDOWN = timedelta(minutes=30)
DISCREPANCY_ALERT_DAILY_MAX = 4
AEMO_SETTLED_SYNC_DELAY_SECONDS = 5.0


async def _run_optional_write_guard(
    writer: Callable[[], Any],
    guard_write: Callable[[Callable[[], Any]], Any] | None = None,
) -> bool:
    """Run one actuator attempt through its immediate write guard, if any."""
    if guard_write is None:
        return bool(await writer())
    return bool(await guard_write(writer))


def _optimizer_settings_groups() -> dict[str, Any]:
    """Return mobile metadata for grouped optimizer settings."""
    return optimizer_settings_groups()


def _entry_percent_int(value: Any) -> int | None:
    """Parse config-entry ratio/percent values into an integer percent."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 1:
        parsed *= 100
    return max(0, min(100, round(parsed)))


def _normalize_tesla_backup_reserve_percent(value: Any) -> int:
    """Return a Tesla-supported reserve target."""
    target = _entry_percent_int(value)
    if target is None:
        target = 0
    return 100 if 81 <= target <= 99 else target


def _tesla_backup_reserve_pulse_percent(target_percent: int) -> int | None:
    """Return a safe temporary reserve above the target, when one exists."""
    if target_percent >= 100:
        return None
    if target_percent >= 80:
        return 100
    return target_percent + 1


def _disabled_optimizer_backup_reserve_target(entry: Any) -> tuple[int | None, str]:
    """Return the user reserve target when the Tesla v1r optimizer is disabled."""
    if entry is None:
        return None, "missing config entry"

    data = getattr(entry, "data", {}) or {}
    options = getattr(entry, "options", {}) or {}
    candidates = (
        # Legacy Controls writes used this private key. Prefer it during
        # migration because it represents the most recent physical reserve
        # chosen by the user, even when an older optimizer-owned value exists.
        (options.get("_user_backup_reserve"), "persisted user backup reserve"),
        (
            data.get(
                CONF_HARDWARE_BACKUP_RESERVE,
                options.get(CONF_HARDWARE_BACKUP_RESERVE),
            ),
            "hardware backup reserve config",
        ),
        (
            options.get(
                CONF_OPTIMIZATION_MANUAL_RESERVE,
                data.get(CONF_OPTIMIZATION_MANUAL_RESERVE),
            ),
            "manual optimizer reserve",
        ),
        (
            data.get(
                CONF_OPTIMIZATION_BACKUP_RESERVE,
                options.get(CONF_OPTIMIZATION_BACKUP_RESERVE),
            ),
            "optimizer floor config",
        ),
    )

    for value, source in candidates:
        target = _entry_percent_int(value)
        if target is not None:
            return target, source

    return _entry_percent_int(DEFAULT_OPTIMIZATION_BACKUP_RESERVE), "optimizer floor"


async def _restore_disabled_optimizer_reserve_if_stale(
    entry: Any,
    battery_coordinator: Any,
    battery_system: str,
    *,
    force_charge_state: dict[str, Any] | None = None,
    force_discharge_state: dict[str, Any] | None = None,
    hold_soc_state: dict[str, Any] | None = None,
) -> bool:
    """Undo a stale IDLE reserve left behind while the optimizer is disabled."""
    if not battery_coordinator:
        return False
    if battery_system in {"tesla", "sigenergy", "goodwe", BATTERY_SYSTEM_CUSTOM}:
        return False
    if (
        (force_charge_state or {}).get("active")
        or (force_discharge_state or {}).get("active")
        or (hold_soc_state or {}).get("active")
    ):
        return False

    target_reserve, target_source = _disabled_optimizer_backup_reserve_target(entry)
    if target_reserve is None:
        return False

    if not hasattr(battery_coordinator, "set_backup_reserve"):
        return False

    data = getattr(battery_coordinator, "data", None) or {}
    live_reserve = _entry_percent_int(
        data.get("backup_reserve")
        if data.get("backup_reserve") is not None
        else data.get("min_soc")
    )
    if live_reserve is None or live_reserve <= target_reserve + 5:
        return False

    try:
        soc = float(
            data.get("battery_level")
            if data.get("battery_level") is not None
            else data.get("battery_soc")
        )
        battery_kw = abs(float(data.get("battery_power", 0) or 0))
        grid_kw = float(data.get("grid_power", 0) or 0)
    except (TypeError, ValueError):
        return False

    soc_near_live_reserve = abs(soc - live_reserve) <= 2.0
    grid_importing = grid_kw >= 0.5
    battery_idle = battery_kw <= 0.15
    if not (soc_near_live_reserve and grid_importing and battery_idle):
        return False

    charge_cmd = data.get("charge_cmd")
    try:
        charge_cmd_int = int(charge_cmd) if charge_cmd is not None else None
    except (TypeError, ValueError):
        charge_cmd_int = None

    restore_method = getattr(battery_coordinator, "restore_work_mode_from_idle", None)
    if restore_method is None and charge_cmd_int in (0xAA, 0xBB):
        restore_method = getattr(battery_coordinator, "restore_normal", None)

    if restore_method is not None and not await restore_method():
        _LOGGER.warning(
            "Disabled optimizer %s reserve cleanup: mode restore failed; "
            "leaving reserve at %d%%",
            battery_system,
            live_reserve,
        )
        return False

    if not await battery_coordinator.set_backup_reserve(target_reserve):
        _LOGGER.warning(
            "Disabled optimizer %s reserve cleanup: failed to restore "
            "reserve from %d%% to %d%%",
            battery_system,
            live_reserve,
            target_reserve,
        )
        return False

    refresh = getattr(battery_coordinator, "async_request_refresh", None)
    if refresh:
        await refresh()
    _LOGGER.info(
        "Disabled optimizer %s reserve cleanup: restored stale reserve "
        "from %d%% to %d%% using %s",
        battery_system,
        live_reserve,
        target_reserve,
        target_source,
    )
    return True


def _parse_battery_health_timestamp(value: Any) -> datetime | None:
    """Parse a battery-health scan timestamp into a comparable datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _battery_health_payload_is_newer(candidate_ts: Any, current_ts: Any) -> bool:
    """Return True when the candidate battery-health result is newer."""
    candidate = _parse_battery_health_timestamp(candidate_ts)
    current = _parse_battery_health_timestamp(current_ts)
    if current is None:
        return candidate is not None
    if candidate is None:
        return False
    return candidate >= current


def _resolve_non_nem_timezone(hass, electricity_provider: str) -> str | None:
    """Pick an IANA timezone for non-NEM (non-Australian) providers.

    Sigenergy/FoxESS cloud sync converters fall back to Australia/Sydney when
    no NEM region is supplied. For Octopus UK and other non-NEM providers,
    that bucketing is ~10 hours off real local time. Returns None for
    Australian providers so the existing NEM-region path keeps running.
    """
    if electricity_provider == "octopus":
        return "Europe/London"
    # For any other non-AU provider, fall back to HA's configured timezone.
    if electricity_provider in ("nz", "epex", "other"):
        return getattr(hass.config, "time_zone", None)
    return None


# Valid NEM region codes. Used to validate user-supplied region values before
# we hand them to AEMOPriceCoordinator.
_NEM_REGIONS = ("NSW1", "VIC1", "QLD1", "SA1", "TAS1")

# IANA timezone → NEM region. Tesla site_info.installation_time_zone gives us
# this for free for Tesla Powerwall users; mapping it lets us spawn an
# AEMO-dispatch trigger for users on Amber / Localvolts / Flow Power non-AEMO
# without asking them to pick a region in the config flow.
# WA (Australia/Perth) and NT (Australia/Darwin) are intentionally absent —
# neither is on the National Electricity Market.
_TZ_TO_NEM_REGION: dict[str, str] = {
    "Australia/Sydney": "NSW1",
    "Australia/ACT": "NSW1",
    "Australia/Canberra": "NSW1",
    "Australia/NSW": "NSW1",
    "Australia/Broken_Hill": "NSW1",
    "Australia/Lord_Howe": "NSW1",
    "Australia/Melbourne": "VIC1",
    "Australia/Victoria": "VIC1",
    "Australia/Brisbane": "QLD1",
    "Australia/Lindeman": "QLD1",
    "Australia/Queensland": "QLD1",
    "Australia/Adelaide": "SA1",
    "Australia/South": "SA1",
    "Australia/Hobart": "TAS1",
    "Australia/Currie": "TAS1",
    "Australia/Tasmania": "TAS1",
}


def _nem_region_from_iana_tz(tz_name: str | None) -> str | None:
    """Map an IANA timezone (e.g. Australia/Sydney) to a NEM region code."""
    if not tz_name:
        return None
    return _TZ_TO_NEM_REGION.get(tz_name)


_FORCE_TARIFF_TEXT_MARKERS = ("force charge", "force discharge")
_FORCE_TARIFF_CODE_PREFIXES = ("charge_", "discharge_")
# Codes the AEMO spike / saving session managers upload themselves — never
# adopt these as a restore baseline (they are the temporary money-event
# tariff, not the user's real tariff). See _select_restorable_tesla_tariff.
_MANAGER_OWN_TARIFF_CODES = ("aemo-spike", "octopus-saving-session")


def _iter_tariff_strings(value: Any):
    """Yield string values from a Tesla tariff payload."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_tariff_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_tariff_strings(item)


def _is_tesla_v1r_force_tariff(tariff: Any) -> bool:
    """Return True for temporary Tesla v1r force charge/discharge tariffs."""
    if not isinstance(tariff, dict):
        return False

    code = str(tariff.get("code", "")).strip().lower()
    if code.startswith(_FORCE_TARIFF_CODE_PREFIXES):
        return True
    if code in _MANAGER_OWN_TARIFF_CODES:
        return True

    utility = str(tariff.get("utility", "")).strip().lower()
    if utility == "teslav1r":
        return True

    return any(
        marker in text.strip().lower()
        for text in _iter_tariff_strings(tariff)
        for marker in _FORCE_TARIFF_TEXT_MARKERS
    )


def _select_restorable_tesla_tariff(*tariffs: Any) -> dict[str, Any] | None:
    """Return the first tariff that is safe to use as a normal restore tariff."""
    for tariff in tariffs:
        if (
            isinstance(tariff, dict)
            and tariff
            and not _is_tesla_v1r_force_tariff(tariff)
        ):
            return copy.deepcopy(tariff)
    return None


def _extract_tesla_tariff_content(payload: Any) -> dict[str, Any] | None:
    """Extract a tariff from legacy and linked-rate-plan response shapes."""
    if not isinstance(payload, dict):
        return None

    response = payload.get("response")
    if isinstance(response, dict):
        nested_response = _extract_tesla_tariff_content(response)
        if nested_response:
            return nested_response

    containers = [payload]
    for key in (
        "tou_settings",
        "rate_plan",
        "rate_plan_settings",
        "utility_rate_plan",
    ):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            containers.append(candidate)

    for container in containers:
        for key in ("tariff_content_v2", "tariff_content"):
            tariff = container.get(key)
            if isinstance(tariff, dict) and tariff:
                return copy.deepcopy(tariff)
    return None


def _tariff_display_name(tariff: Any) -> str:
    if not isinstance(tariff, dict):
        return "unknown"
    return str(tariff.get("name") or tariff.get("code") or "unknown")


import re

import homeassistant.helpers.config_validation as cv
from aiohttp import web
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
)
from homeassistant.helpers.storage import Store

from .battery_backend.profiles import resolve_connection_profile
from .const import (
    BATTERY_SYSTEM_CUSTOM,
    CONF_BATTERY_CURTAILMENT_ENABLED,
    # Battery system selection
    CONF_BATTERY_SYSTEM,
    CONF_CURTAILMENT_CONTROL_IN_MONITORING_MODE,
    CONF_DEMAND_ALLOW_GRID_CHARGING,
    CONF_ELECTRICITY_PROVIDER,
    CONF_FLEET_API_BASE_URL,
    CONF_HARDWARE_BACKUP_RESERVE,
    CONF_MONITORING_MODE,
    CONF_OPTIMIZATION_BACKUP_RESERVE,
    CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
    CONF_OPTIMIZATION_MANUAL_RESERVE,
    CONF_OPTIMIZATION_MAX_CHARGE_W,
    CONF_OPTIMIZATION_MAX_DISCHARGE_W,
    CONF_OPTIMIZATION_MAX_GRID_EXPORT_W,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_TESLA_API_PROVIDER,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_TESLEMETRY_API_TOKEN,
    DEFAULT_CURTAILMENT_CONTROL_IN_MONITORING_MODE,
    DEFAULT_DISCHARGE_DURATION,
    DEFAULT_OPTIMIZATION_BACKUP_RESERVE,
    DISCHARGE_DURATIONS,
    DOMAIN,
    SERVICE_FORCE_CHARGE,
    SERVICE_FORCE_DISCHARGE,
    SERVICE_GET_CALENDAR_HISTORY,
    SERVICE_HOLD_BATTERY_SOC,
    SERVICE_RESTORE_NORMAL,
    SERVICE_SET_BACKUP_RESERVE,
    SERVICE_SET_GRID_CHARGING,
    SERVICE_SET_GRID_EXPORT,
    SERVICE_SET_OPERATION_MODE,
    SERVICE_SYNC_BATTERY_HEALTH,
    SERVICE_SYNC_NOW,
    SERVICE_SYNC_TOU,
    # Tesla integrations for device discovery
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
    TESLA_PROVIDER_FLEET_API,
    TESLA_PROVIDER_TESLEMETRY,
    get_tesla_api_base_url,
)
from .const import (
    CONF_DEMAND_CHARGE_START_TIME as CONF_DEMAND_CHARGE_START_TIME,
)
from .coordinator import (
    TeslaEnergyCoordinator,
)
from .currency import (
    DEFAULT_CURRENCY,
    currency_for_entry,
    currency_metadata,
    normalize_currency,
    presentation_currency_metadata_for_entry,
)
from .sensitive_logging import obfuscate_log_arg, obfuscate_vin_tokens
from .tesla_grid_control import (
    TeslaGridWriteStatus,
    async_set_tesla_grid_charging_confirmed,
    tesla_grid_charging_enabled_from_site_info,
    tesla_site_info_has_structure,
)


def _uses_native_battery_integration(coordinator: Any) -> bool:
    """Return whether battery telemetry/control is owned by another HA integration."""
    if not getattr(coordinator, "uses_native_battery_integration", False):
        return False
    enabled = getattr(coordinator, "_native_integration_enabled", None)
    return bool(enabled()) if callable(enabled) else True


def _configured_battery_capacity_kwh(entry: ConfigEntry) -> float | None:
    """Return the configured optimizer battery capacity in kWh."""
    raw_capacity_wh = entry.options.get(
        CONF_OPTIMIZATION_BATTERY_CAPACITY_WH,
        entry.data.get(CONF_OPTIMIZATION_BATTERY_CAPACITY_WH),
    )
    try:
        capacity_wh = float(raw_capacity_wh or 0)
    except (TypeError, ValueError):
        return None
    if capacity_wh <= 0:
        return None
    return round(capacity_wh / 1000.0, 2)


def _current_capacity_from_soh_kwh(
    rated_capacity_kwh: float | None,
    soh_percent: float | None,
) -> float | None:
    """Estimate current usable capacity from rated capacity and BMS SOH."""
    if rated_capacity_kwh is None or soh_percent is None:
        return None
    if rated_capacity_kwh <= 0 or soh_percent <= 0:
        return None
    return round(rated_capacity_kwh * soh_percent / 100.0, 2)


def _kw_from_ev_power(value: Any, unit: Any = None) -> float:
    try:
        power = float(value or 0)
    except (TypeError, ValueError):
        return 0.0

    unit_key = str(unit or "").strip().lower()
    if unit_key in ("w", "watt", "watts"):
        return power / 1000
    if unit_key in ("kw", "kilowatt", "kilowatts"):
        return power
    return power / 1000 if abs(power) > 100 else power


def _kw_from_power_state(state: Any) -> float:
    if not state or state.state in ("unknown", "unavailable"):
        return 0.0
    return _kw_from_ev_power(
        state.state,
        (getattr(state, "attributes", None) or {}).get("unit_of_measurement"),
    )


def _vehicle_identity_key(value: Any) -> str:
    """Normalize a vehicle identifier/name for best-effort matching."""
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _vehicle_identity_values(vehicle: dict) -> set[str]:
    values = {
        _vehicle_identity_key(vehicle.get("vehicle_id")),
        _vehicle_identity_key(vehicle.get("vin")),
        _vehicle_identity_key(vehicle.get("id")),
        _vehicle_identity_key(vehicle.get("vehicle_name")),
        _vehicle_identity_key(vehicle.get("display_name")),
        _vehicle_identity_key(vehicle.get("name")),
    }
    values.discard("")
    return values


def _vehicle_matches_identifier(vehicle: dict, identifier: Any) -> bool:
    key = _vehicle_identity_key(identifier)
    if not key:
        return False
    for value in _vehicle_identity_values(vehicle):
        if value == key:
            return True
        # VINs and Wall Connector payloads can arrive as embedded strings.
        if len(key) >= 8 and len(value) >= 8 and (key in value or value in key):
            return True
    return False


def _vehicle_has_exact_identifier(vehicle: dict, identifier: Any) -> bool:
    """Return whether a vehicle exposes one exact normalized identifier."""
    key = _vehicle_identity_key(identifier)
    return bool(key and key in _vehicle_identity_values(vehicle))


def _find_vehicle_status(vehicles: list[dict], *identifiers: Any) -> dict | None:
    for identifier in identifiers:
        for vehicle in vehicles:
            if _vehicle_matches_identifier(vehicle, identifier):
                return vehicle
    return None


_MIN_TESLA_CHARGING_POWER_KW = 1.4


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that obfuscates sensitive data like API keys and tokens.
    Shows first 4 and last 4 characters with asterisks in between.
    """

    @staticmethod
    def obfuscate(value: str, show_chars: int = 4) -> str:
        """Obfuscate a string showing only first and last N characters."""
        if len(value) <= show_chars * 2:
            return "*" * len(value)
        return f"{value[:show_chars]}{'*' * (len(value) - show_chars * 2)}{value[-show_chars:]}"

    def _obfuscate_string(self, text: str) -> str:
        """Apply all obfuscation patterns to a string."""
        if not text:
            return text

        # Handle Bearer tokens
        text = re.sub(
            r"(Bearer\s+)([a-zA-Z0-9_-]{20,})",
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle psk_ tokens (Amber API keys)
        text = re.sub(
            r"(psk_)([a-zA-Z0-9]{20,})",
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle user-supplied xAI and Gemini keys used for plan explanations.
        # The normal path never logs these values; this is defense in depth for
        # unexpected exception or third-party client output.
        text = re.sub(
            r"\b(xai-[a-zA-Z0-9_-]{20,})\b",
            lambda m: self.obfuscate(m.group(1)),
            text,
        )
        text = re.sub(
            r"\b(AIza[a-zA-Z0-9_-]{20,})\b",
            lambda m: self.obfuscate(m.group(1)),
            text,
        )

        # Handle authorization headers in websocket/API logs
        text = re.sub(
            r"(authorization:\s*Bearer\s+)([a-zA-Z0-9_-]{20,})",
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle site IDs (alphanumeric, like Amber 01KAR0YMB7JQDVZ10SN1SGA0CV)
        text = re.sub(
            r'(site[_\s]?[iI][dD]["\']?[\s:=]+["\']?)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
        )

        # Handle "for site {id}" pattern
        text = re.sub(
            r"(for site\s+)([a-zA-Z0-9-]{15,})",
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle email addresses
        text = re.sub(
            r"([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})",
            lambda m: self.obfuscate(m.group(1)),
            text,
        )

        # Handle Tesla energy site IDs (numeric, 13-20 digits) - in URLs and JSON
        text = re.sub(
            r'(energy_site[s]?[/\s:=]+["\']?)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle standalone long numeric IDs (Tesla energy site IDs in various contexts)
        text = re.sub(
            r"(\bsite\s+)(\d{13,})",
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle VIN numbers in JSON format ('vin': 'XXX' or "vin": "XXX")
        text = re.sub(
            r'(["\']vin["\']:\s*["\'])([A-HJ-NPR-Z0-9]{17})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        # Handle VIN numbers plain format
        text = re.sub(
            r"(\bvin[\s:=]+)([A-HJ-NPR-Z0-9]{17})\b",
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )
        text = obfuscate_vin_tokens(text, self.obfuscate)

        # Handle DIN numbers in JSON format
        text = re.sub(
            r'(["\']din["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        # Handle DIN numbers plain format
        text = re.sub(
            r'(\bdin[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle serial numbers in JSON format
        text = re.sub(
            r'(["\']serial_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        # Handle serial numbers plain format
        text = re.sub(
            r'(serial[\s_]?(?:number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle gateway IDs in JSON format
        text = re.sub(
            r'(["\']gateway_id["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        # Handle gateway IDs plain format
        text = re.sub(
            r'(gateway[\s_]?(?:id)?[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle warp site numbers in JSON format
        text = re.sub(
            r'(["\']warp_site_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        # Handle warp site numbers plain format
        text = re.sub(
            r'(warp[\s_]?(?:site)?(?:[\s_]?number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

        # Handle asset_site_id (UUIDs)
        text = re.sub(
            r'(["\']asset_site_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        # Handle device_id (UUIDs)
        text = re.sub(
            r'(["\']device_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE,
        )

        return text

    def _obfuscate_arg(self, arg: Any) -> Any:
        """Obfuscate an argument only if it contains sensitive data, preserving type otherwise."""
        return obfuscate_log_arg(arg, self._obfuscate_string)

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log record to obfuscate sensitive data."""
        # Handle the message
        if record.msg:
            record.msg = self._obfuscate_string(str(record.msg))

        # Handle args if present (for %-style formatting)
        # Only convert args to strings if obfuscation patterns match
        # This preserves numeric types for format specifiers like %d and %.3f
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: self._obfuscate_arg(v) for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(self._obfuscate_arg(a) for a in record.args)

        return True


_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(SensitiveDataFilter())


def _active_battery_system(
    entry: ConfigEntry,
    hass: HomeAssistant | None = None,
) -> str | None:
    """Return the effective battery/control brand for a config entry.

    The user's explicit CONF_BATTERY_SYSTEM selection (options override data) is
    authoritative. This matters after a brand switch: the per-brand save helpers
    historically merged the new brand's connection keys additively without
    removing the previous brand's host/station keys, so an entry could carry a
    stale CONF_SUNGROW_HOST while the user has since selected GoodWe. The old
    host-key-presence dispatch would then build a Sungrow coordinator against a
    dead endpoint while every CONF_BATTERY_SYSTEM reader believed it was GoodWe
    — a split-brain. Gating on CONF_BATTERY_SYSTEM mirrors the tariff layer,
    where a stale Amber token no longer masks the real price provider.

    Legacy entries created before CONF_BATTERY_SYSTEM existed carry no brand, so
    we fall back to detecting it from whichever connection key is present, using
    the same precedence as the historical if/elif dispatch chain. This keeps
    working single-brand installs untouched.
    """
    data = getattr(entry, "data", None) or {}
    options = getattr(entry, "options", None) or {}

    def _value(key: str) -> Any:
        return options.get(key, data.get(key))

    battery_system = _value(CONF_BATTERY_SYSTEM)
    if battery_system:
        return battery_system

    # Fall through to None → Tesla default.
    return None


PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.UPDATE,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Storage version for persisting data across HA restarts
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.storage"


def _normalise_tariff_rate_map(rates: Any) -> dict[str, float]:
    """Return comparable TOU rates rounded to avoid float representation noise."""
    if not isinstance(rates, dict):
        return {}
    normalised: dict[str, float] = {}
    for period, value in rates.items():
        try:
            normalised[str(period)] = round(float(value), 6)
        except (TypeError, ValueError):
            continue
    return normalised


def _tariff_charge_rates_by_season(
    tariff: dict[str, Any] | None,
    *,
    sell: bool,
) -> dict[str, dict[str, float]]:
    """Extract every non-empty season's comparable TOU rates."""
    if not isinstance(tariff, dict):
        return {}
    source = tariff.get("sell_tariff", {}) if sell else tariff
    if not isinstance(source, dict):
        return {}
    energy_charges = source.get("energy_charges", {})
    if not isinstance(energy_charges, dict):
        return {}

    normalised: dict[str, dict[str, float]] = {}
    for season_name, season in energy_charges.items():
        if not isinstance(season, dict):
            continue
        rates = season.get("rates")
        comparable = (
            _normalise_tariff_rate_map(rates)
            if isinstance(rates, dict)
            else _normalise_tariff_rate_map(season)
        )
        if comparable:
            normalised[str(season_name)] = comparable
    return normalised


def _strict_static_tariff_rates_by_season(
    tariff: dict[str, Any] | None,
    *,
    sell: bool,
) -> dict[str, dict[str, float]] | None:
    """Extract static readback rates without silently dropping bad values."""
    if not isinstance(tariff, dict):
        return None
    source = tariff.get("sell_tariff", {}) if sell else tariff
    if not isinstance(source, dict):
        return None
    energy_charges = source.get("energy_charges")
    if not isinstance(energy_charges, dict) or not energy_charges:
        return None

    normalised: dict[str, dict[str, float]] = {}
    for season_name, season in energy_charges.items():
        if not isinstance(season_name, str) or not season_name.strip():
            return None
        if not isinstance(season, dict) or not season:
            return None
        if "rates" in season:
            if set(season) != {"rates"} or not isinstance(season["rates"], dict):
                return None
            rates = season["rates"]
        else:
            rates = season
        if not rates:
            return None
        season_rates: dict[str, float] = {}
        for period, value in rates.items():
            if (
                not isinstance(period, str)
                or not period.strip()
                or isinstance(value, bool)
            ):
                return None
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(numeric_value):
                return None
            season_rates[period] = round(numeric_value, 6)
        normalised[season_name] = season_rates
    return normalised or None


def _tariff_charge_rates(
    tariff: dict[str, Any] | None, *, sell: bool
) -> dict[str, float]:
    """Extract Summer TOU rates from Tesla tariff_content shapes."""
    if not isinstance(tariff, dict):
        return {}
    source = tariff.get("sell_tariff", {}) if sell else tariff
    if not isinstance(source, dict):
        return {}
    summer = source.get("energy_charges", {}).get("Summer", {})
    if not isinstance(summer, dict):
        return {}
    rates = summer.get("rates")
    if isinstance(rates, dict):
        return _normalise_tariff_rate_map(rates)
    return _normalise_tariff_rate_map(summer)


def _tesla_tariff_matches_readback(
    expected: dict[str, Any],
    observed: dict[str, Any] | None,
) -> bool:
    """Return True when site_info reflects the tariff we just uploaded."""
    if not isinstance(observed, dict):
        return False

    # Static custom payloads carry a stable marker and must verify every
    # non-empty buy and sell season. This prevents a correct All Year map from
    # masking a mismatched secondary season (or a missing sell map).
    if expected.get("code") == "POWER_SYNC:STATIC_TOU":
        expected_buy_by_season = _strict_static_tariff_rates_by_season(
            expected, sell=False
        )
        observed_buy_by_season = _strict_static_tariff_rates_by_season(
            observed, sell=False
        )
        expected_sell_by_season = _strict_static_tariff_rates_by_season(
            expected, sell=True
        )
        observed_sell_by_season = _strict_static_tariff_rates_by_season(
            observed, sell=True
        )
        return (
            bool(expected_buy_by_season)
            and bool(expected_sell_by_season)
            and (
                observed_buy_by_season is not None
                and observed_sell_by_season is not None
                and expected_buy_by_season == observed_buy_by_season
                and expected_sell_by_season == observed_sell_by_season
            )
        )

    # Dynamic payloads retain the original preferred-Summer comparison and
    # optional-sell semantics. Tesla may omit its sentinel ALL season on readback.
    expected_buy = _tariff_charge_rates(expected, sell=False)
    observed_buy = _tariff_charge_rates(observed, sell=False)
    if expected_buy and observed_buy:
        if expected_buy != observed_buy:
            return False
        expected_sell = _tariff_charge_rates(expected, sell=True)
        observed_sell = _tariff_charge_rates(observed, sell=True)
        return not expected_sell or not observed_sell or expected_sell == observed_sell

    matched = 0
    for field in ("name", "utility", "code"):
        expected_value = expected.get(field)
        observed_value = observed.get(field)
        if expected_value is None or observed_value is None:
            continue
        if str(expected_value) != str(observed_value):
            return False
        matched += 1
    return matched > 0


async def _confirm_tesla_tariff_uploaded(
    session: aiohttp.ClientSession,
    api_base: str,
    site_id: str,
    headers: dict[str, str],
    tariff_data: dict[str, Any],
    *,
    schedule_seconds: tuple[float, ...] = (0.0, 1.0, 3.0, 7.0, 15.0, 25.0),
    timeout_seconds: float = 30.0,
) -> bool:
    """Poll Tesla site_info within a bounded eventual-consistency window."""
    url = f"{api_base}/api/1/energy_sites/{site_id}/site_info"
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + timeout_seconds
    attempts = len(schedule_seconds)

    for attempt, offset_seconds in enumerate(schedule_seconds, start=1):
        target_time = started_at + offset_seconds
        now = loop.time()
        if offset_seconds > 0 and now >= target_time:
            _LOGGER.debug(
                "Skipping missed Tesla TOU readback slot %.1fs for site %s",
                offset_seconds,
                site_id,
            )
            continue
        if now < target_time:
            await asyncio.sleep(min(target_time - now, max(0.0, deadline - now)))

        remaining_seconds = deadline - loop.time()
        if remaining_seconds <= 0:
            break
        request_timeout = min(5.0, remaining_seconds)
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as response:
                if response.status in (401, 403):
                    text = await response.text()
                    _LOGGER.error(
                        "TOU readback authorization failed for site %s: %s - %s",
                        site_id,
                        response.status,
                        text[:200],
                    )
                    return False
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.warning(
                        "TOU readback check failed for site %s: %s - %s",
                        site_id,
                        response.status,
                        text[:200],
                    )
                    continue
                data = await response.json()
                site_info = data.get("response", {})
                observed = _extract_tesla_tariff_content(site_info)
                if _tesla_tariff_matches_readback(tariff_data, observed):
                    elapsed_seconds = loop.time() - started_at
                    _LOGGER.info(
                        "Confirmed Tesla TOU tariff readback for site %s "
                        "after %.1fs (attempt %d/%d)",
                        site_id,
                        elapsed_seconds,
                        attempt,
                        attempts,
                    )
                    return True
                _LOGGER.debug(
                    "Tesla TOU readback for site %s did not match yet (attempt %d/%d)",
                    site_id,
                    attempt,
                    attempts,
                )
        except Exception as err:
            _LOGGER.warning(
                "TOU readback check error for site %s (attempt %d/%d): %s",
                site_id,
                attempt,
                attempts,
                err,
            )
    return False


async def send_tariff_to_tesla(
    hass: HomeAssistant,
    site_id: str,
    tariff_data: dict[str, Any],
    api_token: str,
    api_provider: str = TESLA_PROVIDER_TESLEMETRY,
    max_retries: int = 3,
    timeout_seconds: int = 60,
    fleet_base_url: str | None = None,
    confirm_readback: bool = True,
    accepted_status: dict[str, bool] | None = None,
) -> bool:
    """Send tariff data to Tesla via the configured provider with retry logic.

    Args:
        hass: HomeAssistant instance
        site_id: Tesla energy site ID
        tariff_data: Tariff data to send
        api_token: API token (Fleet API)
        api_provider: API provider fleet_api)
        max_retries: Maximum number of retry attempts (default: 3)
        timeout_seconds: Request timeout in seconds (default: 60)
        fleet_base_url: Regional Fleet API base URL override for EU/AP users.
        confirm_readback: Confirm the uploaded tariff appears in site_info before returning success.
        accepted_status: Optional mutable status populated with ``accepted=True``
            after Tesla accepts the upload, even if readback confirmation fails.

    Returns:
        True if successful, False otherwise
    """
    session = async_get_clientsession(hass)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    payload = {"tou_settings": {"tariff_content_v2": tariff_data}}

    # DEBUG: Log the exact payload being sent to Tesla to diagnose flat pricing issues
    try:
        buy_prices = (
            tariff_data.get("energy_charges", {}).get("Summer", {}).get("rates", {})
        )
        sell_prices = (
            tariff_data.get("sell_tariff", {})
            .get("energy_charges", {})
            .get("Summer", {})
            .get("rates", {})
        )
        if buy_prices:
            buy_values = list(buy_prices.values())
            sell_values = list(sell_prices.values()) if sell_prices else [0]
            unique_buy = len(set(buy_values))
            unique_sell = len(set(sell_values))
            _LOGGER.debug(
                "TOU payload: %d buy prices (min=$%.4f, max=$%.4f, avg=$%.4f, unique=%d)",
                len(buy_values),
                min(buy_values),
                max(buy_values),
                sum(buy_values) / len(buy_values),
                unique_buy,
            )
            _LOGGER.debug(
                "TOU payload: %d sell prices (min=$%.4f, max=$%.4f, unique=%d)",
                len(sell_values),
                min(sell_values),
                max(sell_values),
                unique_sell,
            )
            # Log sample periods to verify variation
            sample_periods = [
                "PERIOD_00_00",
                "PERIOD_06_00",
                "PERIOD_12_00",
                "PERIOD_18_00",
            ]
            for period in sample_periods:
                if period in buy_prices:
                    _LOGGER.debug(
                        "TOU sample: %s buy=$%.4f sell=$%.4f",
                        period,
                        buy_prices[period],
                        sell_prices.get(period, 0),
                    )
            # Log if prices appear flat (informational only)
            if unique_buy == 1:
                _LOGGER.debug(
                    "All buy prices are identical ($%.4f) - tariff will appear flat",
                    buy_values[0],
                )
            elif unique_buy <= 2:
                _LOGGER.debug(
                    "Only %d unique buy prices - tariff may appear flat", unique_buy
                )
    except Exception as err:
        _LOGGER.debug("Error logging payload details: %s", err)

    # Use correct API base URL based on provider
    api_base = get_tesla_api_base_url(api_provider, fleet_base_url)
    url = f"{api_base}/api/1/energy_sites/{site_id}/time_of_use_settings"
    _LOGGER.debug("Sending TOU schedule via %s API", api_provider)
    last_error = None
    retry_after_delay = None  # Set by Retry-After header

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                # Use Retry-After delay if available, otherwise exponential backoff
                wait_time = retry_after_delay or (2**attempt)
                retry_after_delay = None  # Reset for next attempt
                _LOGGER.info(
                    "TOU sync retry attempt %d/%d after %.0fs delay",
                    attempt + 1,
                    max_retries,
                    wait_time,
                )
                await asyncio.sleep(wait_time)

            _LOGGER.debug(
                "Sending TOU schedule to Tesla via %s for site %s (attempt %d/%d)",
                api_provider,
                site_id,
                attempt + 1,
                max_retries,
            )

            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    if accepted_status is not None:
                        accepted_status["accepted"] = True
                    _LOGGER.info(
                        "Successfully synced TOU schedule to Tesla (attempt %d/%d)",
                        attempt + 1,
                        max_retries,
                    )
                    _LOGGER.debug("Tesla API response: %s", result)
                    if not confirm_readback:
                        return True
                    if await _confirm_tesla_tariff_uploaded(
                        session,
                        api_base,
                        site_id,
                        headers,
                        tariff_data,
                    ):
                        return True
                    _LOGGER.error(
                        "TOU upload to Tesla was accepted but site_info did not confirm the tariff for site %s",
                        site_id,
                    )
                    return False

                # Log error and potentially retry
                error_text = await response.text()

                if response.status == 429:
                    # Rate limited — retry with Retry-After if provided
                    from .coordinator import _parse_retry_after

                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "TOU sync rate limited 429 (attempt %d/%d): %s (retry-after: %s)",
                        attempt + 1,
                        max_retries,
                        error_text[:200],
                        f"{retry_after_delay:.0f}s" if retry_after_delay else "not set",
                    )
                    last_error = "Rate limited 429"
                    continue  # Retry on 429

                if response.status >= 500:
                    # Server error — retry, respect Retry-After if present
                    from .coordinator import _parse_retry_after

                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Failed to sync TOU schedule: %s - %s (attempt %d/%d, will retry%s)",
                        response.status,
                        error_text[:200],
                        attempt + 1,
                        max_retries,
                        f", retry-after: {retry_after_delay:.0f}s"
                        if retry_after_delay
                        else "",
                    )
                    last_error = f"Server error {response.status}"
                    continue  # Retry on 5xx errors

                # Other 4xx client errors — don't retry
                _LOGGER.error(
                    "Failed to sync TOU schedule: %s - %s (client error, not retrying)",
                    response.status,
                    error_text,
                )
                return False

        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Error communicating with Tesla via %s (attempt %d/%d): %s",
                api_provider,
                attempt + 1,
                max_retries,
                err,
            )
            last_error = f"Network error: {err}"
            continue  # Retry on network errors

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Tesla API timeout via %s after %ds (attempt %d/%d)",
                api_provider,
                timeout_seconds,
                attempt + 1,
                max_retries,
            )
            last_error = f"Timeout after {timeout_seconds}s"
            continue  # Retry on timeout

        except Exception as err:
            _LOGGER.exception(
                "Unexpected error syncing TOU schedule (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                err,
            )
            last_error = f"Unexpected error: {err}"
            # Don't continue - unexpected errors might indicate a bug
            return False

    # All retries failed
    _LOGGER.error(
        "Failed to sync TOU schedule after %d attempts. Last error: %s",
        max_retries,
        last_error,
    )
    return False


def get_tesla_api_token(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[str | None, str]:
    """
    Get the current Tesla API token and provider for this entry.

    Honors the user's configured CONF_TESLA_API_PROVIDER:
    - powersync: returns the saved psync_ token + powersync provider
    - fleet_api: returns a fresh access token from the tesla_fleet HA integration
    - teslemetry: returns the saved Teslemetry token + teslemetry provider

    The tesla_fleet integration handles token refresh internally and updates its
    config entry data. We always fetch the latest token.

    Returns:
        tuple: (token, provider) where provider is 'powersync', 'fleet_api', or 'teslemetry'
    """
    configured_provider = entry.data.get(
        CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API
    )

    # Tesla Fleet API: pull a live token from the tesla_fleet integration
    if configured_provider == TESLA_PROVIDER_FLEET_API:
        tesla_fleet_entries = hass.config_entries.async_entries("tesla_fleet")
        for tesla_entry in tesla_fleet_entries:
            if tesla_entry.state == ConfigEntryState.LOADED:
                try:
                    if CONF_TOKEN in tesla_entry.data:
                        token_data = tesla_entry.data[CONF_TOKEN]
                        if CONF_ACCESS_TOKEN in token_data:
                            return token_data[
                                CONF_ACCESS_TOKEN
                            ], TESLA_PROVIDER_FLEET_API
                except Exception as e:
                    _LOGGER.warning(
                        f"Failed to extract token from Tesla Fleet integration: {e}"
                    )
        return None, TESLA_PROVIDER_FLEET_API

    # Teslemetry (default fallback)
    token = entry.data.get(CONF_TESLEMETRY_API_TOKEN)
    return (
        (token, TESLA_PROVIDER_TESLEMETRY)
        if token
        else (None, TESLA_PROVIDER_TESLEMETRY)
    )


def _get_tesla_site_configs(
    hass: HomeAssistant, entry: ConfigEntry
) -> list[tuple[str, str, str]]:
    """Return list of (site_id, token, provider) for the Tesla gateway."""
    configs = []
    primary_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
    if primary_id:
        token, provider = get_tesla_api_token(hass, entry)
        if token:
            configs.append((primary_id, token, provider))
    return configs


def _find_calendar_tariff_schedule(hass: HomeAssistant) -> dict | None:
    """Return the first stored tariff schedule with usable import rates."""
    for entry_data in hass.data.get(DOMAIN, {}).values():
        if not isinstance(entry_data, dict):
            continue
        tariff_schedule = entry_data.get("tariff_schedule")
        if not isinstance(tariff_schedule, dict):
            continue
        buy_rates = tariff_schedule.get("buy_rates") or tariff_schedule.get(
            "buy_prices"
        )
        if isinstance(buy_rates, dict) and buy_rates:
            return tariff_schedule
    return None


def _calendar_tou_periods_from_price_keys(
    price_rates: dict[str, Any],
) -> dict[str, dict[str, list[dict[str, int]]]]:
    """Build half-hour TOU definitions from ``PERIOD_HH_MM`` rate keys."""
    tou_periods: dict[str, dict[str, list[dict[str, int]]]] = {}
    for period_name in price_rates:
        try:
            prefix, hour_text, minute_text = str(period_name).rsplit("_", 2)
            if prefix != "PERIOD":
                continue
            from_hour = int(hour_text)
            from_minute = int(minute_text)
            if not 0 <= from_hour <= 23 or from_minute not in (0, 30):
                continue
        except (TypeError, ValueError):
            continue

        end_minutes = from_hour * 60 + from_minute + 30
        period = {
            "fromHour": from_hour,
            "fromMinute": from_minute,
            "toHour": end_minutes // 60,
            "toMinute": end_minutes % 60,
            "fromDayOfWeek": 0,
            "toDayOfWeek": 6,
        }
        tou_periods[str(period_name)] = {"periods": [period]}
    return tou_periods


def _calendar_time_series_is_subdaily(
    parsed_entries: list[tuple[dict[str, Any], Any]],
) -> bool:
    """Return True when more than one row belongs to the same calendar day."""
    dates = [timestamp.date() for _entry, timestamp in parsed_entries]
    return len(dates) != len(set(dates))


def _calculate_cost_from_tariff(
    tariff_schedule: dict,
    time_series: list,
    period: str | None = None,
) -> dict | None:
    """Calculate import/export costs from tariff schedule and time_series energy data.

    For hourly entries (Tesla day period): matches each entry's timestamp to a TOU period
    and looks up the corresponding buy/sell rate.

    For daily entries (Sungrow/FoxESS, or Tesla week/month/year): uses weighted average
    rates across TOU periods for that day's season.

    Returns cost summary dict or None if calculation fails.
    """
    from datetime import datetime as dt
    from datetime import timedelta

    try:
        buy_rates = (
            tariff_schedule.get("buy_rates") or tariff_schedule.get("buy_prices") or {}
        )
        sell_rates = (
            tariff_schedule.get("sell_rates")
            or tariff_schedule.get("sell_prices")
            or {}
        )
        seasons = tariff_schedule.get("seasons", {})
        tou_periods = tariff_schedule.get("tou_periods", {})

        if not isinstance(buy_rates, dict) or not buy_rates:
            return None
        if not isinstance(sell_rates, dict):
            sell_rates = {}
        if not isinstance(seasons, dict):
            seasons = {}
        if not isinstance(tou_periods, dict):
            tou_periods = {}
        if not tou_periods:
            tou_periods = _calendar_tou_periods_from_price_keys(buy_rates)

        def _parse_ts(ts_str: str) -> dt | None:
            """Parse ISO timestamp, handling Z suffix."""
            try:
                return dt.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

        parsed_entries = []
        for entry in time_series:
            ts = _parse_ts(entry.get("timestamp", ""))
            if ts is not None:
                parsed_entries.append((entry, ts))

        if period in ("week", "month", "year"):
            is_hourly = False
        elif period == "day":
            is_hourly = _calendar_time_series_is_subdaily(parsed_entries)
        else:
            is_hourly = _calendar_time_series_is_subdaily(parsed_entries)

        total_import_cost = 0.0
        total_export_earnings = 0.0

        if is_hourly:
            # Recorder/Tesla day rows are hourly energy buckets. Dynamic
            # provider tariffs can change on the half-hour, so price each row
            # from both half-hour slots rather than only its start timestamp.
            for entry, ts in parsed_entries:
                month = ts.month

                # Find season for this entry's month
                entry_season = _find_season_for_month(seasons, month)
                # Get TOU periods for that season
                season_tou = seasons.get(entry_season, {}).get(
                    "tou_periods", tou_periods
                )

                slot_buy_rates = []
                slot_sell_rates = []
                for slot_ts in (ts, ts + timedelta(minutes=30)):
                    tesla_dow = (slot_ts.weekday() + 1) % 7
                    matched_period = _match_tou_period(
                        season_tou,
                        slot_ts.hour,
                        tesla_dow,
                        minute=slot_ts.minute,
                        buy_rates=buy_rates,
                        sell_rates=sell_rates,
                    )
                    slot_buy_rates.append(
                        buy_rates.get(
                            matched_period,
                            buy_rates.get("ALL", buy_rates.get("OFF_PEAK", 0)),
                        )
                    )
                    slot_sell_rates.append(
                        sell_rates.get(
                            matched_period,
                            sell_rates.get("ALL", 0),
                        )
                    )

                buy_rate = sum(slot_buy_rates) / len(slot_buy_rates)
                sell_rate = sum(slot_sell_rates) / len(slot_sell_rates)

                grid_import_wh = entry.get("grid_import", 0)
                grid_export_wh = entry.get("grid_export", 0)

                total_import_cost += (grid_import_wh / 1000.0) * buy_rate
                total_export_earnings += (grid_export_wh / 1000.0) * sell_rate
        else:
            # Daily entries: use weighted average rate for each day
            for entry, ts in parsed_entries:
                month = ts.month
                entry_season = _find_season_for_month(seasons, month)
                season_tou = seasons.get(entry_season, {}).get(
                    "tou_periods", tou_periods
                )

                avg_buy, avg_sell = _weighted_avg_rates(
                    season_tou, buy_rates, sell_rates
                )

                grid_import_wh = entry.get("grid_import", 0)
                grid_export_wh = entry.get("grid_export", 0)

                total_import_cost += (grid_import_wh / 1000.0) * avg_buy
                total_export_earnings += (grid_export_wh / 1000.0) * avg_sell

        return {
            "import_cost": round(total_import_cost, 2),
            "export_earnings": round(total_export_earnings, 2),
            "net_cost": round(total_import_cost - total_export_earnings, 2),
            "estimated": True,
        }

    except Exception as e:
        _LOGGER.error(f"Error calculating cost from tariff: {e}", exc_info=True)
        return None


def _find_season_for_month(seasons: dict, month: int) -> str:
    """Find the season name for a given month."""
    for season_name, season_data in seasons.items():
        from_month = season_data.get("fromMonth", 1)
        to_month = season_data.get("toMonth", 12)
        if from_month <= to_month:
            if from_month <= month <= to_month:
                return season_name
        else:
            if month >= from_month or month <= to_month:
                return season_name
    return (
        "All Year" if "All Year" in seasons else next(iter(seasons.keys()), "All Year")
    )


def _match_tou_period(
    tou_periods: dict,
    hour: int,
    tesla_dow: int,
    *,
    minute: int = 0,
    buy_rates: dict | None = None,
    sell_rates: dict | None = None,
) -> str:
    """Match an hour and day-of-week to a TOU period name.

    Supports custom period names like PEAK_1, PEAK_2, OFF_PEAK_AUTO.
    SUPER_OFF_PEAK checked first, OFF_PEAK last as catch-all.
    """
    from datetime import datetime, timedelta

    from .tariff_time import find_matching_tou_period

    # 2024-01-07 was a Sunday, matching Tesla day 0.
    when = datetime(2024, 1, 7, hour, minute) + timedelta(days=tesla_dow % 7)
    return find_matching_tou_period(
        tou_periods,
        when,
        default="OFF_PEAK",
        buy_rates=buy_rates,
        sell_rates=sell_rates,
    )


def _weighted_avg_rates(
    tou_periods: dict, buy_rates: dict, sell_rates: dict
) -> tuple[float, float]:
    """Calculate weighted average buy/sell rates across all TOU periods for a full day.

    Weights each period by the number of hours it covers (averaged across all 7 days).
    """
    period_hours: dict[str, float] = {}
    total_hours = 0.0

    for period_name, period_data in tou_periods.items():
        if isinstance(period_data, dict) and "periods" in period_data:
            periods_list = period_data["periods"]
        elif isinstance(period_data, list):
            periods_list = period_data
        else:
            continue
        for p in periods_list:
            from_hour = p.get("fromHour", 0)
            from_minute = p.get("fromMinute", 0)
            to_hour = p.get("toHour", 24)
            to_minute = p.get("toMinute", 0)
            from_dow = p.get("fromDayOfWeek", 0)
            to_dow = p.get("toDayOfWeek", 6)
            num_days = max(0, to_dow - from_dow + 1)
            start_minutes = from_hour * 60 + from_minute
            end_minutes = to_hour * 60 + to_minute
            if end_minutes <= start_minutes:
                end_minutes += 24 * 60
            hours = (end_minutes - start_minutes) / 60 * num_days / 7.0
            period_hours[period_name] = period_hours.get(period_name, 0) + hours
            total_hours += hours

    if total_hours == 0:
        fallback_buy = buy_rates.get("ALL", buy_rates.get("OFF_PEAK", 0))
        fallback_sell = sell_rates.get("ALL", 0)
        return fallback_buy, fallback_sell

    avg_buy = 0.0
    avg_sell = 0.0
    for period_name, hours in period_hours.items():
        weight = hours / total_hours
        avg_buy += (
            buy_rates.get(
                period_name, buy_rates.get("ALL", buy_rates.get("OFF_PEAK", 0))
            )
            * weight
        )
        avg_sell += sell_rates.get(period_name, sell_rates.get("ALL", 0)) * weight

    return avg_buy, avg_sell


async def _calculate_cost_from_statistics(
    hass: HomeAssistant, period: str, end_date: str | None
) -> dict | None:
    """Calculate import/export costs from HA long-term statistics.

    Queries the recorder for hourly cost sensor data and sums it over the
    requested calendar period. Returns None if no statistics are available
    (e.g. new installation, sensors not yet recorded).
    """
    try:
        now = dt_util.now()

        # Determine end of period
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
                    tzinfo=now.tzinfo
                )
                # End of that day
                end_dt = end_dt.replace(hour=23, minute=59, second=59)
            except ValueError:
                end_dt = now
        else:
            end_dt = now

        # Calculate start of period using calendar ranges
        if period == "day":
            start_dt = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            # Start of week (Monday)
            days_since_monday = end_dt.weekday()
            start_dt = (end_dt - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif period == "month":
            start_dt = end_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif period == "year":
            start_dt = end_dt.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            return None

        # Find cost sensor entity IDs
        import_cost_entity = None
        export_earnings_entity = None
        for state in hass.states.async_all("sensor"):
            eid = state.entity_id
            if eid.endswith("daily_import_cost") and "power_sync" in eid:
                import_cost_entity = eid
            elif eid.endswith("daily_export_earnings") and "power_sync" in eid:
                export_earnings_entity = eid

        if not import_cost_entity:
            return None

        # Query recorder statistics
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        start_utc = dt_util.as_utc(start_dt)
        end_utc = dt_util.as_utc(end_dt)

        entity_ids = [import_cost_entity]
        if export_earnings_entity:
            entity_ids.append(export_earnings_entity)

        instance = get_instance(hass)
        stats = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start_utc,
            end_utc,
            set(entity_ids),
            "hour",
            None,
            {"change"},
        )

        if not stats:
            return None

        # Sum hourly change values
        total_import_cost = 0.0
        total_export_earnings = 0.0

        if import_cost_entity in stats:
            for entry in stats[import_cost_entity]:
                change = entry.get("change")
                if change is not None:
                    total_import_cost += change

        if export_earnings_entity and export_earnings_entity in stats:
            for entry in stats[export_earnings_entity]:
                change = entry.get("change")
                if change is not None:
                    total_export_earnings += change

        # Only return if we got meaningful data
        if total_import_cost == 0 and total_export_earnings == 0:
            # Check if there are any stats at all (vs just zero cost)
            has_any = any(len(stats.get(eid, [])) > 0 for eid in entity_ids)
            if not has_any:
                return None

        return {
            "import_cost": round(total_import_cost, 2),
            "export_earnings": round(total_export_earnings, 2),
            "net_cost": round(total_import_cost - total_export_earnings, 2),
            "estimated": False,
        }

    except Exception as exc:
        _LOGGER.debug("Failed to calculate cost from statistics: %s", exc)
        return None


_CALENDAR_ENERGY_SUMMARY_COORDINATORS = (
    ("custom_energy_coordinator", "Custom external controller"),
    ("sigenergy_coordinator", "Sigenergy"),
    ("sungrow_coordinator", "Sungrow"),
    ("foxess_coordinator", "FoxESS"),
    ("goodwe_coordinator", "GoodWe"),
    ("alphaess_coordinator", "AlphaESS"),
    ("esy_sunhome_coordinator", "ESY Sunhome"),
    ("solax_coordinator", "Solax"),
    ("saj_h2_coordinator", "SAJ H2"),
    ("fronius_reserva_coordinator", "Fronius GEN24 storage"),
    ("neovolt_coordinator", "Neovolt"),
    ("solaredge_coordinator", "SolarEdge"),
)


def _find_calendar_energy_summary_source(
    hass: HomeAssistant, preferred_entry_id: str | None = None
) -> tuple[str | None, Any | None, str | None]:
    """Return the first non-Tesla coordinator that exposes daily energy totals."""
    domain_data = hass.data.get(DOMAIN, {})
    ordered_entry_data: list[tuple[str, dict[str, Any]]] = []

    if preferred_entry_id:
        preferred_data = domain_data.get(preferred_entry_id)
        if isinstance(preferred_data, dict):
            ordered_entry_data.append((preferred_entry_id, preferred_data))

    for entry_id, data in domain_data.items():
        if entry_id == preferred_entry_id:
            continue
        if isinstance(data, dict):
            ordered_entry_data.append((entry_id, data))

    for entry_id, data in ordered_entry_data:
        for coordinator_key, system_name in _CALENDAR_ENERGY_SUMMARY_COORDINATORS:
            coordinator = data.get(coordinator_key)
            coordinator_data = getattr(coordinator, "data", None)
            if (
                coordinator is not None
                and isinstance(coordinator_data, dict)
                and "energy_summary" in coordinator_data
            ):
                return system_name, coordinator, entry_id

    return None, None, None


def _energy_summary_wh(energy_data: dict[str, Any], key: str) -> float:
    """Read a kWh value from energy_summary and convert it to Wh."""
    try:
        return float(energy_data.get(key) or 0) * 1000
    except (TypeError, ValueError):
        return 0


def _calendar_entry_from_energy_summary(coordinator: Any) -> dict[str, Any]:
    """Build a mobile-compatible entry from current coordinator daily totals."""
    energy_data = {}
    if coordinator and getattr(coordinator, "data", None):
        energy_data = coordinator.data.get("energy_summary", {}) or {}

    return {
        "timestamp": dt_util.now().isoformat(),
        "solar_generation": _energy_summary_wh(energy_data, "pv_today_kwh"),
        "battery_discharge": _energy_summary_wh(energy_data, "discharge_today_kwh"),
        "battery_charge": _energy_summary_wh(energy_data, "charge_today_kwh"),
        "grid_import": _energy_summary_wh(energy_data, "grid_import_today_kwh"),
        "grid_export": _energy_summary_wh(energy_data, "grid_export_today_kwh"),
        "home_consumption": _energy_summary_wh(energy_data, "load_today_kwh"),
    }


def _calendar_entry_with_detail_aliases(entry: dict[str, Any]) -> dict[str, Any]:
    """Add aggregate Tesla-style fields without inventing flow splits."""
    enriched = dict(entry)
    solar_wh = enriched.get("solar_generation", 0) or 0
    battery_discharge_wh = enriched.get("battery_discharge", 0) or 0
    battery_charge_wh = enriched.get("battery_charge", 0) or 0
    grid_import_wh = enriched.get("grid_import", 0) or 0
    grid_export_wh = enriched.get("grid_export", 0) or 0
    home_consumption_wh = enriched.get("home_consumption", 0) or 0

    enriched.setdefault("solar_energy_exported", solar_wh)
    enriched.setdefault("battery_energy_exported", battery_discharge_wh)
    enriched.setdefault("battery_energy_imported", battery_charge_wh)
    enriched.setdefault("consumer_energy_imported", home_consumption_wh)
    enriched.setdefault("grid_energy_imported", grid_import_wh)
    enriched.setdefault("grid_energy_exported", grid_export_wh)
    return enriched


_CALENDAR_STATISTIC_FIELDS = {
    "solar_generation": "daily_solar_energy",
    "battery_discharge": "daily_battery_discharge",
    "battery_charge": "daily_battery_charge",
    "grid_import": "daily_grid_import",
    "grid_export": "daily_grid_export",
    "home_consumption": "daily_load",
}

_CALENDAR_STATISTIC_FIELD_ALIASES = {
    "battery_discharge": ("daily_battery_discharge_foxess",),
    "battery_charge": ("daily_battery_charge_foxess",),
}


def _calendar_statistic_suffixes(field: str) -> tuple[str, ...]:
    """Return entity suffixes that can provide one calendar-history field."""
    primary = _CALENDAR_STATISTIC_FIELDS[field]
    return (primary, *_CALENDAR_STATISTIC_FIELD_ALIASES.get(field, ()))


def _calendar_entry_has_energy(entry: dict[str, Any] | None) -> bool:
    """Return True when a calendar entry contains at least one energy value."""
    if not entry:
        return False
    return any((entry.get(field) or 0) > 0 for field in _CALENDAR_STATISTIC_FIELDS)


def _calendar_time_series_totals_kwh(
    time_series: list[dict[str, Any]],
) -> dict[str, float]:
    """Sum mobile calendar-history rows into kWh for support logging."""
    totals: dict[str, float] = {}
    for field in _CALENDAR_STATISTIC_FIELDS:
        total_wh = 0.0
        for entry in time_series:
            try:
                total_wh += float(entry.get(field) or 0)
            except (TypeError, ValueError):
                continue
        totals[field] = round(total_wh / 1000, 3)
    return totals


def _calendar_energy_state_wh(state: Any) -> float:
    """Convert a Home Assistant energy sensor state to Wh."""
    if state is None:
        return 0
    raw_value = getattr(state, "state", None)
    if raw_value in (None, "", "unknown", "unavailable"):
        return 0
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0

    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "")).lower()
    if unit in ("wh", "watt-hour", "watt-hours"):
        return value
    return value * 1000


def _calendar_entry_from_energy_sensor_states(
    hass: HomeAssistant,
    preferred_entry_id: str | None,
) -> dict[str, Any] | None:
    """Build today's calendar entry from live Tesla v1r daily energy sensors."""
    entity_ids = _find_calendar_statistic_entity_ids(hass, preferred_entry_id)
    if not entity_ids:
        return None

    entry = {
        "timestamp": dt_util.now().isoformat(),
        "solar_generation": 0,
        "battery_discharge": 0,
        "battery_charge": 0,
        "grid_import": 0,
        "grid_export": 0,
        "home_consumption": 0,
    }
    for field, entity_id in entity_ids.items():
        entry[field] = _calendar_energy_state_wh(hass.states.get(entity_id))

    return entry if _calendar_entry_has_energy(entry) else None


def _merge_calendar_energy_entries(
    primary: dict[str, Any],
    fallback: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill missing/zero energy fields in primary from fallback."""
    if not fallback:
        return primary
    merged = dict(primary)
    for field in _CALENDAR_STATISTIC_FIELDS:
        if (merged.get(field) or 0) <= 0 and (fallback.get(field) or 0) > 0:
            merged[field] = fallback[field]
    return merged


def _calendar_current_entry(
    hass: HomeAssistant | None,
    coordinator: Any,
    preferred_entry_id: str | None = None,
) -> dict[str, Any]:
    """Build a current-day calendar entry using all live non-Tesla sources."""
    entry = _calendar_entry_from_energy_summary(coordinator)
    if hass is None:
        return _calendar_entry_with_detail_aliases(entry)
    state_entry = _calendar_entry_from_energy_sensor_states(hass, preferred_entry_id)
    return _calendar_entry_with_detail_aliases(
        _merge_calendar_energy_entries(entry, state_entry)
    )


def _calendar_time_series_from_energy_summary(
    coordinator: Any,
    hass: HomeAssistant | None = None,
    entry_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build a mobile-compatible one-point history from live daily totals."""
    return [_calendar_current_entry(hass, coordinator, entry_id)]


def _calendar_residual_entry(
    current_entry: dict[str, Any],
    existing_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the current live total minus recorder rows already emitted."""
    residual = {
        "timestamp": current_entry.get("timestamp") or dt_util.now().isoformat(),
        "solar_generation": 0,
        "battery_discharge": 0,
        "battery_charge": 0,
        "grid_import": 0,
        "grid_export": 0,
        "home_consumption": 0,
    }
    for field in _CALENDAR_STATISTIC_FIELDS:
        try:
            current_wh = float(current_entry.get(field) or 0)
        except (TypeError, ValueError):
            current_wh = 0
        emitted_wh = 0.0
        for row in existing_rows:
            try:
                emitted_wh += float(row.get(field) or 0)
            except (TypeError, ValueError):
                continue
        residual[field] = max(0, current_wh - emitted_wh)

    return (
        _calendar_entry_with_detail_aliases(residual)
        if _calendar_entry_has_energy(residual)
        else None
    )


def _calendar_reconcile_current_day_rows(
    rows: list[dict[str, Any]],
    current_entry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Keep today's recorder rows consistent with the live daily snapshot.

    Home Assistant's ``change`` statistic can carry a daily-reset sensor's
    previous terminal value into the new day.  When that happens, appending a
    non-negative residual produces a mixed-day response because an overcount
    cannot be subtracted.  The live Tesla v1r daily sensors are the
    authoritative current-day total, so fall back to that snapshot when a
    recorder field materially exceeds it.  Small rounding differences retain
    the hourly rows and normal residual behavior.
    """
    if not rows:
        return (
            [_calendar_entry_with_detail_aliases(current_entry)]
            if _calendar_entry_has_energy(current_entry)
            else []
        )

    live_ahead_fields: list[str] = []
    recorded_ahead_fields: list[str] = []
    for field in _CALENDAR_STATISTIC_FIELDS:
        try:
            live_wh = max(0.0, float(current_entry.get(field) or 0))
        except (TypeError, ValueError):
            live_wh = 0.0
        recorded_wh = 0.0
        for row in rows:
            try:
                recorded_wh += max(0.0, float(row.get(field) or 0))
            except (TypeError, ValueError):
                continue
        material_delta_wh = max(500.0, live_wh * 0.05)
        if live_wh > recorded_wh + material_delta_wh:
            live_ahead_fields.append(field)
        elif recorded_wh > live_wh + material_delta_wh:
            recorded_ahead_fields.append(field)

    # A fresh or unrestored accumulator is normally behind recorder history in
    # every field.  The reset-skew failure has a different signature: at least
    # one live total has progressed beyond recorder while one or more recorder
    # totals still carry yesterday's much larger terminal values.  Requiring
    # both directions avoids replacing valid history for a merely incomplete
    # accumulator or a small statistics-rounding difference, while also
    # catching a single stale field (for example yesterday's solar total before
    # today's first generation).
    if live_ahead_fields and recorded_ahead_fields:
        _LOGGER.warning(
            "Calendar day recorder/live totals are materially mixed "
            "(live ahead: %s; recorder ahead: %s); using the live snapshot",
            ", ".join(live_ahead_fields),
            ", ".join(recorded_ahead_fields),
        )
        return [_calendar_entry_with_detail_aliases(current_entry)]

    reconciled = list(rows)
    residual_entry = _calendar_residual_entry(current_entry, rows)
    if residual_entry:
        reconciled.append(residual_entry)
    return reconciled


def _calendar_period_range(
    period: str, end_date: str | None
) -> tuple[datetime, datetime] | None:
    """Return local start/end datetimes for a calendar-history request."""
    now = dt_util.now()
    if end_date:
        try:
            parsed_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            parsed_date = now.date()
    else:
        parsed_date = now.date()

    end_day_start = datetime(
        parsed_date.year,
        parsed_date.month,
        parsed_date.day,
        tzinfo=now.tzinfo,
    )

    if period == "day":
        start_dt = end_day_start
    elif period == "week":
        start_dt = end_day_start - timedelta(days=end_day_start.weekday())
    elif period == "month":
        start_dt = end_day_start.replace(day=1)
    elif period == "year":
        start_dt = end_day_start.replace(month=1, day=1)
    else:
        return None

    end_dt = end_day_start + timedelta(days=1)
    if start_dt.date() <= now.date() <= end_day_start.date():
        end_dt = now

    return start_dt, end_dt


def _calendar_range_includes_today(
    start_dt: datetime,
    end_dt: datetime,
    now: datetime | None = None,
) -> bool:
    """Return True when a history range should include today's live row."""
    now = now or dt_util.now()
    today_start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return start_dt <= now and end_dt > today_start


def _calendar_statistics_end_dt(
    period: str,
    end_dt: datetime,
    now: datetime,
    includes_today: bool,
) -> datetime:
    """Return the statistics query end while avoiding duplicated live totals."""
    if not includes_today:
        return end_dt
    if period == "day":
        return end_dt
    today_start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    return min(end_dt, today_start)


def _calendar_history_bucket_timestamp(
    timestamp: datetime,
    period: str,
) -> str:
    """Return the calendar-history bucket timestamp for a raw recorder state."""
    local_timestamp = dt_util.as_local(timestamp)
    if period == "day":
        bucket = local_timestamp.replace(minute=0, second=0, microsecond=0)
    else:
        bucket = local_timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return bucket.isoformat()


def _calendar_time_series_from_state_history_rows(
    history: dict[str, list[Any]],
    entity_to_field: dict[str, str],
    period: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    """Build calendar-history deltas from raw daily sensor state history."""
    time_series: dict[str, dict[str, Any]] = {}

    for entity_id, field in entity_to_field.items():
        states = sorted(
            (history or {}).get(entity_id, []) or [],
            key=lambda state: (
                getattr(state, "last_changed", None)
                or getattr(state, "last_updated", None)
                or start_dt
            ),
        )
        previous_wh: float | None = None
        previous_date = None
        for state in states:
            state_time = getattr(state, "last_changed", None) or getattr(
                state, "last_updated", None
            )
            if state_time is None:
                continue

            local_time = dt_util.as_local(state_time)
            if local_time < start_dt or local_time >= end_dt:
                continue

            state_date = local_time.date()
            current_wh = _calendar_energy_state_wh(state)
            if current_wh <= 0:
                if previous_wh is None or previous_date != state_date:
                    previous_wh = current_wh
                    previous_date = state_date
                continue

            # Daily sensors reset to zero around midnight. Treat a lower value
            # on a later local date as a new-day delta. Ignore same-day drops,
            # which can appear as transient restore/reload states and would
            # otherwise duplicate the next good cumulative value.
            if previous_wh is None or previous_date != state_date:
                delta_wh = current_wh
            elif current_wh >= previous_wh:
                delta_wh = current_wh - previous_wh
            else:
                continue
            previous_wh = current_wh
            previous_date = state_date
            if delta_wh <= 0:
                continue

            timestamp = _calendar_history_bucket_timestamp(local_time, period)
            row = time_series.setdefault(
                timestamp,
                {
                    "timestamp": timestamp,
                    "solar_generation": 0,
                    "battery_discharge": 0,
                    "battery_charge": 0,
                    "grid_import": 0,
                    "grid_export": 0,
                    "home_consumption": 0,
                },
            )
            row[field] += delta_wh

    return [
        _calendar_entry_with_detail_aliases(time_series[key])
        for key in sorted(time_series)
    ]


async def _calendar_time_series_from_state_history(
    hass: HomeAssistant,
    period: str,
    start_dt: datetime,
    end_dt: datetime,
    entity_ids: dict[str, str],
) -> list[dict[str, Any]]:
    """Build calendar history from raw recorder states when statistics are empty."""
    if not entity_ids or end_dt <= start_dt:
        return []

    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states

        recorder = get_instance(hass)
        if recorder is None:
            return []

        entity_to_field = {entity_id: field for field, entity_id in entity_ids.items()}
        history = await recorder.async_add_executor_job(
            get_significant_states,
            hass,
            start_dt,
            end_dt,
            list(entity_to_field),
            None,
            False,
        )
        rows = await hass.async_add_executor_job(
            _calendar_time_series_from_state_history_rows,
            history or {},
            entity_to_field,
            period,
            start_dt,
            end_dt,
        )
        if rows:
            _LOGGER.info(
                "Calendar history using recorder state fallback: rows=%d",
                len(rows),
            )
        return rows
    except Exception as exc:
        _LOGGER.debug("Failed to build calendar history from state history: %s", exc)
        return []


def _find_calendar_statistic_entity_ids(
    hass: HomeAssistant,
    preferred_entry_id: str | None,
) -> dict[str, str]:
    """Find Tesla v1r daily energy sensor entity IDs for recorder statistics."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_ids: dict[str, str] = {}

    for entity in registry.entities.values():
        if entity.domain != "sensor" or entity.platform != DOMAIN:
            continue
        unique_id = str(entity.unique_id or "")
        if preferred_entry_id and not unique_id.startswith(f"{preferred_entry_id}_"):
            continue

        for field in _CALENDAR_STATISTIC_FIELDS:
            for suffix in _calendar_statistic_suffixes(field):
                if unique_id.endswith(f"_{suffix}") and hass.states.get(
                    entity.entity_id
                ):
                    entity_ids.setdefault(field, entity.entity_id)
                    break

    if len(entity_ids) == len(_CALENDAR_STATISTIC_FIELDS):
        return entity_ids

    for state in hass.states.async_all("sensor"):
        object_id = state.entity_id.split(".", 1)[-1]
        if "power_sync" not in object_id:
            continue
        for field in _CALENDAR_STATISTIC_FIELDS:
            for suffix in _calendar_statistic_suffixes(field):
                if object_id.endswith(suffix):
                    entity_ids.setdefault(field, state.entity_id)
                    break

    return entity_ids


async def _calendar_time_series_from_statistics(
    hass: HomeAssistant,
    period: str,
    end_date: str | None,
    coordinator: Any,
    preferred_entry_id: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Build calendar history from HA recorder statistics for daily sensors."""
    range_result = _calendar_period_range(period, end_date)
    if not range_result:
        return [], "invalid_range"

    start_dt, end_dt = range_result
    now = dt_util.now()
    includes_today = _calendar_range_includes_today(start_dt, end_dt, now)
    statistic_end_dt = _calendar_statistics_end_dt(
        period,
        end_dt,
        now,
        includes_today,
    )

    entity_ids = _find_calendar_statistic_entity_ids(hass, preferred_entry_id)
    if not entity_ids:
        rows = (
            _calendar_time_series_from_energy_summary(coordinator)
            if includes_today
            else []
        )
        source = (
            "daily_energy_sensors_unavailable"
            if period in ("month", "year")
            else "live"
        )
        return rows, source

    time_series: dict[str, dict[str, Any]] = {}
    statistics_failed = False

    if statistic_end_dt > start_dt:
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            statistic_period = "hour" if period == "day" else "day"
            start_utc = dt_util.as_utc(start_dt)
            end_utc = dt_util.as_utc(statistic_end_dt)
            instance = get_instance(hass)
            stats = await instance.async_add_executor_job(
                statistics_during_period,
                hass,
                start_utc,
                end_utc,
                set(entity_ids.values()),
                statistic_period,
                None,
                {"change"},
            )

            entity_to_field = {
                entity_id: field for field, entity_id in entity_ids.items()
            }
            for entity_id, entries in (stats or {}).items():
                field = entity_to_field.get(entity_id)
                if not field:
                    continue
                for stat_entry in entries:
                    change = stat_entry.get("change")
                    if change is None:
                        continue
                    start = stat_entry.get("start")
                    if not isinstance(start, datetime):
                        continue
                    local_start = dt_util.as_local(start)
                    timestamp = local_start.isoformat()
                    row = time_series.setdefault(
                        timestamp,
                        {
                            "timestamp": timestamp,
                            "solar_generation": 0,
                            "battery_discharge": 0,
                            "battery_charge": 0,
                            "grid_import": 0,
                            "grid_export": 0,
                            "home_consumption": 0,
                        },
                    )
                    row[field] += max(0, float(change)) * 1000
        except Exception as exc:
            statistics_failed = True
            _LOGGER.debug("Failed to build calendar history from statistics: %s", exc)

    rows = [
        _calendar_entry_with_detail_aliases(time_series[key])
        for key in sorted(time_series)
    ]
    history_source = "long_term_statistics" if rows else "live"
    if not rows and statistic_end_dt > start_dt and period in ("day", "week"):
        rows = await _calendar_time_series_from_state_history(
            hass,
            period,
            start_dt,
            statistic_end_dt,
            entity_ids,
        )
        if rows:
            history_source = "state_history"
    elif not rows and statistic_end_dt > start_dt:
        history_source = (
            "long_term_statistics_error"
            if statistics_failed
            else "long_term_statistics_unavailable"
        )
        _LOGGER.warning(
            "Calendar history has no usable long-term statistics for "
            "period=%s; skipping the potentially expensive raw-history fallback",
            period,
        )
    if includes_today:
        current_entry = _calendar_current_entry(hass, coordinator, preferred_entry_id)
        if _calendar_entry_has_energy(current_entry):
            if period == "day" and rows:
                rows = _calendar_reconcile_current_day_rows(
                    rows,
                    current_entry,
                )
            else:
                rows.append(current_entry)

    return rows, history_source


def _calendar_current_optimizer_cost_summary(
    hass: HomeAssistant,
    preferred_entry_id: str | None,
) -> dict[str, Any] | None:
    """Return the live cost source used by the mobile Day summary."""
    domain_data = hass.data.get(DOMAIN, {})
    ordered_entry_data: list[dict[str, Any]] = []
    if preferred_entry_id:
        preferred_data = domain_data.get(preferred_entry_id)
        if isinstance(preferred_data, dict):
            ordered_entry_data.append(preferred_data)
    for entry_id, entry_data in domain_data.items():
        if entry_id == preferred_entry_id or not isinstance(entry_data, dict):
            continue
        ordered_entry_data.append(entry_data)

    for entry_data in ordered_entry_data:
        coordinator = entry_data.get("optimization_coordinator")
        api_data_getter = getattr(coordinator, "get_api_data", None)
        if not callable(api_data_getter):
            continue
        try:
            breakdown = (api_data_getter() or {}).get("daily_cost_breakdown")
            if not isinstance(breakdown, dict):
                continue
            import_cost = float(breakdown["actual_import_cost"])
            export_earnings = float(breakdown["actual_export_earnings"])
            net_cost = float(
                breakdown.get(
                    "actual_cost",
                    import_cost - export_earnings,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
        return {
            "import_cost": round(import_cost, 2),
            "export_earnings": round(export_earnings, 2),
            "net_cost": round(net_cost, 2),
            "estimated": True,
        }

    return None


async def _calendar_result_from_energy_summary(
    hass: HomeAssistant,
    period: str,
    end_date: str | None,
    coordinator: Any,
    entry_id: str | None = None,
    tariff_schedule: dict | None = None,
    source_system: str | None = None,
) -> dict[str, Any]:
    """Return calendar-history response data for energy-summary based systems."""
    time_series, history_source = await _calendar_time_series_from_statistics(
        hass,
        period,
        end_date,
        coordinator,
        entry_id,
    )
    if not time_series:
        range_result = _calendar_period_range(period, end_date)
        includes_today = bool(
            range_result
            and _calendar_range_includes_today(range_result[0], range_result[1])
        )
        time_series = (
            _calendar_time_series_from_energy_summary(coordinator, hass, entry_id)
            if includes_today
            else []
        )
    result = {
        "success": True,
        "period": period,
        "time_series": time_series,
        "serial_number": None,
        "installation_date": None,
        "history_source": history_source,
    }
    limited_sources = {
        "daily_energy_sensors_unavailable",
        "long_term_statistics_error",
        "long_term_statistics_unavailable",
    }
    if history_source in limited_sources:
        result["history_limited"] = True
        result["history_message"] = (
            "No Home Assistant long-term statistics are available for this "
            "period. Month and Year history requires Home Assistant to record "
            "long-term statistics for Tesla v1r's daily energy sensors."
        )

    if period == "day":
        cost_summary = await _calculate_cost_from_statistics(hass, period, end_date)
        if not cost_summary and tariff_schedule:
            cost_summary = _calculate_cost_from_tariff(
                tariff_schedule,
                time_series,
                period,
            )
    else:
        range_result = _calendar_period_range(period, end_date)
        now = dt_util.now()
        requested_end_is_today = not end_date or end_date == now.date().isoformat()
        current_period_contains_only_today = bool(
            requested_end_is_today
            and range_result
            and range_result[0].date() == now.date()
            and _calendar_range_includes_today(
                range_result[0],
                range_result[1],
                now,
            )
        )
        # Non-Tesla energy-summary systems expose daily-reset cost sensors. For
        # week/month/year those recorder statistics can be reset-skewed, so use
        # the same period energy rows returned to the mobile app. On the first
        # day of a live period, however, the period and day contain identical
        # energy. Reuse the mobile Day card's live optimizer cost source so
        # identical intervals cannot display different costs.
        if current_period_contains_only_today:
            cost_summary = _calendar_current_optimizer_cost_summary(
                hass,
                entry_id,
            )
            if not cost_summary:
                cost_summary = await _calculate_cost_from_statistics(
                    hass,
                    "day",
                    now.date().isoformat(),
                )
        else:
            cost_summary = (
                _calculate_cost_from_tariff(
                    tariff_schedule,
                    time_series,
                    period,
                )
                if tariff_schedule
                else await _calculate_cost_from_statistics(
                    hass,
                    period,
                    end_date,
                )
            )
    if not cost_summary and tariff_schedule:
        cost_summary = _calculate_cost_from_tariff(
            tariff_schedule,
            time_series,
            period,
        )
    if cost_summary:
        load_kwh = sum(e.get("home_consumption", 0) for e in time_series) / 1000
        if load_kwh > 0:
            cost_summary["avg_cost_per_kwh"] = round(
                (
                    (cost_summary.get("import_cost") or 0)
                    - (cost_summary.get("export_earnings") or 0)
                )
                / load_kwh,
                4,
            )
        result["cost_summary"] = cost_summary

    totals = _calendar_time_series_totals_kwh(time_series)
    _LOGGER.info(
        "Calendar history energy-summary response: source=%s period=%s "
        "end_date=%s rows=%d totals_kwh solar=%.3f battery_discharge=%.3f "
        "battery_charge=%.3f grid_import=%.3f grid_export=%.3f home=%.3f",
        source_system or "unknown",
        period,
        end_date,
        len(time_series),
        totals["solar_generation"],
        totals["battery_discharge"],
        totals["battery_charge"],
        totals["grid_import"],
        totals["grid_export"],
        totals["home_consumption"],
    )

    return result


def _find_first_tesla_v1r_entry(hass: HomeAssistant):
    for config_entry in hass.config_entries.async_entries(DOMAIN):
        return config_entry
    return None


def _get_tesla_coord_for_view(hass: HomeAssistant):
    """Return (entry, tesla_coordinator) or (entry, None) for HTTP views."""
    entry = _find_first_tesla_v1r_entry(hass)
    if not entry:
        return None, None
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    return entry, entry_data.get("tesla_coordinator")


MAX_REQUEST_BODY_BYTES = 64 * 1024  # 64 KB limit for API request bodies


async def _parse_json_request(
    request: web.Request, max_bytes: int = MAX_REQUEST_BODY_BYTES
) -> dict:
    """Parse JSON request body with size limit. Raises ValueError if too large or invalid."""
    content_length = request.content_length
    if content_length is not None and content_length > max_bytes:
        raise ValueError(
            f"Request body too large ({content_length} bytes, max {max_bytes})"
        )
    body_bytes = await request.read()
    if len(body_bytes) > max_bytes:
        raise ValueError(
            f"Request body too large ({len(body_bytes)} bytes, max {max_bytes})"
        )
    import json as _json

    return _json.loads(body_bytes)


async def fetch_tesla_tariff_schedule(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict | None:
    """Fetch tariff from Tesla site_info API and extract full TOU schedule.

    This is used for:
    1. TariffPriceView HTTP endpoint
    2. EV charging planner tariff forecast
    3. Non-Amber user initialization on startup

    Returns a dict with:
    - current_period: Current TOU period name
    - current_season: Current season name
    - buy_price: Current buy price in cents/kWh
    - sell_price: Current sell price in cents/kWh
    - buy_rates: Dict of period_name -> rate in $/kWh
    - sell_rates: Dict of period_name -> rate in $/kWh
    - tou_periods: Full TOU schedule for planning
    - seasons: Season definitions
    - utility: Utility name
    - plan_name: Plan name
    - last_sync: Timestamp
    """
    try:
        current_token, provider = get_tesla_api_token(hass, entry)
        site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

        if not site_id or not current_token:
            # Only Tesla-energy systems configure a site_id. On other battery
            # systems (Sungrow, FoxESS, Sigenergy, etc.) this Tesla tariff fetch
            # is not applicable, so don't flood WARNINGs every poll — log at
            # DEBUG. Keep a WARNING only when a site_id is set but the token is
            # missing, which is a genuine Tesla misconfiguration worth surfacing.
            if site_id:
                _LOGGER.warning("Missing Tesla token for tariff fetch")
            else:
                _LOGGER.debug(
                    "No Tesla energy site configured; skipping Tesla tariff fetch"
                )
            return None

        session = async_get_clientsession(hass)
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
        }
        api_base = get_tesla_api_base_url(
            provider, entry.data.get(CONF_FLEET_API_BASE_URL)
        )

        # Fetch site_info which contains tariff_content
        async with session.get(
            f"{api_base}/api/1/energy_sites/{site_id}/site_info",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                text = await response.text()
                _LOGGER.error(
                    f"Failed to get site_info for tariff: {response.status} - {text}"
                )
                return None

            data = await response.json()
            site_info = data.get("response", {})

        # Get tariff_content from site_info. Newer Tesla API responses may only
        # expose the v2 tariff shape, which the write/restore paths already use.
        tariff = _extract_tesla_tariff_content(site_info)
        if not tariff:
            _LOGGER.warning(
                "No tariff_content or tariff_content_v2 in Tesla site_info response"
            )
            return None

        _LOGGER.debug(
            f"Tesla tariff content utility: {tariff.get('utility')}, name: {tariff.get('name')}"
        )

        # Reject Tesla v1r-generated fake tariffs (force charge/discharge).
        # If HA restarts while a force mode tariff is on the Tesla API, we must
        # not use it as the real TOU schedule.
        if _is_tesla_v1r_force_tariff(tariff):
            _LOGGER.warning(
                "Tesla API returned a Tesla v1r force tariff (%s) — "
                "ignoring and falling back to custom tariff",
                _tariff_display_name(tariff),
            )
            return None

        # Determine current season and TOU period
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo

        # Get timezone from site_info
        tz_name = site_info.get("installation_time_zone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")
        now = dt.now(tz)

        # Find current season
        seasons = tariff.get("seasons", {})
        from .tariff_time import find_season_for_month, season_rate_maps

        current_season = find_season_for_month(seasons, now.month)

        _LOGGER.debug(
            "Current season: %s, local_time: %s", current_season, now.isoformat()
        )

        tou_periods = seasons.get(current_season, {}).get("tou_periods", {})
        from .tariff_time import find_matching_tou_period

        # Get energy charges for current season
        # tariff_content format: energy_charges.Summer.ON_PEAK = 0.48 (no 'rates' key)
        energy_charges = tariff.get("energy_charges", {})
        season_buy_rates = season_rate_maps(energy_charges)
        buy_rates = season_buy_rates.get(current_season, {})

        # Get sell tariff
        sell_tariff = tariff.get("sell_tariff", {})
        sell_energy_charges = sell_tariff.get("energy_charges", {})
        season_sell_rates = season_rate_maps(sell_energy_charges)
        sell_rates = season_sell_rates.get(current_season, {})
        current_period = find_matching_tou_period(
            tou_periods,
            now,
            default="ALL",
            buy_rates=buy_rates,
            sell_rates=sell_rates,
        )
        _LOGGER.info(f"Tesla TOU period: {current_period}")

        # Get current prices
        current_buy_price = buy_rates.get(current_period, buy_rates.get("ALL", 0))
        current_sell_price = sell_rates.get(current_period, sell_rates.get("ALL", 0))

        # Convert from $/kWh to c/kWh (multiply by 100)
        current_buy_cents = round(current_buy_price * 100, 2)
        current_sell_cents = round(current_sell_price * 100, 2)

        _LOGGER.info(
            f"Tesla tariff: Buy {current_buy_cents}c/kWh, Sell {current_sell_cents}c/kWh (period: {current_period})"
        )

        # Log TOU periods for debugging
        if tou_periods:
            period_summary = []
            for period_name, periods in tou_periods.items():
                if isinstance(periods, list) and periods:
                    first = periods[0]
                    period_summary.append(
                        f"{period_name}: {first.get('fromHour', 0)}-{first.get('toHour', 24)}"
                    )
            _LOGGER.info(f"Tesla TOU periods: {', '.join(period_summary)}")

            # Log rates for each period
            for period_name in tou_periods:
                rate = buy_rates.get(period_name, "N/A")
                if isinstance(rate, (int, float)):
                    _LOGGER.info(f"  {period_name}: {rate * 100:.1f}c/kWh")

        tariff_result = {
            "current_period": current_period,
            "current_season": current_season,
            "buy_price": current_buy_cents,
            "sell_price": current_sell_cents,
            "buy_rates": buy_rates,
            "sell_rates": sell_rates,
            "season_buy_rates": season_buy_rates,
            "season_sell_rates": season_sell_rates,
            "tou_periods": tou_periods,  # Include full TOU schedule for planning
            "seasons": seasons,  # Include season definitions
            "utility": tariff.get("utility", "Unknown"),
            "plan_name": tariff.get("name", "Unknown"),
            **presentation_currency_metadata_for_entry(
                entry,
                tariff.get("currency") or currency_for_entry(entry, hass),
            ),
            "last_sync": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # Store for future use
        if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
            entry_data = hass.data[DOMAIN][entry.entry_id]
            entry_data["tariff_schedule"] = tariff_result
            entry_data["last_restorable_tesla_tariff"] = tariff
            store = entry_data.get("store")
            if store:
                try:
                    stored_data = await store.async_load() or {}
                    stored_data["last_restorable_tesla_tariff"] = tariff
                    await store.async_save(stored_data)
                except Exception as store_err:
                    _LOGGER.debug(
                        "Could not persist last restorable Tesla tariff: %s", store_err
                    )
            _LOGGER.info(
                f"✅ Tesla tariff schedule stored with {len(tou_periods)} TOU periods"
            )

        return tariff_result

    except Exception as e:
        _LOGGER.error(f"Error fetching Tesla tariff: {e}", exc_info=True)
        return None


def convert_custom_tariff_to_schedule(
    custom_tariff: dict,
    currency: str | None = None,
) -> dict:
    """Convert custom_tariff format to tariff_schedule format.

    This converts the user-configured custom tariff (Tesla tariff_content format)
    to the internal tariff_schedule format used by the EV charging planner.

    Args:
        custom_tariff: Custom tariff configuration from automation_store
        currency: Optional ISO 4217 currency override for legacy saved tariffs

    Returns:
        tariff_schedule dict with: current_period, current_season, buy_price, sell_price,
        buy_rates, sell_rates, tou_periods, seasons, utility, plan_name, last_sync
    """
    from .tariff_time import (
        find_matching_tou_period,
        find_season_for_month,
        season_rate_maps,
    )

    try:
        tariff_currency = normalize_currency(
            currency or custom_tariff.get("currency"),
            DEFAULT_CURRENCY,
        )
        # Get current time for determining current period — HA tz, not container UTC
        now = dt_util.now()

        # Extract seasons from custom tariff
        seasons = custom_tariff.get("seasons", {})

        # Find current season
        current_season = find_season_for_month(seasons, now.month)

        _LOGGER.debug(
            "Custom tariff - current season=%s, local_time=%s",
            current_season,
            now.isoformat(),
        )

        # Get TOU periods for current season
        tou_periods = seasons.get(current_season, {}).get("tou_periods", {})

        # Get energy charges
        energy_charges = custom_tariff.get("energy_charges", {})
        season_buy_rates = season_rate_maps(energy_charges)
        buy_rates = season_buy_rates.get(current_season, {})

        # Get sell tariff / feed-in tariff
        sell_tariff = custom_tariff.get("sell_tariff", {})
        sell_energy_charges = sell_tariff.get("energy_charges", {})
        season_sell_rates = season_rate_maps(sell_energy_charges)
        sell_rates = season_sell_rates.get(current_season, {})
        current_period = find_matching_tou_period(
            tou_periods,
            now,
            default="OFF_PEAK",
            buy_rates=buy_rates,
            sell_rates=sell_rates,
        )

        _LOGGER.debug(f"Custom tariff - Current TOU period: {current_period}")

        # Get current prices
        current_buy_price = buy_rates.get(
            current_period, buy_rates.get("ALL", buy_rates.get("OFF_PEAK", 0))
        )
        current_sell_price = sell_rates.get(current_period, sell_rates.get("ALL", 0))

        # Convert from $/kWh to c/kWh
        current_buy_cents = round(current_buy_price * 100, 2)
        current_sell_cents = round(current_sell_price * 100, 2)

        _LOGGER.info(
            f"Custom tariff: Buy {current_buy_cents}c/kWh, Sell {current_sell_cents}c/kWh (period: {current_period})"
        )

        return {
            "current_period": current_period,
            "current_season": current_season,
            "buy_price": current_buy_cents,
            "sell_price": current_sell_cents,
            "buy_rates": buy_rates,
            "sell_rates": sell_rates,
            "season_buy_rates": season_buy_rates,
            "season_sell_rates": season_sell_rates,
            "tou_periods": tou_periods,
            "seasons": seasons,
            "utility": custom_tariff.get("utility", "Custom"),
            "plan_name": custom_tariff.get("name", "Custom Tariff"),
            "currency": tariff_currency,
            **currency_metadata(tariff_currency),
            "last_sync": dt_util.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_custom": True,  # Flag to indicate this is a custom tariff
        }

    except Exception as e:
        _LOGGER.error(f"Error converting custom tariff to schedule: {e}", exc_info=True)
        return {}


def _bootstrap_static_tariff_schedule(
    hass: HomeAssistant,
    entry: ConfigEntry,
    automation_store: Any,
    electricity_provider: str,
) -> dict | None:
    """Expose a persisted static tariff before the first energy refresh.

    Existing entries keep their custom tariff in ``AutomationStore``. Energy
    coordinators can refresh before the full setup dictionary is assembled, so
    put the converted schedule in temporary entry data now rather than letting
    the first measured intervals become deliberately unpriced.
    """
    if electricity_provider not in {
        "agl",
        "globird",
        "aemo_vpp",
        "other",
        "tou_only",
        "nz",
    }:
        return None

    try:
        custom_tariff = automation_store.get_custom_tariff()
    except Exception as err:
        _LOGGER.debug("Could not load persisted custom tariff during startup: %s", err)
        return None
    if not isinstance(custom_tariff, dict) or not custom_tariff:
        custom_tariff = entry.data.get("initial_custom_tariff")
    if not isinstance(custom_tariff, dict) or not custom_tariff:
        return None

    tariff_schedule = convert_custom_tariff_to_schedule(
        custom_tariff,
        currency=currency_for_entry(entry, hass),
    )
    if not tariff_schedule:
        return None

    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data["tariff_schedule"] = tariff_schedule
    _LOGGER.info(
        "Restored persisted custom tariff before first energy refresh for %s: %s",
        electricity_provider,
        custom_tariff.get("name", "Custom Tariff"),
    )
    return tariff_schedule


def get_current_price_from_tariff_schedule(
    tariff_schedule: dict,
) -> tuple[float, float, str]:
    """Calculate current buy/sell price from tariff_schedule TOU periods.

    This recalculates the current TOU period and price in real-time based on
    the stored TOU periods, ensuring prices update when periods change.

    Args:
        tariff_schedule: Tariff schedule dict with tou_periods, buy_rates, sell_rates

    Returns:
        Tuple of (buy_price_cents, sell_price_cents, current_period)
    """
    from .tariff_time import (
        find_matching_tou_period,
        tariff_components_for_datetime,
    )

    try:
        now = (
            dt_util.now()
        )  # HA-configured timezone — naive datetime.now() returns UTC in containers
        current_hour = now.hour

        # Re-select the season for every call so long-running installations do
        # not keep the tariff that was active when the integration loaded.
        tou_periods, buy_rates, sell_rates, _ = tariff_components_for_datetime(
            tariff_schedule,
            now,
        )

        # If no TOU periods, try PERIOD_HH_MM key format or fall back to cached
        if not tou_periods:
            buy_prices = tariff_schedule.get("buy_prices", {})
            sell_prices = tariff_schedule.get("sell_prices", {})
            if buy_prices:
                # buy_prices / sell_prices are stored in $/kWh (Tesla tariff format).
                # Convert to c/kWh so the return value matches the documented contract.
                current_min = now.minute
                slot_min = 0 if current_min < 30 else 30
                period_key = f"PERIOD_{current_hour:02d}_{slot_min:02d}"
                buy_val = buy_prices.get(period_key)
                sell_val = sell_prices.get(period_key, 0)
                if buy_val is not None:
                    return (
                        buy_val * 100,
                        sell_val * 100,
                        f"{current_hour:02d}:{slot_min:02d}",
                    )
            # Fall back to cached prices
            return (
                tariff_schedule.get("buy_price", 25.0),
                tariff_schedule.get("sell_price", 8.0),
                tariff_schedule.get("current_period", "UNKNOWN"),
            )

        current_period = find_matching_tou_period(
            tou_periods,
            now,
            default="OFF_PEAK",
            buy_rates=buy_rates,
            sell_rates=sell_rates,
        )

        # Get prices for current period (rates are in $/kWh, convert to cents)
        # Note: buy_rates may already be in cents if from custom tariff, or $/kWh if from Tesla
        # When the matched period isn't in buy_rates (e.g. GloBird gaps at 14-17, 21-24),
        # try common fallback period names, then use the median of available rates.
        buy_rate = buy_rates.get(current_period)
        if buy_rate is None:
            buy_rate = buy_rates.get("ALL")
        if buy_rate is None:
            for fb in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                if fb in buy_rates:
                    buy_rate = buy_rates[fb]
                    break
        if buy_rate is None:
            defined = sorted(
                v for v in buy_rates.values() if isinstance(v, (int, float))
            )
            buy_rate = defined[len(defined) // 2] if defined else 0.25

        sell_rate = sell_rates.get(current_period)
        if sell_rate is None:
            sell_rate = sell_rates.get("ALL")
        if sell_rate is None:
            for fb in ("OFF_PEAK", "PARTIAL_PEAK", "SHOULDER"):
                if fb in sell_rates:
                    sell_rate = sell_rates[fb]
                    break
        if sell_rate is None:
            defined = sorted(
                v for v in sell_rates.values() if isinstance(v, (int, float))
            )
            sell_rate = defined[len(defined) // 2] if defined else 0.08

        # Rates from Tesla tariff_content are always in $/kWh.
        # The old heuristic (< 1.0 = $/kWh, >= 1.0 = cents) broke for
        # high tariff rates like $2/kWh or $5/kWh (treated as 2c or 5c).
        # Since all paths into this function store rates in $/kWh,
        # always convert to cents.
        buy_price_cents = round(buy_rate * 100, 2)
        sell_price_cents = round(sell_rate * 100, 2)

        return (buy_price_cents, sell_price_cents, current_period)

    except Exception as e:
        _LOGGER.debug(f"Error calculating price from TOU periods: {e}")
        # Fallback to cached prices
        return (
            tariff_schedule.get("buy_price", 25.0),
            tariff_schedule.get("sell_price", 8.0),
            "UNKNOWN",
        )


def get_current_prices_for_curtailment(
    entry_data: dict[str, Any] | None,
    price_coordinators: tuple[Any, ...] = (),
) -> tuple[float | None, float | None, str | None]:
    """Return Amber-style feed-in and import prices in c/kWh for curtailment."""

    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _entry_provider(data: dict[str, Any] | None) -> str | None:
        if not isinstance(data, dict):
            return None
        entry = data.get("entry")
        entry_options = getattr(entry, "options", {}) or {}
        entry_data_inner = getattr(entry, "data", {}) or {}
        return (
            entry_options.get("electricity_provider")
            or entry_data_inner.get("electricity_provider")
            or data.get("electricity_provider")
        )

    def _prices_from_tariff_schedule() -> tuple[float | None, float | None, str | None]:
        tariff_schedule = (entry_data or {}).get("tariff_schedule")
        if isinstance(tariff_schedule, dict) and tariff_schedule:
            buy_cents, sell_cents, _ = get_current_price_from_tariff_schedule(
                tariff_schedule
            )
            buy_price = _as_float(buy_cents)
            sell_price = _as_float(sell_cents)
            schedule_import_price = buy_price if buy_price is not None else None
            if sell_price is not None:
                # Curtailment uses Amber's feedIn convention:
                # negative means the customer earns money for export.
                return -sell_price, schedule_import_price, "tariff_schedule"
            return None, schedule_import_price, None
        return None, None, None

    # Flow Power's AEMO coordinator exposes raw wholesale feed-in prices, while
    # actual export credit is encoded in the generated tariff schedule.
    if _entry_provider(entry_data) == "flow_power":
        feedin_price, import_price, price_source = _prices_from_tariff_schedule()
        if feedin_price is not None:
            return feedin_price, import_price, price_source

    import_price: float | None = None
    for price_coord in price_coordinators:
        data = getattr(price_coord, "data", None)
        if not isinstance(data, dict):
            continue

        feedin_price: float | None = None
        current_prices = data.get("current", [])
        if not isinstance(current_prices, list):
            continue

        for price_data in current_prices:
            if not isinstance(price_data, dict):
                continue
            price = _as_float(price_data.get("perKwh"))
            if price is None:
                continue
            channel = price_data.get("channelType")
            if channel == "feedIn":
                feedin_price = price
            elif channel == "general":
                import_price = price

        if feedin_price is not None:
            return feedin_price, import_price, "price_coordinator"

    feedin_price, schedule_import_price, price_source = _prices_from_tariff_schedule()
    if schedule_import_price is not None:
        import_price = schedule_import_price
    if feedin_price is not None:
        return feedin_price, import_price, price_source

    return None, import_price, None


def should_fetch_tesla_tariff_on_startup(
    electricity_provider: str,
    has_tesla_site: bool,
    token_getter: Any,
) -> bool:
    """Return true when startup should fetch a Tesla-app tariff schedule."""
    return (
        electricity_provider in ("globird", "aemo_vpp")
        and has_tesla_site
        and token_getter is not None
    )


_AUTOMATION_TRIGGER_TYPES = frozenset(
    {
        "time",
        "battery",
        "flow",
        "grid_import_energy",
        "grid_export_energy",
        "price",
        "grid",
        "weather",
        "solar_forecast",
        "ev",
        "ocpp",
    }
)


def _automation_trigger_validation_error(trigger: Any) -> str | None:
    """Return a user-facing validation error for an automation trigger."""
    if not isinstance(trigger, dict):
        return "Automation trigger must be an object"
    trigger_type = trigger.get("trigger_type")
    if trigger_type not in _AUTOMATION_TRIGGER_TYPES:
        return f"Unsupported automation trigger type: {trigger_type}"
    if trigger_type not in {"grid_import_energy", "grid_export_energy"}:
        return None

    direction = "import" if trigger_type == "grid_import_energy" else "export"
    threshold_key = f"grid_{direction}_energy_threshold_kwh"
    try:
        threshold = float(trigger.get(threshold_key))
    except (TypeError, ValueError):
        threshold = None
    if threshold is None or not math.isfinite(threshold) or threshold <= 0:
        return f"Grid {direction} energy threshold must be greater than 0 kWh"

    for key in ("time_window_start", "time_window_end"):
        value = trigger.get(key)
        if not isinstance(value, str):
            return f"Grid {direction} energy trigger requires a valid time window"
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            return f"Grid {direction} energy trigger requires HH:MM window times"
    return None


_last_api_error_notification: dict[str, float] = {}
_API_ERROR_COOLDOWN_SECONDS = 300  # 5 minutes between same notification


async def _notify_api_error(hass, title: str, message: str) -> None:
    """Send push notification for API errors with cooldown to prevent spam.

    Tesla's Fleet API can return 504 Gateway Timeout in bursts (e.g. at the
    top of each hour). Without cooldown, the user gets multiple identical
    notifications within seconds. This deduplicates by title — same error
    title is suppressed for 5 minutes after the first notification.
    """
    import time

    now = time.time()
    last_sent = _last_api_error_notification.get(title, 0)
    if now - last_sent < _API_ERROR_COOLDOWN_SECONDS:
        _LOGGER.debug(
            "Suppressing duplicate notification '%s' (cooldown %ds remaining)",
            title,
            int(_API_ERROR_COOLDOWN_SECONDS - (now - last_sent)),
        )
        return

    try:
        from .automations.actions import _send_expo_push

        await _send_expo_push(hass, f"⚠️ {title}", message)
        _last_api_error_notification[title] = now
    except Exception:
        pass  # Don't let notification failures cascade


def _preload_powerwall_local_modules() -> None:
    """Import protobuf C extension off the event loop.

    google.protobuf loads a native C extension (google._upb._message) on first
    import via importlib.import_module, which blocks the HA event loop and
    triggers a WARNING. Importing the transport module here (called via
    hass.async_add_executor_job) populates sys.modules before the async setup
    chain needs them, so subsequent imports in the event loop are no-ops.
    """
    from .powerwall_local import transport  # noqa: F401


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry,
) -> bool:
    """Allow removal of legacy standalone Powerwall pack devices."""
    legacy_prefix = f"{config_entry.entry_id}_pw_"
    return any(
        domain == DOMAIN and str(identifier).startswith(legacy_prefix)
        for domain, identifier in (getattr(device_entry, "identifiers", set()) or set())
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tesla v1r from a config entry."""
    _LOGGER.info("=" * 60)
    _LOGGER.info("Tesla v1r integration loading...")
    _LOGGER.info("Domain: %s", DOMAIN)
    _LOGGER.info("Entry ID: %s", entry.entry_id)
    _LOGGER.info("Entry state: %s", entry.state)
    _LOGGER.info("=" * 60)

    def _entry_value(key: str, default: Any = None) -> Any:
        """Read config entry options with data fallback."""
        return entry.options.get(key, entry.data.get(key, default))

    battery_connection_profile = resolve_connection_profile(
        entry.data,
        entry.options,
    )
    _LOGGER.info(
        "Battery connection profile: %s (%s)",
        battery_connection_profile.profile_id,
        battery_connection_profile.route_kind,
    )

    # Every setup gets a private generation token.  Dispatch callbacks can be
    # queued from another thread, so entry_id alone is not enough to tell an
    # old callback from one belonging to a freshly reloaded entry.
    aemo_dispatch_generation = object()

    def _aemo_dispatch_entry_data() -> dict[str, Any] | None:
        """Return this setup generation's live entry data, or ``None``.

        Callbacks can outlive an unload and see a new dictionary for the same
        entry_id after reload.  Check both the dictionary's generation token
        and its stopping flag before callback-owned work can recreate, mutate,
        or command through entry state.
        """
        current = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not isinstance(current, dict):
            return None
        if current.get("aemo_dispatch_generation") is not aemo_dispatch_generation:
            return None
        if current.get("aemo_dispatch_stopping", False):
            return None
        return current

    # Register the parent (hub) device.
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or "Tesla v1r",
        manufacturer="Tesla v1r",
        model="Hub",
    )

    def _is_monitoring_mode() -> bool:
        """Check if monitoring mode is active (blocks all control commands)."""
        if battery_connection_profile.monitoring_only:
            return True
        return entry.options.get(
            CONF_MONITORING_MODE,
            entry.data.get(CONF_MONITORING_MODE, False),
        )

    def _monitoring_mode_allows_curtailment(
        curtailment_enabled: bool,
    ) -> bool:
        """Return whether an enabled automatic curtailment route may write.

        Monitoring Mode remains the global source of truth for all other
        hardware commands.  Curtailment is the one explicitly opt-in,
        narrowly scoped exception because its export-limit action is
        independent of battery dispatch.  A reload handoff remains a hard
        fence: the old setup must not issue a command while its replacement is
        being prepared.
        """
        if not _is_monitoring_mode():
            return True
        if not curtailment_enabled:
            return False
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        if isinstance(entry_data, dict) and entry_data.get(
            "_monitoring_handoff_active", False
        ):
            return False
        return bool(
            _entry_value(
                CONF_CURTAILMENT_CONTROL_IN_MONITORING_MODE,
                DEFAULT_CURTAILMENT_CONTROL_IN_MONITORING_MODE,
            )
        )

    def _control_call_source(call: ServiceCall) -> str:
        """Return the normalized source for a battery control service call."""
        source = str(call.data.get("source", "")).lower()
        if source:
            return source
        if getattr(call.context, "user_id", None):
            return "user"
        return "unknown"

    def _monitoring_mode_should_block_control(call: ServiceCall) -> bool:
        """Return True when monitoring mode should block this control call."""
        if battery_connection_profile.monitoring_only:
            return True
        source = _control_call_source(call)
        return _is_monitoring_mode() and source not in ("user", "manual")

    def _optimizer_current_force_action_matches(force_type: str) -> bool:
        """Return True when the optimizer is actively asking for a force mode."""
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        opt_coordinator = entry_data.get("optimization_coordinator")
        if not opt_coordinator or not getattr(opt_coordinator, "_enabled", False):
            return False

        current_actions: set[str] = set()
        opt_data = getattr(opt_coordinator, "data", None)
        if isinstance(opt_data, dict):
            for key in (
                "effective_current_action",
                "current_action",
                "planned_current_action",
            ):
                value = opt_data.get(key)
                if value:
                    current_actions.add(str(value).strip().lower())

        get_current_action = getattr(opt_coordinator, "_get_current_action", None)
        if callable(get_current_action):
            try:
                action = get_current_action()
            except Exception as err:
                _LOGGER.debug("Could not inspect optimizer current action: %s", err)
            else:
                action_name = getattr(action, "action", None)
                if action_name:
                    current_actions.add(str(action_name).strip().lower())

        if force_type == "charge":
            return "charge" in current_actions
        if force_type == "discharge":
            return bool(current_actions.intersection(("discharge", "export")))
        return False

    async def _clear_force_timer_state_without_restore(
        force_type: str, reason: str
    ) -> None:
        """Clear an expired manual force timer without changing hardware state."""
        if force_type == "charge":
            state = force_charge_state
            signal = f"{DOMAIN}_force_charge_state"
        else:
            state = force_discharge_state
            signal = f"{DOMAIN}_force_discharge_state"

        _LOGGER.info("%s; clearing manual timer state without restore_normal", reason)
        state["active"] = False
        state["expires_at"] = None
        state["hardware_expires_at"] = None
        state["cancel_expiry_timer"] = None
        state["duration"] = 0
        state["saved_tariff"] = None
        state["saved_operation_mode"] = None
        state["saved_backup_reserve"] = None
        state["saved_grid_charging_enabled"] = None
        if force_type == "discharge":
            state["saved_export_rule"] = None

        async_dispatcher_send(
            hass,
            signal,
            {
                "active": False,
                "expires_at": None,
                "duration": 0,
            },
        )
        await persist_force_mode_state()

    # Get initial Tesla API token and provider
    # Use get_tesla_api_token() which fetches fresh from tesla_fleet if available
    tesla_api_token, tesla_api_provider = get_tesla_api_token(hass, entry)

    if not tesla_api_token:
        _LOGGER.error("No Tesla API credentials available")
        raise ConfigEntryNotReady("No Tesla API credentials configured")

    if tesla_api_provider == TESLA_PROVIDER_FLEET_API:
        _LOGGER.info(
            "Detected Tesla Fleet integration - using Fleet API tokens for site %s",
            entry.data[CONF_TESLA_ENERGY_SITE_ID],
        )

    # Create token getter that always fetches fresh token (handles token refresh)
    # This is called before each API request to ensure we use the latest token
    def token_getter():
        return get_tesla_api_token(hass, entry)

    tesla_coordinator = TeslaEnergyCoordinator(
        hass,
        entry.data[CONF_TESLA_ENERGY_SITE_ID],
        tesla_api_token,
        api_provider=tesla_api_provider,
        token_getter=token_getter,
        entry_id=entry.entry_id,
        fleet_base_url=entry.data.get(CONF_FLEET_API_BASE_URL),
    )

    # Warm the local Powerwall coordinator before Tesla's first cloud refresh.
    # If Tesla live_status returns an empty response during startup, the Tesla
    # coordinator can still publish local LAN telemetry instead of making the
    # config entry unavailable.
    if (
        tesla_coordinator
        and entry.data.get(CONF_POWERWALL_LOCAL_PAIRED)
        and battery_connection_profile.profile_id != "tesla_powerwall_monitoring"
    ):
        try:
            await hass.async_add_executor_job(_preload_powerwall_local_modules)
            from .powerwall_local.views import (
                ensure_coordinator as _ensure_pwlocal_coordinator,
            )

            await _ensure_pwlocal_coordinator(hass, entry)
        except Exception as _err:
            _LOGGER.debug(
                "Powerwall local coordinator early warmup skipped before Tesla refresh: %s",
                _err,
            )

    # Fetch initial data
    if tesla_coordinator:
        if battery_connection_profile.profile_id == "tesla_powerwall_monitoring":
            try:
                await tesla_coordinator.async_config_entry_first_refresh()
            except Exception as err:
                _LOGGER.warning(
                    "Tesla Powerwall integration entities are not ready yet; "
                    "keeping monitoring coordinator active so it can retry: %s",
                    err,
                )
        else:
            await tesla_coordinator.async_config_entry_first_refresh()

    # Initialize persistent storage for data that survives HA restarts
    # (like Teslemetry's RestoreEntity pattern for export rule state)
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
    stored_data = await store.async_load() or {}
    stored_grid_charging_preferences = stored_data.get(
        "tesla_grid_charging_preferences",
        {},
    )
    tesla_grid_charging_preferences = {
        str(site_id): value
        for site_id, value in (
            stored_grid_charging_preferences.items()
            if isinstance(stored_grid_charging_preferences, dict)
            else ()
        )
        if isinstance(value, bool)
    }
    cached_export_rule = stored_data.get("cached_export_rule")
    if cached_export_rule:
        _LOGGER.info(
            f"Restored cached_export_rule='{cached_export_rule}' from persistent storage"
        )

    # Restore manual export override (set via PS app Grid Export toggle)
    stored_manual_export_override = stored_data.get("manual_export_override", False)
    stored_manual_export_rule = stored_data.get("manual_export_rule")
    if stored_manual_export_override:
        _LOGGER.info(
            "Restored manual_export_override=True (rule='%s') from persistent storage",
            stored_manual_export_rule,
        )

    # Restore battery health data from storage
    battery_health = stored_data.get("battery_health")
    if battery_health:
        _LOGGER.info(
            f"Restored battery health from storage: {battery_health.get('degradation_percent')}% degradation"
        )

    # Restore force charge/discharge state from storage (survives HA restarts)
    force_mode_state = stored_data.get("force_mode_state")
    if force_mode_state:
        _LOGGER.info(f"Found persisted force mode state: {force_mode_state}")
    pending_tesla_restore = stored_data.get("pending_tesla_restore")
    if pending_tesla_restore:
        _LOGGER.warning(
            "Found an unfinished Tesla restore from a previous run: %s",
            pending_tesla_restore.get("reason"),
        )

    last_restorable_tesla_tariff = _select_restorable_tesla_tariff(
        stored_data.get("last_restorable_tesla_tariff")
    )
    if last_restorable_tesla_tariff:
        _LOGGER.info(
            "Restored cached Tesla tariff baseline from storage (name: %s)",
            _tariff_display_name(last_restorable_tesla_tariff),
        )

    # Store coordinators and WebSocket client in hass.data. The Tesla
    # capability probe can publish early while the first refresh is running, so
    # preserve those values when replacing the setup entry with the full data.
    existing_entry_data = hass.data.setdefault(DOMAIN, {}).get(entry.entry_id, {})
    powerwall_local_runtime = existing_entry_data.get("powerwall_local")
    startup_tariff_schedule = existing_entry_data.get("tariff_schedule")
    tesla_capabilities = existing_entry_data.get("tesla_capabilities")
    if tesla_capabilities is None and tesla_coordinator:
        tesla_capabilities = dict(
            getattr(tesla_coordinator, "tesla_capabilities", {}) or {}
        )
    tesla_site_country = existing_entry_data.get("tesla_site_country")
    if tesla_site_country is None and tesla_coordinator:
        tesla_site_country = getattr(tesla_coordinator, "_site_country", None)
    hass.data[DOMAIN][entry.entry_id] = {
        "tesla_coordinator": tesla_coordinator,
        "tesla_capabilities": tesla_capabilities or {},
        "tesla_site_country": tesla_site_country,
        "tesla_grid_charging_preferences": tesla_grid_charging_preferences,
        "battery_connection_profile": battery_connection_profile,
        "powerwall_local": powerwall_local_runtime
        or {"client": None, "coordinator": None, "pairing_manager": None},
        "entry": entry,
        "tariff_schedule": startup_tariff_schedule,
        "demand_allow_grid_charging": entry.options.get(
            CONF_DEMAND_ALLOW_GRID_CHARGING,
            entry.data.get(CONF_DEMAND_ALLOW_GRID_CHARGING, False),
        ),  # Allow grid charging during demand peak periods
        "battery_health": battery_health,  # Restored from persistent storage (from mobile app TEDAPI scans)
        "powerwall_bms_health_poll_cancel": None,  # 5-minute pack energy/BMS refresh timer
        "powerwall_solar_strings_poll_cancel": None,  # 30-second PW2/PW3 DC string voltage refresh timer
        "force_mode_state": force_mode_state,  # Restored force charge/discharge state
        "pending_tesla_restore": pending_tesla_restore,  # Unfinished restore to complete at startup
        "last_restorable_tesla_tariff": last_restorable_tesla_tariff,
        "store": store,  # Reference to Store for saving updates
        "token_getter": token_getter,  # Function to get fresh Tesla API token
        "saving_session_cancel": None,  # Will store the session check cancel function
        "calibration_suspected": False,
        "calibration_detected_at": None,
        "calibration_source": None,
        "calibration_sources": [],
        "_calibration_sources": [],
        "_calibration_alert_clear_polls": 0,
        "_mode_stick_failures": [],  # list of timestamps for calibration detection
        "_calibration_check_unsub": None,
    }

    def _network_static_export_limit_w() -> float | None:
        """Return the configured static site cap for envelope normalization."""
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        optimization = entry_data.get("optimization_coordinator")
        configured = getattr(
            getattr(optimization, "_config", None), "max_grid_export_w", None
        )
        if configured is None:
            configured = entry.options.get(
                CONF_OPTIMIZATION_MAX_GRID_EXPORT_W,
                entry.data.get(CONF_OPTIMIZATION_MAX_GRID_EXPORT_W),
            )
        try:
            value = float(configured)
        except (TypeError, ValueError):
            return None
        return max(0.0, value)

    # Build the local Powerwall coordinator before entities are created. Tesla
    # energy sensors attach a second listener to this coordinator so paired
    # installs update from LAN telemetry instead of waiting for cloud samples.
    if (
        entry.data.get(CONF_POWERWALL_LOCAL_PAIRED)
        and battery_connection_profile.profile_id != "tesla_powerwall_monitoring"
    ):
        try:
            await hass.async_add_executor_job(_preload_powerwall_local_modules)
            from .powerwall_local.views import (
                ensure_coordinator as _ensure_pwlocal_coordinator,
            )

            await _ensure_pwlocal_coordinator(hass, entry)
        except Exception as _err:
            _LOGGER.debug(
                "Powerwall local coordinator early warmup skipped: %s",
                _err,
            )

    from .auto_update import async_setup_auto_update

    hass.data[DOMAIN][entry.entry_id][
        "auto_update_cancel"
    ] = await async_setup_auto_update(
        hass,
        entry,
    )

    # Track firmware version for change notifications (Tesla only)
    if tesla_coordinator:
        last_known_firmware = stored_data.get("last_known_firmware")

        async def _check_firmware_change():
            """Check if firmware has changed and notify."""
            nonlocal last_known_firmware
            data = tesla_coordinator.data
            if not data:
                return
            current_fw = data.get("firmware")
            if not current_fw:
                return
            if last_known_firmware and current_fw != last_known_firmware:
                _LOGGER.info(
                    "Firmware update detected: %s -> %s",
                    last_known_firmware,
                    current_fw,
                )
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        hass, "Powerwall Update", f"Firmware updated: {current_fw}"
                    )
                except Exception:
                    pass
            if current_fw != last_known_firmware:
                last_known_firmware = current_fw
                sd = await store.async_load() or {}
                sd["last_known_firmware"] = current_fw
                await store.async_save(sd)

        def _on_coordinator_update():
            """Listener for coordinator data updates."""
            hass.async_create_task(_check_firmware_change())

        tesla_coordinator.async_add_listener(_on_coordinator_update)

    # Helper function to update and persist cached export rule
    async def update_cached_export_rule(new_rule: str) -> None:
        """Update the cached export rule in memory and persist to storage."""
        hass.data[DOMAIN][entry.entry_id]["cached_export_rule"] = new_rule
        try:
            store = hass.data[DOMAIN][entry.entry_id]["store"]
            # Preserve other stored data (like battery_health)
            stored_data = await store.async_load() or {}
            stored_data["cached_export_rule"] = new_rule
            await store.async_save(stored_data)
            _LOGGER.debug(f"Persisted cached_export_rule='{new_rule}' to storage")
        except Exception as err:
            _LOGGER.warning(
                f"Could not persist cached_export_rule='{new_rule}' to storage: {err}"
            )
        # Signal sensor to update
        async_dispatcher_send(hass, f"power_sync_curtailment_updated_{entry.entry_id}")

    async def refresh_powerwall_local_after_settings_write(label: str) -> None:
        """Refresh local Powerwall settings readback after a successful write."""
        try:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            local_coord = (entry_data.get("powerwall_local") or {}).get("coordinator")
            refresh = getattr(local_coord, "async_request_refresh", None)
            if refresh:
                await refresh()
        except Exception as err:
            _LOGGER.debug(
                "Powerwall local readback refresh after %s failed: %s",
                label,
                err,
            )

    def _get_cached_live_status() -> dict | None:
        """Get live status from the active site coordinator when available."""

        try:
            from .automations.live_status import coordinator_data_to_ev_live_status

            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            for coord_key in ("tesla_coordinator",):
                coordinator = entry_data.get(coord_key)
                data = getattr(coordinator, "data", None)
                if not data:
                    continue
                live_status = coordinator_data_to_ev_live_status(data)
                inverter_last_state = entry_data.get("inverter_last_state")
                if inverter_last_state == "curtailed":
                    live_status["is_curtailed"] = True
                elif inverter_last_state in ("normal", "running"):
                    live_status["is_curtailed"] = False
                _LOGGER.debug("Live status from %s", coord_key)
                return live_status
        except Exception as e:
            _LOGGER.debug("Error getting cached coordinator live status: %s", e)

        return None

    # Helper function to get live status from the active coordinator or Tesla API
    async def get_live_status() -> dict | None:
        """Get current live status from coordinator data or Tesla API.

        Returns:
            Dict with battery_soc, grid_power, solar_power, etc. or None if unavailable
            grid_power: Negative = exporting to grid, Positive = importing from grid
        """
        cached_status = _get_cached_live_status()
        if cached_status:
            return cached_status

        if not callable(token_getter):
            _LOGGER.debug("No Tesla API token getter available for live status check")
            return None

        try:
            current_token, current_provider = token_getter()
            if not current_token:
                _LOGGER.debug("No Tesla API token available for live status check")
                return None

            session = async_get_clientsession(hass)
            api_base_url = get_tesla_api_base_url(
                current_provider, entry.data.get(CONF_FLEET_API_BASE_URL)
            )
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }

            async with session.get(
                f"{api_base_url}/api/1/energy_sites/{entry.data[CONF_TESLA_ENERGY_SITE_ID]}/live_status",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    site_status = data.get("response", {})
                    result = {
                        "battery_soc": site_status.get("percentage_charged"),
                        "grid_power": site_status.get(
                            "grid_power"
                        ),  # Negative = exporting
                        "solar_power": site_status.get("solar_power"),
                        "battery_power": site_status.get(
                            "battery_power"
                        ),  # Negative = charging
                        "load_power": site_status.get("load_power"),
                    }
                    _LOGGER.debug(
                        f"Live status: SOC={result['battery_soc']}%, grid={result['grid_power']}W, solar={result['solar_power']}W"
                    )
                    return result
                else:
                    _LOGGER.debug(f"Failed to get live_status: {response.status}")

        except Exception as e:
            _LOGGER.debug(f"Error getting live status: {e}")

        return None

    async def _tesla_charge_kick(
        reason: str,
        *,
        allow_grid_field_absent_compatibility: bool = False,
        kick_generation: int,
        command_generation: int,
        operation_generation: int,
        initial_delay_seconds: float = 0,
    ) -> None:
        """Mode-bounce PW3 to force it to act on backup_reserve=100%.

        PW3 firmware can delay up to ~2 hours before acting on a
        backup_reserve=100% API command despite HTTP 200.  A quick
        self_consumption → autonomous bounce forces it to re-read its
        configuration and start charging immediately.

        Args:
            reason: Short label for log messages (e.g. "force_charge", "backup_reserve_100").
        """

        def _charge_kick_is_current() -> bool:
            return (
                _tesla_charge_kick_generation[0] == kick_generation
                and _command_generation[0] == command_generation
                and _tesla_operation_generation[0] == operation_generation
            )

        def _charge_kick_mode_owner_is_current() -> bool:
            """Keep restoring autonomous unless a newer mode owner took over."""
            return (
                _command_generation[0] == command_generation
                and _tesla_operation_generation[0] == operation_generation
            )

        if not _charge_kick_is_current():
            _LOGGER.debug("Skipping superseded charge kick (%s)", reason)
            return
        if initial_delay_seconds > 0:
            await asyncio.sleep(initial_delay_seconds)
            if not _charge_kick_is_current():
                _LOGGER.debug(
                    "Skipping superseded delayed charge kick (%s)",
                    reason,
                )
                return

        # Skip charge kick during suspected calibration
        _ck_entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        if _ck_entry_data.get("calibration_suspected"):
            _LOGGER.debug("Skipping charge kick (%s) — calibration suspected", reason)
            return

        try:
            # --- Pre-check: already charging? ---
            status = await get_live_status()
            if not _charge_kick_is_current():
                return
            if status and status.get("battery_power") is not None:
                bp = status["battery_power"]
                if bp < -200:
                    _LOGGER.info(
                        "Charge kick (%s): already charging (%dW), skipping",
                        reason,
                        abs(int(bp)),
                    )
                    return

            current_token, current_provider = token_getter()
            if not current_token:
                _LOGGER.warning("Charge kick (%s): no Tesla API token", reason)
                return

            site_id = entry.data.get(CONF_TESLA_ENERGY_SITE_ID)
            if not site_id:
                _LOGGER.warning("Charge kick (%s): no energy site ID", reason)
                return

            session = async_get_clientsession(hass)
            api_base = get_tesla_api_base_url(
                current_provider, entry.data.get(CONF_FLEET_API_BASE_URL)
            )
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            ensure_grid_charging = reason in {"force_charge", "backup_reserve_100"}

            async def _enable_grid_charging_after_bounce() -> bool:
                if not _charge_kick_is_current():
                    return False
                if not ensure_grid_charging:
                    return True
                result = await _tesla_force_apply_grid_charging(
                    [(str(site_id), current_token, current_provider)],
                    True,
                    reason=f"charge kick ({reason})",
                    is_current=_charge_kick_is_current,
                )
                confirmed = _tesla_force_result_all_confirmed(
                    result,
                    [(str(site_id), current_token, current_provider)],
                )
                field_absent_compatibility = False
                if (
                    not confirmed
                    and allow_grid_field_absent_compatibility
                    and reason in {"force_charge", "backup_reserve_100"}
                ):
                    field_absent_compatibility = (
                        _tesla_force_result_all_grid_field_absent_safe(
                            result,
                            [(str(site_id), current_token, current_provider)],
                        )
                    )
                    confirmed = field_absent_compatibility
                if field_absent_compatibility:
                    _LOGGER.warning(
                        "Charge kick (%s): Tesla accepted grid charging but "
                        "the readback field is unavailable; continuing to "
                        "charging verification",
                        reason,
                    )
                elif confirmed:
                    _LOGGER.info(
                        "Charge kick (%s): confirmed grid charging is enabled",
                        reason,
                    )
                return confirmed

            async def _mode_bounce() -> bool:
                """Execute self_consumption → 5s → autonomous bounce.

                Returns True if autonomous mode was verified.
                """
                if not _charge_kick_is_current():
                    return False
                # Switch to self_consumption
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": "self_consumption"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        _LOGGER.info(
                            "Charge kick (%s): switched to self_consumption", reason
                        )
                    else:
                        text = await resp.text()
                        _LOGGER.warning(
                            "Charge kick (%s): self_consumption POST failed %s - %s",
                            reason,
                            resp.status,
                            text,
                        )
                        return False

                if not _charge_kick_mode_owner_is_current():
                    return False
                await asyncio.sleep(5)
                if not _charge_kick_mode_owner_is_current():
                    return False

                # Switch back to autonomous (3 retries with verification)
                for attempt in range(3):
                    try:
                        if not _charge_kick_mode_owner_is_current():
                            return False
                        async with session.post(
                            f"{api_base}/api/1/energy_sites/{site_id}/operation",
                            headers=headers,
                            json={"default_real_mode": "autonomous"},
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as resp:
                            if resp.status != 200:
                                text = await resp.text()
                                _LOGGER.warning(
                                    "Charge kick (%s): autonomous POST failed %s - %s (attempt %d/3)",
                                    reason,
                                    resp.status,
                                    text,
                                    attempt + 1,
                                )
                                if attempt < 2:
                                    await asyncio.sleep(3)
                                continue

                        # Verify mode actually changed
                        await asyncio.sleep(2)
                        if not _charge_kick_mode_owner_is_current():
                            return False
                        async with session.get(
                            f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as verify_resp:
                            if verify_resp.status == 200:
                                data = await verify_resp.json()
                                mode = data.get("response", {}).get("default_real_mode")
                                if mode == "autonomous":
                                    _LOGGER.info(
                                        "Charge kick (%s): switched back to autonomous",
                                        reason,
                                    )
                                    return await _enable_grid_charging_after_bounce()
                                _LOGGER.warning(
                                    "Charge kick (%s): mode is '%s' not 'autonomous' (attempt %d/3)",
                                    reason,
                                    mode,
                                    attempt + 1,
                                )
                            else:
                                # Can't verify — assume success
                                _LOGGER.warning(
                                    "Charge kick (%s): couldn't verify mode (status %s), assuming success",
                                    reason,
                                    verify_resp.status,
                                )
                                return await _enable_grid_charging_after_bounce()
                    except Exception as e:
                        _LOGGER.warning(
                            "Charge kick (%s): autonomous attempt %d/3 error: %s",
                            reason,
                            attempt + 1,
                            e,
                        )

                    if attempt < 2:
                        await asyncio.sleep(3)
                        if not _charge_kick_mode_owner_is_current():
                            return False

                return False

            async def _restore_after_failed_bounce() -> None:
                """Recover either bounce without overwriting a newer owner."""
                if not _charge_kick_is_current():
                    _LOGGER.info(
                        "Charge kick (%s) superseded during confirmation; skipping stale cleanup",
                        reason,
                    )
                    return
                _LOGGER.error(
                    "Charge kick (%s): failed to restore autonomous mode after bounce — "
                    "auto-restoring normal operation",
                    reason,
                )
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        hass,
                        "Battery Alert",
                        f"Charge kick ({reason}): failed — auto-restoring normal operation",
                    )
                except Exception:
                    pass
                # Notification delivery yields; a new command may now own the mode.
                if not _charge_kick_is_current():
                    return
                # Reuse normal restoration and its persisted, bounded retry path.
                try:
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RESTORE_NORMAL,
                        {},
                        blocking=True,
                    )
                except Exception as restore_err:
                    _LOGGER.error(
                        "Auto-restore after charge kick failure also failed: %s",
                        restore_err,
                    )
                return

            # --- Execute the initial mode bounce ---
            _LOGGER.info("Charge kick (%s): mode bounce starting", reason)
            if not await _mode_bounce():
                await _restore_after_failed_bounce()
                return

            # --- Background verification task ---
            async def _verify_charging() -> None:
                """Poll live_status every 60s for up to 5 min to confirm charging started."""
                retry_bounce_done = False
                for poll in range(5):
                    await asyncio.sleep(60)
                    if not _charge_kick_is_current():
                        _LOGGER.debug(
                            "Charge kick verification (%s) superseded", reason
                        )
                        return
                    try:
                        poll_status = await get_live_status()
                        if poll_status and poll_status.get("battery_power") is not None:
                            bp = poll_status["battery_power"]
                            if bp < -200:
                                _LOGGER.info(
                                    "Charge kick verification (%s): charging confirmed (%dW) after %ds",
                                    reason,
                                    abs(int(bp)),
                                    (poll + 1) * 60,
                                )
                                return

                        # Battery already full — nothing to charge, not a failure
                        soc = poll_status.get("battery_soc") if poll_status else None
                        if soc is not None and soc >= 99:
                            _LOGGER.info(
                                "Charge kick verification (%s): battery already at %.0f%% — skipping",
                                reason,
                                soc,
                            )
                            return

                        # At the 120s mark (poll index 1), retry bounce once
                        if poll == 1 and not retry_bounce_done:
                            _LOGGER.info(
                                "Charge kick verification (%s): not charging after 120s, retrying bounce",
                                reason,
                            )
                            retry_bounce_done = True
                            if not await _mode_bounce():
                                await _restore_after_failed_bounce()
                                return
                            if not _charge_kick_is_current():
                                return

                    except Exception as e:
                        _LOGGER.debug("Charge kick verification poll error: %s", e)

                if not _charge_kick_is_current():
                    return
                _LOGGER.error(
                    "Charge kick verification (%s): battery did NOT start charging within 5 minutes",
                    reason,
                )
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        hass,
                        "Battery Alert",
                        f"Charge kick ({reason}): Powerwall did not start charging within 5 minutes",
                    )
                except Exception:
                    pass

            hass.async_create_task(_verify_charging())

        except Exception as e:
            _LOGGER.warning("Charge kick (%s) failed: %s", reason, e)

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    def _static_tou_tariff_schedule_for_sync() -> dict | None:
        """Return a stored static TOU schedule usable for battery tariff sync."""
        entry_data = _aemo_dispatch_entry_data()
        if entry_data is None:
            return None
        tariff_schedule = entry_data.get("tariff_schedule")
        if isinstance(tariff_schedule, dict) and (
            tariff_schedule.get("tou_periods") or tariff_schedule.get("buy_prices")
        ):
            return tariff_schedule

        automation_store_ref = entry_data.get("automation_store") or hass.data.get(
            DOMAIN, {}
        ).get("automation_store")
        custom_tariff = None
        if automation_store_ref:
            try:
                custom_tariff = automation_store_ref.get_custom_tariff()
            except Exception as err:
                _LOGGER.debug("Could not load custom tariff for TOU sync: %s", err)

        if not custom_tariff:
            return None

        tariff_schedule = convert_custom_tariff_to_schedule(
            custom_tariff,
            currency=currency_for_entry(entry, hass),
        )
        if tariff_schedule:
            entry_data["tariff_schedule"] = tariff_schedule
            return tariff_schedule
        return None

    def _static_tou_custom_tariff_for_sync() -> dict | None:
        """Return the saved raw custom tariff for a manual Tesla upload.

        ``tariff_schedule`` is intentionally a lossy display/planner shape;
        the raw custom tariff retains every season and AGL reward period.
        """
        entry_data = _aemo_dispatch_entry_data()
        if entry_data is None:
            return None
        automation_store_ref = entry_data.get("automation_store") or hass.data.get(
            DOMAIN, {}
        ).get("automation_store")
        custom_tariff = None
        if automation_store_ref:
            try:
                custom_tariff = automation_store_ref.get_custom_tariff()
            except Exception as err:
                _LOGGER.debug(
                    "Could not load raw custom tariff for Tesla TOU sync: %s", err
                )
        if not isinstance(custom_tariff, dict):
            # The initial config-flow value is normally moved into the store
            # during setup, but keep this fallback for an in-flight reload.
            custom_tariff = entry.data.get("initial_custom_tariff")
        return custom_tariff if isinstance(custom_tariff, dict) else None

    # ======================================================================
    # FORCE DISCHARGE AND RESTORE NORMAL SERVICES
    # ======================================================================

    # Get persisted force mode state (survives HA restarts)
    persisted_force_state = (
        hass.data[DOMAIN][entry.entry_id].get("force_mode_state") or {}
    )
    if persisted_force_state.get("source") == "optimizer":
        hass.data[DOMAIN][entry.entry_id]["optimizer_force_restart_restore_pending"] = (
            True
        )

    # A restore whose Tesla writes failed schedules an in-process retry. That
    # retry cannot run if the process is shutting down, and it self-skips once
    # force state has been cleared — so without a persisted marker a failed
    # restore is abandoned silently and the hardware is left unverified with
    # nothing tracking it. This marker lets the next startup finish the job.
    persisted_pending_restore = (
        hass.data[DOMAIN][entry.entry_id].get("pending_tesla_restore") or {}
    )

    # Storage for saved tariff and operation mode during force discharge
    force_discharge_state = {
        "active": False,
        "saved_tariff": None,
        "saved_operation_mode": None,
        "saved_backup_reserve": None,
        "saved_export_rule": None,
        "saved_grid_charging_enabled": None,
        "expires_at": None,
        "hardware_expires_at": None,
        "duration": None,
        "power_w": 0,
        "battery_discharge_w": 0,
        "cancel_expiry_timer": None,
        "_skip_backup_reserve_restore": False,
    }

    # Storage for saved tariff and operation mode during force charge
    force_charge_state = {
        "active": False,
        "saved_tariff": None,
        "saved_operation_mode": None,
        "saved_backup_reserve": None,
        "saved_grid_charging_enabled": None,
        "expires_at": None,
        "hardware_expires_at": None,
        "duration": None,
        "power_w": 0,
        "cancel_expiry_timer": None,
        "cancel_hardware_refresh_timer": None,
        "_skip_backup_reserve_restore": False,
    }
    if persisted_force_state.get("mode") == "charge":
        force_charge_state["_skip_backup_reserve_restore"] = bool(
            persisted_force_state.get("_skip_backup_reserve_restore")
        )
    elif persisted_force_state.get("mode") == "discharge":
        force_discharge_state["_skip_backup_reserve_restore"] = bool(
            persisted_force_state.get("_skip_backup_reserve_restore")
        )

    # Hold-SoC mode: brand-specific battery movement suppression. Some brands
    # can block both directions; others only block discharge while still
    # accepting excess solar. Duration-based with auto-restore on expiry.
    hold_soc_state = {
        "active": False,
        "saved_operation_mode": None,
        "saved_backup_reserve": None,
        "expires_at": None,
        "cancel_expiry_timer": None,
        "locked_soc": None,  # SoC at the moment Hold was engaged, for diagnostics
        "brand": None,
        "pending": False,
    }

    # Self-consumption override: duration-based like force charge/discharge.
    # When active, battery ignores TOU optimisation and runs pure
    # self-consumption until the timer expires or the user calls
    # restore_normal.
    self_consumption_state = {
        "active": False,
        "engaged_at": None,
        "expires_at": None,
        "duration": 0,
        "source": "user",
        "cancel_expiry_timer": None,
    }

    # Generation counter — incremented synchronously at the start of every
    # service command (before any await).  Each auto-restore callback captures
    # its generation at the time the timer is scheduled; if the counter has
    # advanced when the callback fires, a newer command was issued in the
    # meantime and the restore is silently skipped.
    #
    # This is the defence-in-depth layer.  The primary protection is
    # _cancel_all_force_timers(), which cancels pending timers before I/O.
    # Together they close a race where a previous command's expiry timer is
    # already dequeued in asyncio by the time cancel() is called.
    _command_generation = [0]  # mutable list so inner functions share one counter
    _tesla_charge_kick_generation = [0]
    _tesla_operation_generation = [0]
    _tesla_reserve_generation = [0]
    # A reserve nudge must be atomic: once the temporary value is written, its
    # exact target must be restored before a newer reserve command can proceed.
    _tesla_reserve_write_lock = asyncio.Lock()
    _tesla_reserve_write_tasks: set[asyncio.Task] = set()
    _tesla_reserve_pulse_runtime = hass.data[DOMAIN][entry.entry_id]
    _tesla_reserve_pulse_runtime["tesla_reserve_write_tasks"] = (
        _tesla_reserve_write_tasks
    )
    _tesla_reserve_pulse_runtime["tesla_reserve_pulse_stopping"] = False

    def _supersede_tesla_charge_kick(
        reason: str = "",
        *,
        owns_operation_mode: bool = False,
    ) -> int:
        """Invalidate pending Tesla charge-kick work before a newer command."""
        _tesla_charge_kick_generation[0] += 1
        if owns_operation_mode:
            _tesla_operation_generation[0] += 1
        if reason:
            _LOGGER.debug("Superseding pending Tesla charge kick: %s", reason)
        return _tesla_charge_kick_generation[0]

    def _schedule_tesla_charge_kick(
        reason: str,
        *,
        allow_grid_field_absent_compatibility: bool = False,
        initial_delay_seconds: float = 0,
    ) -> None:
        """Schedule a charge kick with synchronously captured command tokens."""
        kick_generation = _supersede_tesla_charge_kick(f"schedule {reason}")
        command_generation = _command_generation[0]
        operation_generation = _tesla_operation_generation[0]
        hass.async_create_task(
            _tesla_charge_kick(
                reason,
                allow_grid_field_absent_compatibility=(
                    allow_grid_field_absent_compatibility
                ),
                kick_generation=kick_generation,
                command_generation=command_generation,
                operation_generation=operation_generation,
                initial_delay_seconds=initial_delay_seconds,
            )
        )

    def _cancel_all_force_timers(reason: str = "") -> None:
        """Cancel all pending force-mode expiry timers.

        Must be called synchronously (no await) at the start of every service
        handler that issues an inverter command, before the first await.
        Because the asyncio event loop is single-threaded and this function
        contains no awaits, it is guaranteed to run to completion before any
        pending timer callback can be dequeued — even if the timer's scheduled
        time has already passed.
        """
        if reason:
            _LOGGER.debug("Cancelling pending force timers: %s", reason)
        for _state in (
            force_discharge_state,
            force_charge_state,
            hold_soc_state,
            self_consumption_state,
        ):
            for _timer_key in ("cancel_expiry_timer", "cancel_hardware_refresh_timer"):
                _cancel = _state.get(_timer_key)
                if _cancel:
                    _cancel()
                    _state[_timer_key] = None

    def _clear_self_consumption_state(send_update: bool = True) -> None:
        """Clear the user-facing self-consumption override/timer state."""
        if self_consumption_state.get("cancel_expiry_timer"):
            try:
                self_consumption_state["cancel_expiry_timer"]()
            except Exception:
                pass
        self_consumption_state["active"] = False
        self_consumption_state["engaged_at"] = None
        self_consumption_state["expires_at"] = None
        self_consumption_state["duration"] = 0
        self_consumption_state["source"] = "user"
        self_consumption_state["cancel_expiry_timer"] = None
        if send_update:
            async_dispatcher_send(
                hass,
                f"{DOMAIN}_self_consumption_state",
                {
                    "active": False,
                    "expires_at": None,
                    "duration": 0,
                },
            )

    def _clear_hold_soc_state() -> None:
        """Clear the user-facing Hold SoC state after hardware restore succeeds."""
        if hold_soc_state.get("cancel_expiry_timer"):
            try:
                hold_soc_state["cancel_expiry_timer"]()
            except Exception:
                pass
        hold_soc_state["active"] = False
        hold_soc_state["expires_at"] = None
        hold_soc_state["cancel_expiry_timer"] = None
        hold_soc_state["brand"] = None
        hold_soc_state["pending"] = False
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_hold_soc_state",
            {
                "active": False,
            },
        )

    # Store force states in hass.data so TariffPriceView can access them
    # This allows the endpoint to return real tariff instead of fake ML tariff
    hass.data[DOMAIN][entry.entry_id]["force_charge_state"] = force_charge_state
    hass.data[DOMAIN][entry.entry_id]["force_discharge_state"] = force_discharge_state
    # HD-13: also register hold_soc_state — sensor.py's Battery Mode sensor
    # reads entry_data.get("hold_soc_state", {}) and without this it always
    # sees an empty dict, so Hold SoC can never be reflected in the sensor.
    hass.data[DOMAIN][entry.entry_id]["hold_soc_state"] = hold_soc_state
    hass.data[DOMAIN][entry.entry_id]["self_consumption_state"] = self_consumption_state

    async def _cache_restorable_tesla_tariff(
        tariff: Any, source: str
    ) -> dict[str, Any] | None:
        """Cache a normal Tesla TOU tariff; ignore temporary force tariffs."""
        restorable_tariff = _select_restorable_tesla_tariff(tariff)
        if not restorable_tariff:
            if tariff:
                _LOGGER.warning(
                    "Ignoring Tesla v1r force tariff from %s as restore baseline (name: %s)",
                    source,
                    _tariff_display_name(tariff),
                )
            return None

        hass.data[DOMAIN][entry.entry_id]["last_restorable_tesla_tariff"] = (
            restorable_tariff
        )
        try:
            stored_data = await store.async_load() or {}
            stored_data["last_restorable_tesla_tariff"] = restorable_tariff
            await store.async_save(stored_data)
        except Exception as store_err:
            _LOGGER.debug(
                "Could not persist Tesla tariff baseline from %s: %s", source, store_err
            )
        return restorable_tariff

    def _cached_restorable_tesla_tariff() -> dict[str, Any] | None:
        return _select_restorable_tesla_tariff(
            hass.data.get(DOMAIN, {})
            .get(entry.entry_id, {})
            .get("last_restorable_tesla_tariff")
        )

    def _configured_restorable_tesla_tariff() -> dict[str, Any] | None:
        return _select_restorable_tesla_tariff(
            None,
            entry.data.get("initial_custom_tariff"),
        )

    def _optional_bool(value: Any) -> bool | None:
        """Return a bool for API booleans/strings, or None when unknown."""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off"):
                return False
            return None
        return bool(value)

    def _tesla_grid_charging_enabled_from_site_info(
        site_info: dict[str, Any],
    ) -> bool | None:
        """Extract Tesla grid-charging state from site_info."""
        return tesla_grid_charging_enabled_from_site_info(site_info)

    def _remember_tesla_grid_charging_preference(
        site_id: str,
        enabled: bool | None,
    ) -> bool | None:
        """Remember an observed or explicitly requested persistent preference."""
        if enabled is None:
            return tesla_grid_charging_preferences.get(str(site_id))
        tesla_grid_charging_preferences[str(site_id)] = bool(enabled)
        return bool(enabled)

    async def _persist_tesla_grid_charging_preference(
        site_configs: list[tuple[str, str, str]],
        enabled: bool,
        *,
        source: str,
    ) -> None:
        """Persist a confirmed or narrowly accepted user preference."""
        for site_id, _token, _provider in site_configs:
            _remember_tesla_grid_charging_preference(site_id, enabled)
        try:
            data = await store.async_load() or {}
            data["tesla_grid_charging_preferences"] = dict(
                tesla_grid_charging_preferences
            )
            await store.async_save(data)
        except Exception as store_err:
            _LOGGER.warning(
                "Could not persist Tesla grid charging preference from %s: %s",
                source,
                store_err,
            )

    async def _unknown_tesla_grid_charging_baselines(
        site_configs: list[tuple[str, str, str]],
    ) -> list[str]:
        """Return Tesla sites whose pre-force grid setting is not observable."""
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        local_coordinator = (
            entry_data.get("powerwall_local", {}).get("coordinator")
            if entry.data.get(CONF_POWERWALL_LOCAL_PAIRED)
            else None
        )
        local_snapshot = getattr(local_coordinator, "data", None)
        local_last_success = getattr(
            local_coordinator,
            "last_success_monotonic",
            None,
        )
        local_snapshot_fresh = (
            local_snapshot is not None
            and local_last_success is not None
            and time.monotonic() - local_last_success
            <= TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS
        )
        session = async_get_clientsession(hass)
        unknown_sites: list[str] = []

        for index, (site_id, current_token, provider) in enumerate(site_configs):
            observed_grid_charging = None
            if index == 0 and local_snapshot_fresh:
                local_enabled = getattr(
                    local_snapshot,
                    "grid_charging_enabled",
                    None,
                )
                if local_enabled is not None:
                    observed_grid_charging = bool(local_enabled)

            if observed_grid_charging is None:
                headers = {
                    "Authorization": f"Bearer {current_token}",
                    "Content-Type": "application/json",
                }
                api_base = get_tesla_api_base_url(
                    provider,
                    entry.data.get(CONF_FLEET_API_BASE_URL),
                )
                try:
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            site_info = data.get("response", {})
                            observed_grid_charging = (
                                _tesla_grid_charging_enabled_from_site_info(site_info)
                            )
                except Exception as baseline_err:
                    _LOGGER.warning(
                        "Could not read Tesla grid charging baseline for site %s: %s",
                        site_id,
                        baseline_err,
                    )

            resolved_grid_charging = _remember_tesla_grid_charging_preference(
                site_id,
                observed_grid_charging,
            )
            if resolved_grid_charging is None:
                unknown_sites.append(site_id)

        return unknown_sites

    async def _require_tesla_force_grid_charging_baselines(
        site_configs: list[tuple[str, str, str]],
        inherited_grid_charging: Any = None,
        *,
        inheritance_required: bool = False,
    ) -> None:
        """Reject a force transition that cannot restore grid charging."""
        if inheritance_required and inherited_grid_charging is None:
            raise HomeAssistantError(
                "Cannot switch Tesla force modes because the original Grid "
                "Charging setting is unavailable. Restore normal operation, "
                "set the Tesla v1r Grid Charging control once, then retry."
            )
        if inherited_grid_charging is not None:
            return
        unknown_sites = await _unknown_tesla_grid_charging_baselines(site_configs)
        if unknown_sites:
            raise HomeAssistantError(
                "Cannot start Tesla force mode because the current Grid "
                "Charging setting is unavailable. Set the Tesla v1r Grid "
                "Charging control once, then retry."
            )

    def _coerce_force_power_w(value: Any) -> int:
        """Normalize service/store force-power values to a non-negative watt value."""
        try:
            power_w = int(float(value))
        except (TypeError, ValueError):
            return 0
        return max(0, power_w)

    def _configured_force_power_w(direction: str) -> int:
        """Return the optimizer max power setting for manual force commands."""
        key = (
            CONF_OPTIMIZATION_MAX_CHARGE_W
            if direction == "charge"
            else CONF_OPTIMIZATION_MAX_DISCHARGE_W
        )
        value = entry.options.get(key, entry.data.get(key))
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0
        if parsed <= 0:
            return 0
        # Legacy/manual writes may have stored kW even though the key is *_W.
        if parsed <= 100:
            parsed *= 1000
        return int(round(parsed))

    def _resolve_force_command_power_w(direction: str, requested: Any) -> int:
        """Resolve force power while respecting optimizer max power settings."""
        explicit_power_w = _coerce_force_power_w(requested)
        configured_power_w = _configured_force_power_w(direction)
        if explicit_power_w > 0:
            if configured_power_w > 0 and explicit_power_w > configured_power_w:
                _LOGGER.info(
                    "Force %s: clamping explicit power %dW to optimizer max %dW",
                    direction,
                    explicit_power_w,
                    configured_power_w,
                )
                return configured_power_w
            return explicit_power_w
        if configured_power_w > 0:
            _LOGGER.info(
                "Force %s: no explicit power_w supplied; using optimizer max %dW",
                direction,
                configured_power_w,
            )
        return configured_power_w

    def _manual_projection_signature(state: dict[str, Any] | None) -> tuple | None:
        """Return the optimizer-relevant identity of an external control."""
        state = state or {}
        if (
            state.get("mode")
            not in (
                "charge",
                "discharge",
                "hold_soc",
                "self_consumption",
            )
            or state.get("source") == "optimizer"
        ):
            return None
        return (
            state.get("mode"),
            state.get("source", "user"),
            state.get("expires_at"),
            state.get("power_w"),
            state.get("locked_soc"),
        )

    _last_manual_projection_signature = [
        _manual_projection_signature(persisted_force_state)
    ]
    _manual_replan_runtime: dict[str, Any] = {
        "requested": False,
        "task": None,
    }

    async def _run_manual_control_replan() -> None:
        """Coalesce force mutations into fresh optimizer projections."""
        try:
            while _manual_replan_runtime["requested"]:
                _manual_replan_runtime["requested"] = False
                await asyncio.sleep(0)
                coordinator = (
                    hass.data.get(DOMAIN, {})
                    .get(entry.entry_id, {})
                    .get("optimization_coordinator")
                )
                if coordinator is None or not getattr(
                    coordinator,
                    "enabled",
                    False,
                ):
                    continue
                try:
                    await coordinator.force_reoptimize()
                except Exception as err:
                    _LOGGER.warning(
                        "Manual control changed but immediate optimizer replan failed: %s",
                        err,
                        exc_info=True,
                    )
        finally:
            _manual_replan_runtime["task"] = None
            if _manual_replan_runtime["requested"]:
                _request_manual_control_replan()

    def _request_manual_control_replan() -> None:
        """Schedule a non-blocking replan after a manual control mutation."""
        _manual_replan_runtime["requested"] = True
        task = _manual_replan_runtime.get("task")
        if task is None or task.done():
            _manual_replan_runtime["task"] = hass.async_create_task(
                _run_manual_control_replan()
            )

    # Helper function to persist force mode state to storage
    async def persist_pending_tesla_restore(
        reason: str | None,
        *,
        skip_backup_reserve_restore: bool = False,
    ) -> None:
        """Record (or clear) a Tesla restore that did not finish.

        An in-process retry timer cannot survive the process exiting, and it
        self-skips once force state has been cleared. Persisting the intent
        means the next startup can finish the restore instead of leaving the
        hardware in a half-restored state with nothing tracking it.
        """
        stored_data = await store.async_load() or {}
        marker = (
            {
                "reason": reason,
                "recorded_at": dt_util.utcnow().isoformat(),
                "_skip_backup_reserve_restore": bool(skip_backup_reserve_restore),
            }
            if reason
            else None
        )
        stored_data["pending_tesla_restore"] = marker
        await store.async_save(stored_data)
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(entry_data, dict):
            entry_data["pending_tesla_restore"] = marker
        if marker:
            _LOGGER.warning(
                "Recorded an unfinished Tesla restore for completion at next "
                "startup: %s",
                reason,
            )
        else:
            _LOGGER.debug("Cleared the unfinished Tesla restore marker")

    async def persist_force_mode_state() -> None:
        """Persist current force charge/discharge/hold state to storage."""
        stored_data = await store.async_load() or {}

        # Only save what's needed to restore after restart
        state_to_save = None
        if force_charge_state["active"]:
            state_to_save = {
                "mode": "charge",
                "expires_at": force_charge_state["expires_at"].isoformat()
                if force_charge_state["expires_at"]
                else None,
                "hardware_expires_at": force_charge_state[
                    "hardware_expires_at"
                ].isoformat()
                if force_charge_state.get("hardware_expires_at")
                else None,
                "duration": force_charge_state.get("duration"),
                "power_w": _coerce_force_power_w(force_charge_state.get("power_w", 0)),
                "source": force_charge_state.get("source", "user"),
                "saved_tariff": _select_restorable_tesla_tariff(
                    force_charge_state["saved_tariff"]
                ),
                "saved_operation_mode": force_charge_state["saved_operation_mode"],
                "saved_backup_reserve": force_charge_state["saved_backup_reserve"],
                "saved_grid_charging_enabled": force_charge_state.get(
                    "saved_grid_charging_enabled"
                ),
                "_skip_backup_reserve_restore": bool(
                    force_charge_state.get("_skip_backup_reserve_restore")
                ),
            }
        elif force_discharge_state["active"]:
            state_to_save = {
                "mode": "discharge",
                "expires_at": force_discharge_state["expires_at"].isoformat()
                if force_discharge_state["expires_at"]
                else None,
                "hardware_expires_at": force_discharge_state[
                    "hardware_expires_at"
                ].isoformat()
                if force_discharge_state.get("hardware_expires_at")
                else None,
                "duration": force_discharge_state.get("duration"),
                "power_w": _coerce_force_power_w(
                    force_discharge_state.get("power_w", 0)
                ),
                "battery_discharge_w": _coerce_force_power_w(
                    force_discharge_state.get("battery_discharge_w", 0)
                ),
                "source": force_discharge_state.get("source", "user"),
                "saved_tariff": _select_restorable_tesla_tariff(
                    force_discharge_state["saved_tariff"]
                ),
                "saved_operation_mode": force_discharge_state["saved_operation_mode"],
                "saved_backup_reserve": force_discharge_state["saved_backup_reserve"],
                "saved_export_rule": force_discharge_state["saved_export_rule"],
                "saved_grid_charging_enabled": force_discharge_state.get(
                    "saved_grid_charging_enabled"
                ),
                "_skip_backup_reserve_restore": bool(
                    force_discharge_state.get("_skip_backup_reserve_restore")
                ),
            }
        elif hold_soc_state["active"]:
            # OB-5: Hold SoC is a third force-like mode (battery locked in
            # backup/standby) and must be persisted the same way force
            # charge/discharge are, or a restart/reload mid-hold leaves the
            # hardware stuck in standby with nothing tracking it. Without
            # this branch, state_to_save stays None here and the write below
            # clobbers any previously-persisted force state with None.
            state_to_save = {
                "mode": "hold_soc",
                "expires_at": hold_soc_state["expires_at"].isoformat()
                if hold_soc_state.get("expires_at")
                else None,
                "locked_soc": hold_soc_state.get("locked_soc"),
                "saved_operation_mode": hold_soc_state.get("saved_operation_mode"),
                "saved_backup_reserve": hold_soc_state.get("saved_backup_reserve"),
                "brand": hold_soc_state.get("brand"),
            }
        elif self_consumption_state["active"]:
            state_to_save = {
                "mode": "self_consumption",
                "expires_at": self_consumption_state["expires_at"].isoformat()
                if self_consumption_state.get("expires_at")
                else None,
                "duration": self_consumption_state.get("duration"),
                "engaged_at": self_consumption_state["engaged_at"].isoformat()
                if self_consumption_state.get("engaged_at")
                else None,
                "source": self_consumption_state.get("source", "user"),
            }

        if state_to_save and state_to_save.get("mode") in ("charge", "discharge"):
            solax_coord = (
                hass.data.get(DOMAIN, {})
                .get(entry.entry_id, {})
                .get("solax_coordinator")
            )
            get_restore_state = getattr(
                solax_coord,
                "get_force_restore_state",
                None,
            )
            if callable(get_restore_state):
                solax_restore_state = get_restore_state()
                if solax_restore_state:
                    state_to_save["solax_restore_state"] = solax_restore_state

        stored_data["force_mode_state"] = state_to_save
        stored_data["tesla_grid_charging_preferences"] = dict(
            tesla_grid_charging_preferences
        )
        await store.async_save(stored_data)
        try:
            manual_signature = _manual_projection_signature(state_to_save)
            if manual_signature != _last_manual_projection_signature[0]:
                _last_manual_projection_signature[0] = manual_signature
                _request_manual_control_replan()
        except NameError:
            # Source-extracted persistence tests intentionally execute this
            # nested helper without its setup-entry closure.
            pass
        if state_to_save:
            _LOGGER.debug(
                f"Persisted force mode state: {state_to_save['mode']} expires {state_to_save['expires_at']}"
            )
        else:
            _LOGGER.debug("Cleared persisted force mode state")

    # Restore force mode state from persistence (after HA restart)
    async def restore_force_mode_from_persistence():
        """Restore force charge/discharge state after HA restart."""

        if not persisted_force_state:
            # No force state to re-arm, but a previous run may still owe a
            # restore whose Tesla writes never verified. Finish it here: the
            # in-process retry that was scheduled for it died with that
            # process, and nothing else will ever pick it up.
            if persisted_pending_restore:
                _LOGGER.warning(
                    "Completing an unfinished Tesla restore from a previous run (%s)",
                    persisted_pending_restore.get("reason"),
                )
                try:
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RESTORE_NORMAL,
                        {
                            "_allow_monitoring_restore": True,
                            "_skip_backup_reserve_restore": bool(
                                persisted_pending_restore.get(
                                    "_skip_backup_reserve_restore"
                                )
                            ),
                            "source": "startup_pending_restore",
                        },
                        blocking=True,
                    )
                except Exception as err:
                    _LOGGER.error(
                        "Could not complete the unfinished Tesla restore: %s",
                        err,
                    )
            return

        def _native_battery_control_coordinator() -> Any | None:
            coordinator = (
                hass.data.get(DOMAIN, {})
                .get(entry.entry_id, {})
                .get("battery_energy_coordinator")
            )
            return (
                coordinator if _uses_native_battery_integration(coordinator) else None
            )

        async def _wait_for_native_battery_control_ready(operation: str) -> bool:
            """Wait for the upstream integration without imposing a fixed timeout."""
            coordinator = _native_battery_control_coordinator()
            if coordinator is None:
                return True

            attempt = 0
            while entry.entry_id in hass.data.get(DOMAIN, {}):
                checker = getattr(coordinator, "startup_control_ready", None)
                if callable(checker) and checker():
                    return True
                attempt += 1
                if attempt == 1:
                    _LOGGER.info(
                        "%s is waiting for the native Home Assistant battery "
                        "integration to become ready",
                        operation,
                    )
                await asyncio.sleep(min(30, 5 * attempt))

            _LOGGER.info(
                "%s stopped waiting because the config entry was unloaded",
                operation,
            )
            return False

        async def _restore_persisted_normal(
            service_data: dict[str, Any],
        ) -> bool:
            """Restore persisted state once the active battery integration is ready."""
            coordinator = _native_battery_control_coordinator()
            if not await _wait_for_native_battery_control_ready(
                "Persisted force cleanup"
            ):
                return False

            attempt = 0
            while entry.entry_id in hass.data.get(DOMAIN, {}):
                try:
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RESTORE_NORMAL,
                        service_data,
                        blocking=True,
                    )
                    return True
                except Exception as err:
                    if coordinator is None:
                        _LOGGER.error(
                            "Persisted force cleanup failed: %s",
                            err,
                            exc_info=True,
                        )
                        return False
                    attempt += 1
                    if attempt == 1:
                        _LOGGER.warning(
                            "Native battery persisted cleanup attempt failed; "
                            "preserving state for retry: %s",
                            err,
                        )
                    if not await _wait_for_native_battery_control_ready(
                        "Persisted force cleanup retry"
                    ):
                        return False
                    await asyncio.sleep(min(30, 5 * attempt))

            _LOGGER.info(
                "Native battery persisted cleanup stopped because the config entry "
                "was unloaded"
            )
            return False

        mode = persisted_force_state.get("mode")
        expires_at_str = persisted_force_state.get("expires_at")

        if not mode or not expires_at_str:
            _LOGGER.info("No valid persisted force mode state to restore")
            if persisted_force_state.get("source") == "optimizer":
                hass.data[DOMAIN][entry.entry_id][
                    "optimizer_force_restart_restore_pending"
                ] = False
            return

        if mode in ("charge", "discharge"):
            solax_coord = (
                hass.data.get(DOMAIN, {})
                .get(entry.entry_id, {})
                .get("solax_coordinator")
            )
            set_restore_state = getattr(
                solax_coord,
                "set_force_restore_state",
                None,
            )
            if callable(set_restore_state):
                set_restore_state(persisted_force_state.get("solax_restore_state"))

        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            # Ensure timezone-aware
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=dt_util.UTC)

            hardware_expires_at = None
            hardware_expires_at_str = persisted_force_state.get("hardware_expires_at")
            if hardware_expires_at_str:
                try:
                    hardware_expires_at = datetime.fromisoformat(
                        hardware_expires_at_str
                    )
                    if hardware_expires_at.tzinfo is None:
                        hardware_expires_at = hardware_expires_at.replace(
                            tzinfo=dt_util.UTC
                        )
                except (TypeError, ValueError):
                    hardware_expires_at = None

            now = dt_util.utcnow()
            persisted_source = persisted_force_state.get("source", "user")
            persisted_power_w = _coerce_force_power_w(
                persisted_force_state.get("power_w", 0)
            )
            persisted_battery_discharge_w = _coerce_force_power_w(
                persisted_force_state.get("battery_discharge_w", 0)
            )

            if mode == "self_consumption":
                duration = persisted_force_state.get("duration")
                engaged_at_str = persisted_force_state.get("engaged_at")
                engaged_at = None
                if engaged_at_str:
                    try:
                        engaged_at = datetime.fromisoformat(engaged_at_str)
                        if engaged_at.tzinfo is None:
                            engaged_at = engaged_at.replace(tzinfo=dt_util.UTC)
                    except (TypeError, ValueError):
                        engaged_at = None

                if now >= expires_at:
                    _LOGGER.info(
                        f"⏰ Persisted self-consumption override has expired (was {expires_at_str}), restoring normal operation"
                    )

                    self_consumption_state["active"] = True
                    self_consumption_state["expires_at"] = expires_at
                    self_consumption_state["duration"] = duration or 0
                    self_consumption_state["engaged_at"] = engaged_at
                    self_consumption_state["source"] = persisted_source

                    restore_completed = await _restore_persisted_normal(
                        {"source": "user"}
                    )
                    if restore_completed:
                        _LOGGER.info(
                            "✅ Restored normal operation after expired self-consumption override"
                        )
                        stored_data = await store.async_load() or {}
                        stored_data["force_mode_state"] = None
                        await store.async_save(stored_data)
                    else:
                        _LOGGER.warning(
                            "Expired self-consumption cleanup remains stored for retry"
                        )
                else:
                    remaining_seconds = (expires_at - now).total_seconds()
                    remaining_minutes = remaining_seconds / 60
                    _LOGGER.info(
                        f"🔄 Restoring self-consumption override from persistence ({remaining_minutes:.1f} min remaining)"
                    )

                    self_consumption_state["active"] = True
                    self_consumption_state["expires_at"] = expires_at
                    self_consumption_state["duration"] = duration or int(
                        remaining_minutes
                    )
                    self_consumption_state["engaged_at"] = engaged_at or now
                    self_consumption_state["source"] = persisted_source

                    _restore_gen_self_persisted = _command_generation[0]

                    async def auto_restore_self_consumption_persisted(_now):
                        if _command_generation[0] != _restore_gen_self_persisted:
                            _LOGGER.debug(
                                "Persisted self-consumption timer superseded — skipping restore"
                            )
                            return
                        if self_consumption_state["active"]:
                            _LOGGER.info(
                                "⏰ Self-consumption override expired (restored timer), auto-restoring"
                            )
                            await _restore_persisted_normal({"source": "user"})

                    self_consumption_state["cancel_expiry_timer"] = (
                        async_track_point_in_utc_time(
                            hass, auto_restore_self_consumption_persisted, expires_at
                        )
                    )

                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_self_consumption_state",
                        {
                            "active": True,
                            "expires_at": expires_at.isoformat(),
                            "duration": self_consumption_state["duration"],
                            "engaged_at": self_consumption_state[
                                "engaged_at"
                            ].isoformat(),
                        },
                    )
                    _LOGGER.info(
                        f"✅ Self-consumption override restored from persistence, expires in {remaining_minutes:.1f} min"
                    )
                return

            # OB-5: Hold SoC is not a charge/discharge tariff replay — it's a
            # standby/backup lock — so it gets its own branch instead of the
            # "state = force_charge_state if mode == 'charge' else
            # force_discharge_state" selection used below. It also never
            # persists with source == "optimizer" (handle_hold_battery_soc
            # skips state management/persistence entirely for optimizer-
            # sourced holds), so there is no optimizer-replay branch to
            # mirror here.
            if mode == "hold_soc":
                locked_soc = persisted_force_state.get("locked_soc")
                saved_operation_mode = persisted_force_state.get("saved_operation_mode")
                saved_backup_reserve = persisted_force_state.get("saved_backup_reserve")

                if now >= expires_at:
                    _LOGGER.info(
                        f"⏰ Persisted Hold SoC has expired (was {expires_at_str}), restoring normal operation"
                    )

                    # Populate hold_soc_state so handle_restore_normal can do
                    # a full cleanup (mirrors the charge/discharge expired
                    # path immediately below).
                    hold_soc_state["active"] = True
                    hold_soc_state["expires_at"] = expires_at
                    hold_soc_state["locked_soc"] = locked_soc
                    hold_soc_state["saved_operation_mode"] = saved_operation_mode
                    hold_soc_state["saved_backup_reserve"] = saved_backup_reserve
                    hold_soc_state["brand"] = persisted_force_state.get("brand")

                    restore_completed = await _restore_persisted_normal(
                        {
                            "source": "hold_soc_cleanup",
                            "_force_restore": True,
                        }
                    )
                    if restore_completed:
                        if hold_soc_state.get("active"):
                            _LOGGER.warning(
                                "Expired Hold SoC restore did not complete; preserving state for retry"
                            )
                        else:
                            _LOGGER.info(
                                "✅ Restored normal operation after expired Hold SoC"
                            )
                    else:
                        _LOGGER.warning(
                            "Expired Hold SoC cleanup remains stored for retry"
                        )

                    if not hold_soc_state.get("active"):
                        stored_data = await store.async_load() or {}
                        stored_data["force_mode_state"] = None
                        await store.async_save(stored_data)
                else:
                    # Hold SoC is still active - the inverter/gateway itself
                    # stays in standby/backup mode across a restart (that's
                    # the hardware fault this bug is about), so there is
                    # nothing to re-issue here — just re-arm the in-memory
                    # state, its expiry timer, and the dispatcher so the
                    # mobile Controls screen and the eventual auto-restore
                    # both work again.
                    remaining_seconds = (expires_at - now).total_seconds()
                    remaining_minutes = remaining_seconds / 60
                    _LOGGER.info(
                        f"🔄 Restoring Hold SoC from persistence ({remaining_minutes:.1f} min remaining)"
                    )

                    hold_soc_state["active"] = True
                    hold_soc_state["expires_at"] = expires_at
                    hold_soc_state["locked_soc"] = locked_soc
                    hold_soc_state["saved_operation_mode"] = saved_operation_mode
                    hold_soc_state["saved_backup_reserve"] = saved_backup_reserve
                    hold_soc_state["brand"] = persisted_force_state.get("brand")

                    # OB-7: async_unload_entry now cancels this timer on every
                    # unload/reload, so re-arming it here can never collide
                    # with an orphaned pre-reload timer.
                    _restore_gen_hold_persisted = _command_generation[0]

                    async def auto_restore_hold_soc_persisted(_now):
                        if _command_generation[0] != _restore_gen_hold_persisted:
                            _LOGGER.debug(
                                "Persisted Hold SoC timer superseded — skipping restore"
                            )
                            return
                        if hold_soc_state["active"]:
                            _LOGGER.info(
                                "⏰ Hold SoC expired (restored timer), auto-restoring"
                            )
                            await _restore_persisted_normal(
                                {"source": "hold_soc_cleanup", "_force_restore": True},
                            )

                    hold_soc_state["cancel_expiry_timer"] = (
                        async_track_point_in_utc_time(
                            hass, auto_restore_hold_soc_persisted, expires_at
                        )
                    )

                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_hold_soc_state",
                        {
                            "active": True,
                            "expires_at": expires_at.isoformat(),
                            "locked_soc": locked_soc,
                        },
                    )
                    _LOGGER.info(
                        f"✅ Hold SoC restored from persistence, expires in {remaining_minutes:.1f} min"
                    )
                return

            if _is_monitoring_mode():
                saved_tariff = _select_restorable_tesla_tariff(
                    persisted_force_state.get("saved_tariff"),
                    _cached_restorable_tesla_tariff(),
                    _configured_restorable_tesla_tariff(),
                )
                state = (
                    force_charge_state if mode == "charge" else force_discharge_state
                )
                state["active"] = True
                state["expires_at"] = expires_at
                state["hardware_expires_at"] = hardware_expires_at
                state["duration"] = persisted_force_state.get("duration")
                state["power_w"] = persisted_power_w
                state["source"] = persisted_source
                state["saved_tariff"] = saved_tariff
                state["saved_operation_mode"] = persisted_force_state.get(
                    "saved_operation_mode"
                )
                state["saved_backup_reserve"] = persisted_force_state.get(
                    "saved_backup_reserve"
                )
                state["saved_export_rule"] = persisted_force_state.get(
                    "saved_export_rule"
                )
                state["saved_grid_charging_enabled"] = persisted_force_state.get(
                    "saved_grid_charging_enabled"
                )
                restore_completed = False
                _LOGGER.info(
                    "[MONITORING] Persisted force %s will not be replayed; restoring normal operation",
                    mode,
                )
                restore_completed = await _restore_persisted_normal(
                    {
                        "source": persisted_source,
                        "_force_restore": True,
                        "_allow_monitoring_restore": True,
                    }
                )
                if not restore_completed:
                    _LOGGER.warning(
                        "Persisted force %s cleanup remains pending for retry",
                        mode,
                    )
                    return
                if persisted_source == "optimizer":
                    hass.data[DOMAIN][entry.entry_id][
                        "optimizer_force_restart_restore_pending"
                    ] = False
                stored_data = await store.async_load() or {}
                stored_data["force_mode_state"] = None
                await store.async_save(stored_data)
                return

            if persisted_source == "optimizer":
                # Optimizer-owned force modes are schedule decisions, not user
                # commands. After a restart/update the LP must recalculate from
                # current prices/SOC instead of replaying a stale force tariff.
                _LOGGER.info(
                    "Ignoring persisted optimizer force %s after restart; "
                    "restoring normal tariff and letting the LP recalculate",
                    mode,
                )
                saved_tariff = _select_restorable_tesla_tariff(
                    persisted_force_state.get("saved_tariff"),
                    _cached_restorable_tesla_tariff(),
                    _configured_restorable_tesla_tariff(),
                )
                state = (
                    force_charge_state if mode == "charge" else force_discharge_state
                )
                state["active"] = True
                state["expires_at"] = expires_at
                state["hardware_expires_at"] = hardware_expires_at
                state["duration"] = persisted_force_state.get("duration")
                state["power_w"] = persisted_power_w
                state["source"] = persisted_source
                state["saved_tariff"] = saved_tariff
                state["saved_operation_mode"] = persisted_force_state.get(
                    "saved_operation_mode"
                )
                state["saved_backup_reserve"] = persisted_force_state.get(
                    "saved_backup_reserve"
                )
                state["saved_export_rule"] = persisted_force_state.get(
                    "saved_export_rule"
                )
                state["saved_grid_charging_enabled"] = persisted_force_state.get(
                    "saved_grid_charging_enabled"
                )

                restore_completed = await _restore_persisted_normal(
                    {"source": "optimizer"}
                )
                if not restore_completed:
                    _LOGGER.warning(
                        "Persisted optimizer force %s cleanup remains pending "
                        "for retry",
                        mode,
                    )
                    return

                _LOGGER.info(
                    "Cleared stale optimizer force %s after restart",
                    mode,
                )
                hass.data[DOMAIN][entry.entry_id][
                    "optimizer_force_restart_restore_pending"
                ] = False

                stored_data = await store.async_load() or {}
                stored_data["force_mode_state"] = None
                await store.async_save(stored_data)
                return

            if now >= expires_at:
                # Force mode has expired during restart - need to restore Tesla to normal
                _LOGGER.info(
                    f"⏰ Persisted force {mode} has expired (was {expires_at_str}), restoring saved tariff"
                )

                saved_tariff = _select_restorable_tesla_tariff(
                    persisted_force_state.get("saved_tariff"),
                    _cached_restorable_tesla_tariff(),
                    _configured_restorable_tesla_tariff(),
                )
                saved_backup_reserve = persisted_force_state.get("saved_backup_reserve")

                # Populate force state so handle_restore_normal can do a full cleanup
                # (restore tariff, operation mode, backup reserve, clear state)
                state = (
                    force_charge_state if mode == "charge" else force_discharge_state
                )
                state["active"] = True
                state["expires_at"] = expires_at
                state["hardware_expires_at"] = hardware_expires_at
                state["duration"] = persisted_force_state.get("duration")
                state["power_w"] = persisted_power_w
                state["source"] = persisted_force_state.get("source", "user")
                state["saved_tariff"] = saved_tariff
                state["saved_operation_mode"] = persisted_force_state.get(
                    "saved_operation_mode"
                )
                state["saved_backup_reserve"] = saved_backup_reserve
                state["saved_export_rule"] = persisted_force_state.get(
                    "saved_export_rule"
                )
                state["saved_grid_charging_enabled"] = persisted_force_state.get(
                    "saved_grid_charging_enabled"
                )

                restore_completed = await _restore_persisted_normal(
                    {
                        "source": "force_timer",
                        "_allow_monitoring_restore": True,
                    }
                )
                if restore_completed:
                    _LOGGER.info(
                        "✅ Restored normal operation after expired force mode"
                    )
                    stored_data = await store.async_load() or {}
                    stored_data["force_mode_state"] = None
                    await store.async_save(stored_data)
                else:
                    _LOGGER.warning(
                        "Expired persisted force %s cleanup remains stored for retry",
                        mode,
                    )
            else:
                # Force mode is still active - restore state and re-setup timer
                remaining_seconds = (expires_at - now).total_seconds()
                remaining_minutes = remaining_seconds / 60
                _LOGGER.info(
                    f"🔄 Restoring force {mode} from persistence ({remaining_minutes:.1f} min remaining)"
                )

                if mode == "charge":
                    force_charge_state["active"] = True
                    force_charge_state["expires_at"] = expires_at
                    force_charge_state["hardware_expires_at"] = hardware_expires_at
                    force_charge_state["duration"] = persisted_force_state.get(
                        "duration", int(remaining_minutes)
                    )
                    force_charge_state["power_w"] = persisted_power_w
                    force_charge_state["source"] = persisted_force_state.get(
                        "source", "user"
                    )
                    force_charge_state["saved_tariff"] = (
                        _select_restorable_tesla_tariff(
                            persisted_force_state.get("saved_tariff"),
                            _cached_restorable_tesla_tariff(),
                            _configured_restorable_tesla_tariff(),
                        )
                    )
                    force_charge_state["saved_operation_mode"] = (
                        persisted_force_state.get("saved_operation_mode")
                    )
                    force_charge_state["saved_backup_reserve"] = (
                        persisted_force_state.get("saved_backup_reserve")
                    )
                    force_charge_state["saved_grid_charging_enabled"] = (
                        persisted_force_state.get("saved_grid_charging_enabled")
                    )

                    # Re-issue the charge command to the inverter
                    if not await _wait_for_native_battery_control_ready(
                        "Persisted force charge replay"
                    ):
                        return
                    replay_remaining_seconds = (
                        expires_at - dt_util.utcnow()
                    ).total_seconds()
                    if replay_remaining_seconds <= 0:
                        _LOGGER.info(
                            "Persisted force charge expired while waiting for "
                            "the native battery integration; restoring normal "
                            "operation instead of replaying it"
                        )
                        if await _restore_persisted_normal(
                            {
                                "source": "force_timer",
                                "_allow_monitoring_restore": True,
                            }
                        ):
                            stored_data = await store.async_load() or {}
                            stored_data["force_mode_state"] = None
                            await store.async_save(stored_data)
                        return
                    try:
                        remaining_min = max(
                            1,
                            math.ceil(replay_remaining_seconds / 60),
                        )
                        service_data = {"duration": remaining_min}
                        if persisted_power_w > 0:
                            service_data["power_w"] = persisted_power_w
                        await hass.services.async_call(
                            DOMAIN,
                            SERVICE_FORCE_CHARGE,
                            service_data,
                            blocking=True,
                        )
                        _LOGGER.info(
                            "🔋 Re-issued force charge command after restart (%d min, %dW)",
                            remaining_min,
                            persisted_power_w,
                        )
                    except Exception as e:
                        _LOGGER.error(
                            "Failed to re-issue force charge after restart: %s", e
                        )

                    # Re-setup expiry timer
                    _restore_gen_charge_persisted = _command_generation[0]

                    async def auto_restore_charge(_now):
                        if _command_generation[0] != _restore_gen_charge_persisted:
                            _LOGGER.debug(
                                "Persisted force charge timer superseded — skipping restore"
                            )
                            return
                        if force_charge_state["active"]:
                            _LOGGER.info(
                                "⏰ Force charge expired (restored timer), auto-restoring"
                            )
                            await hass.services.async_call(
                                DOMAIN,
                                SERVICE_RESTORE_NORMAL,
                                {
                                    "source": "force_timer",
                                    "_allow_monitoring_restore": True,
                                },
                                blocking=True,
                            )

                    force_charge_state["cancel_expiry_timer"] = (
                        async_track_point_in_utc_time(
                            hass, auto_restore_charge, expires_at
                        )
                    )

                    # Dispatch event for UI
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_force_charge_state",
                        {
                            "active": True,
                            "expires_at": expires_at.isoformat(),
                            "duration": int(remaining_minutes),
                        },
                    )
                    _LOGGER.info(
                        f"✅ Force charge restored from persistence, expires in {remaining_minutes:.1f} min"
                    )

                elif mode == "discharge":
                    force_discharge_state["active"] = True
                    force_discharge_state["expires_at"] = expires_at
                    force_discharge_state["hardware_expires_at"] = hardware_expires_at
                    force_discharge_state["duration"] = persisted_force_state.get(
                        "duration", int(remaining_minutes)
                    )
                    force_discharge_state["power_w"] = persisted_power_w
                    force_discharge_state["battery_discharge_w"] = (
                        persisted_battery_discharge_w
                    )
                    force_discharge_state["source"] = persisted_force_state.get(
                        "source", "user"
                    )
                    force_discharge_state["saved_tariff"] = (
                        _select_restorable_tesla_tariff(
                            persisted_force_state.get("saved_tariff"),
                            _cached_restorable_tesla_tariff(),
                            _configured_restorable_tesla_tariff(),
                        )
                    )
                    force_discharge_state["saved_operation_mode"] = (
                        persisted_force_state.get("saved_operation_mode")
                    )
                    force_discharge_state["saved_backup_reserve"] = (
                        persisted_force_state.get("saved_backup_reserve")
                    )
                    force_discharge_state["saved_export_rule"] = (
                        persisted_force_state.get("saved_export_rule")
                    )
                    force_discharge_state["saved_grid_charging_enabled"] = (
                        persisted_force_state.get("saved_grid_charging_enabled")
                    )

                    # Re-issue the discharge command to the inverter.
                    # After restart, the inverter reverts to normal mode —
                    # the state flag alone doesn't resume hardware discharge.
                    if not await _wait_for_native_battery_control_ready(
                        "Persisted force discharge replay"
                    ):
                        return
                    replay_remaining_seconds = (
                        expires_at - dt_util.utcnow()
                    ).total_seconds()
                    if replay_remaining_seconds <= 0:
                        _LOGGER.info(
                            "Persisted force discharge expired while waiting "
                            "for the native battery integration; restoring "
                            "normal operation instead of replaying it"
                        )
                        if await _restore_persisted_normal(
                            {
                                "source": "force_timer",
                                "_allow_monitoring_restore": True,
                            }
                        ):
                            stored_data = await store.async_load() or {}
                            stored_data["force_mode_state"] = None
                            await store.async_save(stored_data)
                        return
                    try:
                        remaining_min = max(
                            1,
                            math.ceil(replay_remaining_seconds / 60),
                        )
                        service_data = {"duration": remaining_min}
                        if persisted_power_w > 0:
                            service_data["power_w"] = persisted_power_w
                        await hass.services.async_call(
                            DOMAIN,
                            SERVICE_FORCE_DISCHARGE,
                            service_data,
                            blocking=True,
                        )
                        _LOGGER.info(
                            "🔋 Re-issued force discharge command after restart (%d min, %dW)",
                            remaining_min,
                            persisted_power_w,
                        )
                    except Exception as e:
                        _LOGGER.error(
                            "Failed to re-issue force discharge after restart: %s", e
                        )

                    # Re-setup expiry timer
                    _restore_gen_discharge_persisted = _command_generation[0]

                    async def auto_restore_discharge(_now):
                        if _command_generation[0] != _restore_gen_discharge_persisted:
                            _LOGGER.debug(
                                "Persisted force discharge timer superseded — skipping restore"
                            )
                            return
                        if force_discharge_state["active"]:
                            _LOGGER.info(
                                "⏰ Force discharge expired (restored timer), auto-restoring"
                            )
                            await hass.services.async_call(
                                DOMAIN,
                                SERVICE_RESTORE_NORMAL,
                                {
                                    "source": "force_timer",
                                    "_allow_monitoring_restore": True,
                                },
                                blocking=True,
                            )

                    force_discharge_state["cancel_expiry_timer"] = (
                        async_track_point_in_utc_time(
                            hass, auto_restore_discharge, expires_at
                        )
                    )

                    # Dispatch event for UI
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_force_discharge_state",
                        {
                            "active": True,
                            "expires_at": expires_at.isoformat(),
                            "duration": int(remaining_minutes),
                        },
                    )
                    _LOGGER.info(
                        f"✅ Force discharge restored from persistence, expires in {remaining_minutes:.1f} min"
                    )

        except Exception as e:
            _LOGGER.error(
                f"Error restoring force mode from persistence: {e}", exc_info=True
            )
            if persisted_force_state.get("source") == "optimizer":
                _LOGGER.warning(
                    "Optimizer startup cleanup remains pending after an "
                    "unexpected restore error"
                )

    # NOTE: restore_force_mode_from_persistence is scheduled AFTER handle_restore_normal
    # is defined (see below), because it needs handle_restore_normal in its closure.

    async def _tesla_force_read_operation_mode(
        session,
        api_base: str,
        site_id: str,
        headers: dict[str, str],
    ) -> str | None:
        try:
            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.warning(
                        "Tesla force-mode operation readback failed for site %s: %s - %s",
                        site_id,
                        response.status,
                        text[:200],
                    )
                    return None
                data = await response.json()
                return data.get("response", {}).get("default_real_mode")
        except Exception as err:
            _LOGGER.warning(
                "Tesla force-mode operation readback error for site %s: %s",
                site_id,
                err,
            )
            return None

    async def _tesla_force_confirm_operation_mode(
        session,
        api_base: str,
        site_id: str,
        headers: dict[str, str],
        expected_mode: str,
        *,
        attempts: int = 4,
        delay_seconds: float = 2.0,
    ) -> bool:
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await asyncio.sleep(delay_seconds)
            observed_mode = await _tesla_force_read_operation_mode(
                session,
                api_base,
                site_id,
                headers,
            )
            if observed_mode == expected_mode:
                _LOGGER.info(
                    "Confirmed Tesla force-mode operation %s for site %s (attempt %d/%d)",
                    expected_mode,
                    site_id,
                    attempt,
                    attempts,
                )
                return True
            _LOGGER.warning(
                "Tesla force-mode operation readback for site %s is %s, expected %s (attempt %d/%d)",
                site_id,
                observed_mode,
                expected_mode,
                attempt,
                attempts,
            )
        return False

    async def _tesla_force_read_backup_reserve(
        session,
        api_base: str,
        site_id: str,
        headers: dict[str, str],
    ) -> float | None:
        """Read one Tesla site's effective backup reserve from site_info."""
        try:
            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.warning(
                        "Tesla backup-reserve readback failed for site %s: %s - %s",
                        site_id,
                        response.status,
                        text[:200],
                    )
                    return None
                data = await response.json()
                observed = data.get("response", {}).get("backup_reserve_percent")
                if isinstance(observed, bool):
                    return None
                try:
                    return float(observed)
                except (TypeError, ValueError):
                    return None
        except Exception as err:
            _LOGGER.warning(
                "Tesla backup-reserve readback error for site %s: %s",
                site_id,
                err,
            )
            return None

    async def _tesla_force_confirm_backup_reserve(
        session,
        api_base: str,
        site_id: str,
        headers: dict[str, str],
        expected_percent: int,
        *,
        attempts: int = 4,
        delay_seconds: float = 2.0,
        is_current: Callable[[], bool] | None = None,
    ) -> bool:
        """Confirm an accepted Tesla reserve write reached site_info."""
        for attempt in range(1, attempts + 1):
            if is_current is not None and not is_current():
                _LOGGER.info(
                    "Tesla backup-reserve readback for site %s was superseded",
                    site_id,
                )
                return False
            if attempt > 1:
                await asyncio.sleep(delay_seconds)
                if is_current is not None and not is_current():
                    _LOGGER.info(
                        "Tesla backup-reserve readback for site %s was "
                        "superseded while waiting",
                        site_id,
                    )
                    return False
            observed = await _tesla_force_read_backup_reserve(
                session,
                api_base,
                site_id,
                headers,
            )
            if observed is not None and abs(observed - expected_percent) <= 0.5:
                if is_current is not None and not is_current():
                    _LOGGER.info(
                        "Tesla backup-reserve readback match for site %s was "
                        "superseded",
                        site_id,
                    )
                    return False
                _LOGGER.info(
                    "Confirmed Tesla backup reserve %.1f%% for site %s (attempt %d/%d)",
                    observed,
                    site_id,
                    attempt,
                    attempts,
                )
                return True
            _LOGGER.warning(
                "Tesla backup-reserve readback for site %s is %s, expected %d%% "
                "(attempt %d/%d)",
                site_id,
                observed,
                expected_percent,
                attempt,
                attempts,
            )
        return False

    async def _tesla_force_set_operation_mode(
        session,
        api_base: str,
        site_id: str,
        headers: dict[str, str],
        mode: str,
        *,
        reason: str,
        max_attempts: int = 3,
        guard_write: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> bool:
        from .coordinator import _parse_retry_after

        retry_after_delay: float | None = None
        last_status: int | None = None
        last_text = ""

        async def _authorize_operation_mode_attempt() -> bool:
            return True

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                wait_time = retry_after_delay or (2 ** (attempt - 1))
                retry_after_delay = None
                await asyncio.sleep(wait_time)

            if not await _run_optional_write_guard(
                _authorize_operation_mode_attempt,
                guard_write,
            ):
                _LOGGER.warning(
                    "Tesla %s operation mode attempt %d/%d blocked by control guard for site %s",
                    reason,
                    attempt,
                    max_attempts,
                    site_id,
                )
                return False

            try:
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": mode},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    last_status = response.status
                    last_text = await response.text()
                    if response.status == 200:
                        _LOGGER.info(
                            "Tesla %s set operation mode to %s for site %s",
                            reason,
                            mode,
                            site_id,
                        )
                        if await _tesla_force_confirm_operation_mode(
                            session,
                            api_base,
                            site_id,
                            headers,
                            mode,
                        ):
                            return True
                        _LOGGER.warning(
                            "Tesla %s operation mode %s was accepted but readback did not verify for site %s",
                            reason,
                            mode,
                            site_id,
                        )
                        continue

                    if response.status in (429, 500, 502, 503, 504):
                        retry_after_delay = _parse_retry_after(response)
                        _LOGGER.warning(
                            "Tesla %s operation mode attempt %d/%d failed for site %s: %s - %s",
                            reason,
                            attempt,
                            max_attempts,
                            site_id,
                            response.status,
                            last_text[:200],
                        )
                        continue

                    _LOGGER.warning(
                        "Tesla %s operation mode failed for site %s: %s - %s",
                        reason,
                        site_id,
                        response.status,
                        last_text[:200],
                    )
                    return False
            except asyncio.TimeoutError:
                last_status = None
                last_text = "timeout"
                _LOGGER.warning(
                    "Tesla %s operation mode attempt %d/%d timed out for site %s",
                    reason,
                    attempt,
                    max_attempts,
                    site_id,
                )
            except aiohttp.ClientError as err:
                last_status = None
                last_text = str(err)
                _LOGGER.warning(
                    "Tesla %s operation mode attempt %d/%d network error for site %s: %s",
                    reason,
                    attempt,
                    max_attempts,
                    site_id,
                    err,
                )

        _LOGGER.error(
            "Tesla %s operation mode %s failed for site %s after %d attempts: %s - %s",
            reason,
            mode,
            site_id,
            max_attempts,
            last_status,
            last_text[:200],
        )
        return False

    def _tesla_force_result_all_confirmed(
        result: dict[str, list[str]],
        site_configs: list[tuple[str, str, str]],
    ) -> bool:
        """Return whether every requested Tesla gateway confirmed a write."""
        expected = {site_id for site_id, _token, _provider in site_configs}
        return bool(expected) and expected.issubset(set(result["confirmed_sites"]))

    def _tesla_force_result_all_grid_field_absent_safe(
        result: dict[str, list[str]],
        site_configs: list[tuple[str, str, str]],
    ) -> bool:
        """Allow opted-in charge paths when Tesla omits only the readback field."""
        expected = {site_id for site_id, _token, _provider in site_configs}
        confirmed = set(result.get("confirmed_sites", []))
        field_absent = set(result.get("field_absent_sites", []))
        failed = set(result.get("failed_sites", []))
        return (
            bool(expected)
            and not expected.intersection(failed)
            and expected.issubset(confirmed.union(field_absent))
        )

    async def _tesla_force_apply_operation_mode(
        site_configs: list[tuple[str, str, str]],
        mode: str,
        *,
        reason: str,
        prefer_local: bool = True,
        guard_write: Callable[[Callable[[], Any]], Any] | None = None,
    ) -> dict[str, list[str]]:
        """Apply Tesla operation mode locally first, then cloud-fallback per site."""
        from .const import CONF_POWERWALL_LOCAL_DIN
        from .powerwall_local.dispatch import dispatch_powerwall_write

        result: dict[str, list[str]] = {
            "confirmed_sites": [],
            "accepted_sites": [],
            "failed_sites": [],
        }
        remaining = list(site_configs)
        session = async_get_clientsession(hass)
        if prefer_local and remaining:
            site_id, current_token, provider = remaining.pop(0)
            din = entry.data.get(CONF_POWERWALL_LOCAL_DIN)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = get_tesla_api_base_url(
                provider,
                entry.data.get(CONF_FLEET_API_BASE_URL),
            )

            async def _local(transport) -> bool:
                if not din or not await transport.write_config(
                    din, {"default_real_mode": mode}
                ):
                    return False
                for attempt in range(1, 4):
                    if attempt > 1:
                        await asyncio.sleep(2)
                    config = await transport.read_config(din)
                    observed = (
                        config.get("default_real_mode")
                        if isinstance(config, dict)
                        else None
                    )
                    if observed == mode:
                        return True
                if site_id not in result["accepted_sites"]:
                    result["accepted_sites"].append(site_id)
                return False

            async def _cloud() -> bool:
                return await _tesla_force_set_operation_mode(
                    session,
                    api_base,
                    site_id,
                    headers,
                    mode,
                    reason=reason,
                    guard_write=guard_write,
                )

            async def _guarded_local(transport) -> bool:
                return await _run_optional_write_guard(
                    lambda: _local(transport),
                    guard_write,
                )

            if await dispatch_powerwall_write(
                hass,
                entry,
                local_call=_guarded_local,
                cloud_call=_cloud,
                label=f"{reason} operation mode",
                timeout=15.0,
                retry_local_once=False,
            ):
                result["confirmed_sites"].append(site_id)
                if site_id in result["accepted_sites"]:
                    result["accepted_sites"].remove(site_id)
            else:
                result["failed_sites"].append(site_id)

        for site_id, current_token, provider in remaining:
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = get_tesla_api_base_url(
                provider,
                entry.data.get(CONF_FLEET_API_BASE_URL),
            )

            async def _cloud_site() -> bool:
                return await _tesla_force_set_operation_mode(
                    session,
                    api_base,
                    site_id,
                    headers,
                    mode,
                    reason=reason,
                    guard_write=guard_write,
                )

            if await _cloud_site():
                result["confirmed_sites"].append(site_id)
                if site_id in result["accepted_sites"]:
                    result["accepted_sites"].remove(site_id)
            else:
                result["failed_sites"].append(site_id)
        return result

    async def _tesla_force_apply_grid_charging(
        site_configs: list[tuple[str, str, str]],
        enabled: bool,
        *,
        reason: str,
        prefer_local: bool = True,
        is_current: Callable[[], bool] | None = None,
    ) -> dict[str, list[str]]:
        """Apply Tesla grid charging locally first and require real readback."""
        from .const import CONF_POWERWALL_LOCAL_DIN
        from .powerwall_local.dispatch import dispatch_powerwall_write

        result: dict[str, list[str]] = {
            "confirmed_sites": [],
            "accepted_sites": [],
            "field_absent_sites": [],
            "failed_sites": [],
        }
        remaining = list(site_configs)
        session = async_get_clientsession(hass)

        async def _cloud_apply(
            site_id: str,
            current_token: str,
            provider: str,
        ) -> bool:
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = get_tesla_api_base_url(
                provider,
                entry.data.get(CONF_FLEET_API_BASE_URL),
            )
            outcome = await async_set_tesla_grid_charging_confirmed(
                session,
                api_base,
                site_id,
                headers,
                enabled,
                is_current=is_current,
            )
            if outcome.applied:
                return True
            if outcome.status is TeslaGridWriteStatus.ACCEPTED_FIELD_ABSENT:
                if site_id not in result["accepted_sites"]:
                    result["accepted_sites"].append(site_id)
                if site_id not in result["field_absent_sites"]:
                    result["field_absent_sites"].append(site_id)
                _LOGGER.info(
                    "Tesla %s grid charging %s accepted for site %s; "
                    "direct readback omitted the grid-charging field",
                    reason,
                    "enable" if enabled else "disable",
                    site_id,
                )
                return False
            if (
                outcome.status is TeslaGridWriteStatus.ACCEPTED_UNCONFIRMED
                and site_id not in result["accepted_sites"]
            ):
                result["accepted_sites"].append(site_id)
            _LOGGER.warning(
                "Tesla %s grid charging %s did not verify for site %s (%s%s)",
                reason,
                "enable" if enabled else "disable",
                site_id,
                outcome.status.value,
                f": {outcome.detail}" if outcome.detail else "",
            )
            return False

        if prefer_local and remaining:
            site_id, current_token, provider = remaining.pop(0)
            din = entry.data.get(CONF_POWERWALL_LOCAL_DIN)

            async def _local(transport) -> bool:
                if is_current is not None and not is_current():
                    return False
                if not din or not await transport.write_config(
                    din,
                    {
                        "site_info.disallow_charge_from_grid_with_solar_installed": not enabled
                    },
                ):
                    return False
                if site_id not in result["accepted_sites"]:
                    result["accepted_sites"].append(site_id)
                for attempt in range(1, 4):
                    if is_current is not None and not is_current():
                        return False
                    if attempt > 1:
                        await asyncio.sleep(2)
                    config = await transport.read_config(din)
                    site_info = (
                        ((config or {}).get("site_info") or {})
                        if isinstance(config, dict)
                        else {}
                    )
                    observed = tesla_grid_charging_enabled_from_site_info(site_info)
                    if observed is enabled:
                        return True
                return False

            async def _cloud() -> bool:
                return await _cloud_apply(site_id, current_token, provider)

            if await dispatch_powerwall_write(
                hass,
                entry,
                local_call=_local,
                cloud_call=_cloud,
                label=f"{reason} grid charging",
                timeout=15.0,
                retry_local_once=False,
            ):
                result["confirmed_sites"].append(site_id)
                if site_id in result["accepted_sites"]:
                    result["accepted_sites"].remove(site_id)
            else:
                if site_id not in result["field_absent_sites"]:
                    result["failed_sites"].append(site_id)

        for site_id, current_token, provider in remaining:
            if await _cloud_apply(site_id, current_token, provider):
                result["confirmed_sites"].append(site_id)
                if site_id in result["accepted_sites"]:
                    result["accepted_sites"].remove(site_id)
            else:
                if site_id not in result["field_absent_sites"]:
                    result["failed_sites"].append(site_id)
        return result

    async def _tesla_force_set_backup_reserve_cloud(
        session,
        site_id: str,
        current_token: str,
        provider: str,
        percent: int,
        *,
        reason: str,
    ) -> bool:
        """Set one Tesla site's reserve through Fleet API with bounded retries."""
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
        }
        api_base = get_tesla_api_base_url(
            provider,
            entry.data.get(CONF_FLEET_API_BASE_URL),
        )
        for attempt in range(1, 4):
            try:
                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/backup",
                    headers=headers,
                    json={"backup_reserve_percent": percent},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        if await _tesla_force_confirm_backup_reserve(
                            session,
                            api_base,
                            site_id,
                            headers,
                            percent,
                        ):
                            return True
                        _LOGGER.warning(
                            "Tesla %s reserve was accepted but readback did not "
                            "verify for site %s",
                            reason,
                            site_id,
                        )
                        continue
                    text = await response.text()
                    if response.status not in (429, 500, 502, 503, 504):
                        _LOGGER.warning(
                            "Tesla %s reserve failed for site %s: %s - %s",
                            reason,
                            site_id,
                            response.status,
                            text[:200],
                        )
                        return False
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning(
                    "Tesla %s reserve attempt %d/3 failed for site %s: %s",
                    reason,
                    attempt,
                    site_id,
                    err,
                )
            if attempt < 3:
                await asyncio.sleep(2**attempt)
        return False

    async def _tesla_force_apply_backup_reserve_unlocked(
        site_configs: list[tuple[str, str, str]],
        percent: int,
        *,
        reason: str,
        prefer_local: bool = True,
    ) -> dict[str, list[str]]:
        """Apply user-scale reserve through the independently verified cloud path.

        Local config uses a different reserve scale. Neither cached cloud/local
        pairs nor reading back our own converted payload establishes its offset.
        Until the gateway supplies that conversion authoritatively, fail closed
        on local reserve writes, including restore and optimizer callers.
        ``prefer_local`` is retained for compatibility with those callers.
        """
        result: dict[str, list[str]] = {
            "confirmed_sites": [],
            "accepted_sites": [],
            "failed_sites": [],
        }
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        for key in (
            "powerwall_local_low_soe_reserve_pct",
            "powerwall_local_backup_reserve_write_local_pct",
            "powerwall_local_backup_reserve_write_user_pct",
        ):
            entry_data.pop(key, None)
        coordinator = entry_data.get("tesla_coordinator")
        if coordinator is not None:
            coordinator.invalidate_site_info_cache()
        session = async_get_clientsession(hass)
        for site_id, current_token, provider in site_configs:
            if await _tesla_force_set_backup_reserve_cloud(
                session,
                site_id,
                current_token,
                provider,
                percent,
                reason=reason,
            ):
                result["confirmed_sites"].append(site_id)
            else:
                result["failed_sites"].append(site_id)
        return result

    async def _tesla_force_apply_backup_reserve(
        site_configs: list[tuple[str, str, str]],
        percent: int,
        *,
        reason: str,
        prefer_local: bool = True,
    ) -> dict[str, list[str]]:
        """Serialize an ordinary Tesla reserve write with dispatch nudges."""
        current_task = asyncio.current_task()
        if current_task is not None:
            _tesla_reserve_write_tasks.add(current_task)
        try:
            if _tesla_reserve_pulse_runtime.get("tesla_reserve_pulse_stopping"):
                return {
                    "confirmed_sites": [],
                    "accepted_sites": [],
                    "failed_sites": [
                        site_id for site_id, _token, _provider in site_configs
                    ],
                }
            async with _tesla_reserve_write_lock:
                if _tesla_reserve_pulse_runtime.get("tesla_reserve_pulse_stopping"):
                    return {
                        "confirmed_sites": [],
                        "accepted_sites": [],
                        "failed_sites": [
                            site_id for site_id, _token, _provider in site_configs
                        ],
                    }
                return await _tesla_force_apply_backup_reserve_unlocked(
                    site_configs,
                    percent,
                    reason=reason,
                    prefer_local=prefer_local,
                )
        finally:
            if current_task is not None:
                _tesla_reserve_write_tasks.discard(current_task)

    async def _tesla_force_pulse_backup_reserve(
        site_configs: list[tuple[str, str, str]],
        target_percent: int,
        *,
        reason: str,
        prefer_local: bool = True,
        is_current: Callable[[], bool] | None = None,
    ) -> dict[str, list[str]]:
        """Wake Tesla dispatch with a safe reserve reapply, then restore it.

        Tesla gateways can confirm a mode write while retaining their previous
        physical dispatch. A stronger reserve value, a short pause, and an
        exact restore makes the gateway re-evaluate dispatch without weakening
        the user's configured protection. At 100%, where no stronger valid
        reserve exists, the exact target is confirmed both before and after the
        pause. The temporary value stays inside this internal primitive, so it
        never changes the user's persisted reserve setting.
        """
        target_percent = _normalize_tesla_backup_reserve_percent(target_percent)
        pulse_percent = _tesla_backup_reserve_pulse_percent(target_percent)
        site_ids = [site_id for site_id, _token, _provider in site_configs]

        def _failed_result() -> dict[str, list[str]]:
            return {
                "confirmed_sites": [],
                "accepted_sites": [],
                "failed_sites": list(site_ids),
            }

        current_task = asyncio.current_task()
        if current_task is not None:
            _tesla_reserve_write_tasks.add(current_task)

        async def _restore_exact_target() -> dict[str, list[str]]:
            """Restore the authoritative target with one bounded retry."""
            final_result = _failed_result()
            for attempt in range(1, 3):
                final_result = await _tesla_force_apply_backup_reserve_unlocked(
                    site_configs,
                    target_percent,
                    reason=f"{reason} restore {target_percent}%",
                    prefer_local=prefer_local,
                )
                if _tesla_force_result_all_confirmed(
                    final_result,
                    site_configs,
                ):
                    return final_result
                if attempt < 2:
                    _LOGGER.warning(
                        "Tesla %s exact reserve restore did not verify; retrying",
                        reason,
                    )
                    await asyncio.sleep(1)
            return final_result

        async def _finish_restore_despite_cancellation() -> dict[str, list[str]]:
            """Drain the exact restore before propagating task cancellation."""
            restore_task = asyncio.create_task(_restore_exact_target())
            cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    final_result = await asyncio.shield(restore_task)
                    break
                except asyncio.CancelledError as err:
                    cancellation = err
                    continue
            if cancellation is not None:
                raise cancellation
            return final_result

        try:
            if _tesla_reserve_pulse_runtime.get("tesla_reserve_pulse_stopping"):
                _LOGGER.info("Tesla %s reserve pulse skipped during unload", reason)
                return _failed_result()
            if is_current is not None and not is_current():
                _LOGGER.info("Tesla %s reserve pulse superseded before start", reason)
                return _failed_result()

            async with _tesla_reserve_write_lock:
                if is_current is not None and not is_current():
                    _LOGGER.info(
                        "Tesla %s reserve pulse superseded while waiting to start",
                        reason,
                    )
                    return _failed_result()

                pulse_result: dict[str, list[str]] = _failed_result()
                final_result: dict[str, list[str]] = _failed_result()
                try:
                    first_percent = (
                        target_percent if pulse_percent is None else pulse_percent
                    )
                    first_reason = (
                        f"{reason} safe reapply {target_percent}%"
                        if pulse_percent is None
                        else f"{reason} temporary {pulse_percent}%"
                    )
                    pulse_result = await _tesla_force_apply_backup_reserve_unlocked(
                        site_configs,
                        first_percent,
                        reason=first_reason,
                        prefer_local=prefer_local,
                    )
                    if not _tesla_force_result_all_confirmed(
                        pulse_result,
                        site_configs,
                    ):
                        _LOGGER.warning(
                            "Tesla %s initial reserve write did not verify for "
                            "every site; the exact target will still be restored",
                            reason,
                        )
                    await asyncio.sleep(3)
                finally:
                    final_result = await _finish_restore_despite_cancellation()

                if is_current is not None and not is_current():
                    _LOGGER.info(
                        "Tesla %s reserve pulse superseded after exact restore",
                        reason,
                    )
                    return _failed_result()

                pulse_confirmed = set(pulse_result["confirmed_sites"])
                final_confirmed = set(final_result["confirmed_sites"])
                confirmed_sites = [
                    site_id
                    for site_id in site_ids
                    if site_id in pulse_confirmed and site_id in final_confirmed
                ]
                return {
                    "confirmed_sites": confirmed_sites,
                    "accepted_sites": sorted(
                        set(pulse_result["accepted_sites"])
                        | set(final_result["accepted_sites"])
                    ),
                    "failed_sites": [
                        site_id
                        for site_id in site_ids
                        if site_id not in confirmed_sites
                    ],
                }
        finally:
            if current_task is not None:
                _tesla_reserve_write_tasks.discard(current_task)

    def _tesla_force_retry_expiry(
        state: dict,
        requested_hardware_expiry: datetime,
        *,
        degraded: bool,
    ) -> datetime:
        """Bound failed/unconfirmed force writes without creating a new scheduler."""
        if not degraded:
            state["apply_retry_count"] = 0
            return requested_hardware_expiry.astimezone(dt_util.UTC)
        retry_count = min(int(state.get("apply_retry_count") or 0) + 1, 4)
        state["apply_retry_count"] = retry_count
        retry_seconds = min(120 * (2 ** (retry_count - 1)), 900)
        return min(
            requested_hardware_expiry.astimezone(dt_util.UTC),
            (dt_util.utcnow() + timedelta(seconds=retry_seconds)).astimezone(
                dt_util.UTC
            ),
        )

    async def _commit_solaredge_force_transition(
        direction: str,
        duration: int,
        power_w: float,
        source: str,
        solaredge_coord: Any,
        write: Callable[[], Any],
    ) -> dict[str, Any]:
        """Publish a SolarEdge force mode only after its write is confirmed.

        A current force timer remains the only cleanup path while the next
        SolarEdge transition is awaiting confirmation.  In particular, a
        rejected or uncertain write must not strand the previous local state.
        """
        try:
            confirmed = await write()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "SolarEdge force %s failed before local lifecycle commit: %s",
                direction,
                err,
            )
            raise HomeAssistantError(
                f"SolarEdge force {direction} failed; check control health"
            ) from err
        if not confirmed:
            raise HomeAssistantError(
                f"SolarEdge force {direction} was not confirmed; "
                "check control health before retrying"
            )

        _cancel_all_force_timers(f"confirmed SolarEdge force_{direction} command")
        _command_generation[0] += 1
        restore_generation = _command_generation[0]
        if self_consumption_state.get("active"):
            _clear_self_consumption_state()

        state = force_charge_state if direction == "charge" else force_discharge_state
        previous_state = (
            force_discharge_state if direction == "charge" else force_charge_state
        )
        previous_direction = "discharge" if direction == "charge" else "charge"
        was_previous_active = bool(previous_state.get("active"))
        previous_state["active"] = False
        previous_state["expires_at"] = None
        state["active"] = True
        state["source"] = source
        state["duration"] = duration
        state["power_w"] = power_w
        state["expires_at"] = dt_util.utcnow() + timedelta(minutes=duration)
        _LOGGER.info(
            "SolarEdge FORCE %s ACTIVE for %d minutes (power_w=%s)",
            direction.upper(),
            duration,
            power_w,
        )

        if was_previous_active:
            async_dispatcher_send(
                hass,
                f"{DOMAIN}_force_{previous_direction}_state",
                {"active": False, "expires_at": None, "duration": 0},
            )
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_force_{direction}_state",
            {
                "active": True,
                "expires_at": state["expires_at"].isoformat(),
                "duration": duration,
            },
        )

        controller_generation = solaredge_coord.intent_generation

        async def auto_restore_solaredge(_now):
            if _command_generation[0] != restore_generation:
                _LOGGER.debug(
                    "SolarEdge force %s timer superseded — skipping restore", direction
                )
                return
            if state["active"]:
                _LOGGER.info("SolarEdge force %s expired, auto-restoring", direction)
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_RESTORE_NORMAL,
                    {
                        "source": "force_timer",
                        "_allow_monitoring_restore": True,
                        "_solaredge_generation": controller_generation,
                    },
                    blocking=True,
                )

        state["cancel_expiry_timer"] = async_track_point_in_utc_time(
            hass, auto_restore_solaredge, state["expires_at"]
        )
        try:
            await persist_force_mode_state()
        except Exception:
            _LOGGER.exception(
                "SolarEdge force %s is active but its restart state could not be persisted",
                direction,
            )
            return {
                "success": True,
                "warning": (
                    f"Force {direction} is active, but its restart state could not be saved."
                ),
            }
        return {"success": True}

    async def handle_force_discharge(call: ServiceCall) -> dict[str, Any] | None:
        """Force discharge mode - switches to autonomous with high export tariff."""

        # Warn if calibration suspected (don't block — user manual command)
        _fd_entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        if _fd_entry_data.get("calibration_suspected"):
            _LOGGER.warning(
                "Force discharge called during suspected calibration — command may not stick"
            )

        # Log call context for debugging (helps identify if called by automation)
        context = call.context
        _LOGGER.info(
            f"🔋 Force discharge service called (context: user_id={context.user_id}, parent_id={context.parent_id})"
        )

        raw_duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        _LOGGER.debug(
            f"Force discharge raw duration from call.data: {raw_duration!r} (type: {type(raw_duration).__name__})"
        )
        source = _control_call_source(call)
        command_power_w = _resolve_force_command_power_w(
            "discharge",
            call.data.get("power_w", 0),
        )
        requested_export_power_w = command_power_w
        requested_battery_discharge_w = (
            _coerce_force_power_w(call.data.get("battery_discharge_w", 0))
            if source == "optimizer"
            else 0
        )
        solax_home_discharge_w = max(
            0,
            requested_battery_discharge_w - requested_export_power_w,
        )
        raw_tariff_duration = call.data.get("_tariff_duration")

        # Convert to int if string (from HA service selector or button-card)
        try:
            duration = int(raw_duration)
        except (ValueError, TypeError):
            _LOGGER.warning(
                f"Could not convert duration {raw_duration!r} to int, using default {DEFAULT_DISCHARGE_DURATION}"
            )
            duration = DEFAULT_DISCHARGE_DURATION
        if source == "optimizer":
            if not (1 <= duration <= 1440):
                _LOGGER.warning(
                    "Optimizer force discharge duration %s out of range, using default %d",
                    duration,
                    DEFAULT_DISCHARGE_DURATION,
                )
                duration = DEFAULT_DISCHARGE_DURATION
        elif duration not in DISCHARGE_DURATIONS:
            _LOGGER.warning(
                f"Duration {duration} not in allowed values {DISCHARGE_DURATIONS}, using default {DEFAULT_DISCHARGE_DURATION}"
            )
            duration = DEFAULT_DISCHARGE_DURATION

        tariff_duration = duration
        if source == "optimizer" and raw_tariff_duration is not None:
            try:
                parsed_tariff_duration = int(raw_tariff_duration)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Optimizer force discharge tariff duration %r invalid; using %d",
                    raw_tariff_duration,
                    duration,
                )
            else:
                if duration <= parsed_tariff_duration <= 1440:
                    tariff_duration = parsed_tariff_duration
                else:
                    _LOGGER.warning(
                        "Optimizer force discharge tariff duration %s out of range; using %d",
                        parsed_tariff_duration,
                        duration,
                    )

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would force discharge for %d minutes (source=%s, power_w=%s) — blocked by monitoring mode",
                duration,
                source,
                command_power_w,
            )
            if source == "optimizer":
                raise HomeAssistantError(
                    "Optimizer force discharge blocked by monitoring mode"
                )
            return

        _LOGGER.info(
            f"🔋 FORCE DISCHARGE: Activating for {duration} minutes (source={source})"
        )

        tesla_preflight_sites = _get_tesla_site_configs(hass, entry)
        if tesla_preflight_sites and not force_discharge_state.get("active"):
            inherited_grid_charging = None
            transitioning_from_force_charge = force_charge_state.get("active")
            if transitioning_from_force_charge:
                inherited_grid_charging = _optional_bool(
                    force_charge_state.get("saved_grid_charging_enabled")
                )
            await _require_tesla_force_grid_charging_baselines(
                tesla_preflight_sites,
                inherited_grid_charging,
                inheritance_required=bool(transitioning_from_force_charge),
            )

        # Cancel any pending expiry timers and advance the generation counter
        # synchronously — before any await — so that a queued restore callback
        # from a previous command cannot fire during this command's I/O window.
        _cancel_all_force_timers("new force_discharge command")
        _command_generation[0] += 1
        _restore_gen = _command_generation[0]
        _reserve_gen = _tesla_reserve_generation[0]
        force_discharge_state["_skip_backup_reserve_restore"] = False
        if self_consumption_state.get("active"):
            _clear_self_consumption_state()

        # Mutual exclusion + baseline inheritance: if a force_charge is
        # active when this command lands, we transition directly without
        # re-saving the current (charge-modified) state as the baseline.
        # Instead we inherit force_charge's saved_* values so that the
        # eventual auto-restore reverts to the TRUE pre-force state rather
        # than the intermediate force_charge state. This also flips
        # was_already_force_discharging to True so the Tesla save-baseline
        # block further down is skipped.
        _transitioning_from_force_charge = force_charge_state.get("active", False)
        if _transitioning_from_force_charge:
            _LOGGER.info(
                "force_discharge: transitioning from active force_charge, "
                "inheriting saved baseline (tariff/mode/reserve)"
            )
            force_discharge_state["saved_tariff"] = force_charge_state.get(
                "saved_tariff"
            )
            force_discharge_state["saved_operation_mode"] = force_charge_state.get(
                "saved_operation_mode"
            )
            force_discharge_state["saved_backup_reserve"] = force_charge_state.get(
                "saved_backup_reserve"
            )
            force_discharge_state["saved_grid_charging_enabled"] = (
                force_charge_state.get("saved_grid_charging_enabled")
            )
            # saved_export_rule is left as-is; force_charge doesn't modify
            # the export rule, so any pre-existing value on
            # force_discharge_state is still accurate. If it's None (fresh
            # discharge), auto-restore will skip the export rule which is
            # the same no-op behavior as before.
            force_charge_state["active"] = False
            force_charge_state["saved_tariff"] = None
            force_charge_state["saved_operation_mode"] = None
            force_charge_state["saved_backup_reserve"] = None
            force_charge_state["saved_grid_charging_enabled"] = None
            force_charge_state["expires_at"] = None

        # Set force discharge state IMMEDIATELY so the optimizer sees it
        # before the async Modbus/API call completes (same race fix as force_charge).
        # Treat a force_charge transition as "already force discharging" so
        # the save-baseline block is skipped (otherwise it'd overwrite the
        # saved_* fields we just inherited with the current force_charge state).
        was_already_force_discharging = (
            force_discharge_state.get("active", False)
            or _transitioning_from_force_charge
        )
        force_discharge_state["active"] = True
        force_discharge_state["source"] = source
        force_discharge_state["duration"] = duration
        force_discharge_state["power_w"] = command_power_w
        force_discharge_state["battery_discharge_w"] = (
            solax_home_discharge_w + command_power_w
            if requested_battery_discharge_w > 0
            else 0
        )
        force_discharge_state.pop("command_status", None)

        tesla_force_discharge_mutated = False
        try:
            # Get Tesla gateway config
            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                force_discharge_state["active"] = False
                _LOGGER.error("Missing Tesla site ID or token for force discharge")
                return {"success": False, "error": "missing Tesla site configuration"}

            session = async_get_clientsession(hass)
            saved_states = force_discharge_state.get("saved_states") or {}

            async def _cleanup_failed_tesla_force_discharge(
                reason: str,
                *,
                preserve_newer_reserve: bool = False,
            ) -> None:
                """Restore Tesla settings after any partially applied discharge."""
                force_discharge_state["_skip_backup_reserve_restore"] = (
                    preserve_newer_reserve
                )
                retry_expiry = dt_util.utcnow() + timedelta(minutes=15)
                force_discharge_state["active"] = True
                force_discharge_state["source"] = source
                force_discharge_state["expires_at"] = dt_util.utcnow() + timedelta(
                    minutes=2
                )
                force_discharge_state["hardware_expires_at"] = (
                    _tesla_force_retry_expiry(
                        force_discharge_state,
                        retry_expiry,
                        degraded=True,
                    )
                )
                _LOGGER.warning(
                    "Force discharge partially applied; restoring saved Tesla state (%s)",
                    reason,
                )
                try:
                    await persist_force_mode_state()
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RESTORE_NORMAL,
                        {
                            "source": "force_cleanup",
                            "_allow_monitoring_restore": True,
                            "_skip_backup_reserve_restore": preserve_newer_reserve,
                        },
                        blocking=True,
                    )
                except Exception as err:
                    force_discharge_state["active"] = True
                    _LOGGER.error(
                        "Immediate Tesla force-discharge cleanup failed; state remains armed: %s",
                        err,
                    )
                    await persist_force_mode_state()

            # Step 1: Save current tariff and state (if not already in discharge mode)
            if not was_already_force_discharging:
                # Initialize per-site saved states dict
                saved_states = {}
                for site_id, current_token, provider in site_configs:
                    headers = {
                        "Authorization": f"Bearer {current_token}",
                        "Content-Type": "application/json",
                    }
                    api_base = get_tesla_api_base_url(
                        provider, entry.data.get(CONF_FLEET_API_BASE_URL)
                    )
                    site_state = {}

                    _LOGGER.info(
                        "Saving current tariff before force discharge for site %s...",
                        site_id,
                    )
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/tariff_rate",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            resp = data.get("response", {})
                            tariff_candidate = _extract_tesla_tariff_content(resp)
                            saved_tariff = await _cache_restorable_tesla_tariff(
                                tariff_candidate,
                                f"tariff_rate for site {site_id}",
                            )
                            site_state["saved_tariff"] = saved_tariff
                            if saved_tariff:
                                _LOGGER.info(
                                    "Saved tariff for site %s (name: %s)",
                                    site_id,
                                    saved_tariff.get("name", "unknown"),
                                )
                            elif tariff_candidate:
                                _LOGGER.warning(
                                    "Current tariff for site %s is a Tesla v1r force tariff; not saving as restore tariff",
                                    site_id,
                                )
                            else:
                                _LOGGER.warning(
                                    "Could not extract tariff from tariff_rate for site %s - will try site_info",
                                    site_id,
                                )
                        else:
                            _LOGGER.warning(
                                "tariff_rate endpoint returned %s for site %s - will try site_info fallback",
                                response.status,
                                site_id,
                            )

                    # Get and save current operation mode, backup reserve, and tariff (fallback)
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            site_info = data.get("response", {})
                            site_state["saved_operation_mode"] = site_info.get(
                                "default_real_mode"
                            )
                            # Determine the user's real backup reserve to restore later.
                            # Priority: 1) optimizer's startup reserve (persisted from first boot — authoritative)
                            #           2) optimizer's pre-IDLE value (if IDLE elevated it)
                            #           3) Tesla API value (ONLY if not 100% — force charge sets it to 100%)
                            #           4) Default to 0% (safe — won't force charge)
                            api_reserve = site_info.get("backup_reserve_percent")
                            opt_coord = (
                                hass.data.get(DOMAIN, {})
                                .get(entry.entry_id, {})
                                .get("optimization_coordinator")
                            )
                            startup_reserve = (
                                getattr(opt_coord, "_startup_backup_reserve", None)
                                if opt_coord
                                else None
                            )
                            pre_idle = (
                                getattr(opt_coord, "_pre_idle_backup_reserve", None)
                                if opt_coord
                                else None
                            )
                            if startup_reserve is not None:
                                site_state["saved_backup_reserve"] = startup_reserve
                            elif pre_idle is not None:
                                site_state["saved_backup_reserve"] = pre_idle
                            else:
                                resolved_reserve = None
                                if opt_coord is not None and hasattr(
                                    opt_coord, "resolve_restore_target"
                                ):
                                    resolved_reserve = (
                                        await opt_coord.resolve_restore_target()
                                    )
                                if resolved_reserve is not None:
                                    site_state["saved_backup_reserve"] = (
                                        resolved_reserve
                                    )
                                elif api_reserve is not None and api_reserve < 100:
                                    site_state["saved_backup_reserve"] = api_reserve
                                else:
                                    site_state["saved_backup_reserve"] = 0
                            _LOGGER.info(
                                "Site %s: saved operation mode: %s, backup reserve: %s%% (api=%s%%, pre_idle=%s%%, startup=%s%%)",
                                site_id,
                                site_state["saved_operation_mode"],
                                site_state["saved_backup_reserve"],
                                api_reserve,
                                pre_idle,
                                startup_reserve,
                            )

                            components = site_info.get("components", {})
                            saved_export_rule = components.get(
                                "customer_preferred_export_rule"
                            )
                            if saved_export_rule is None:
                                non_export = components.get(
                                    "non_export_configured", False
                                )
                                saved_export_rule = (
                                    "never" if non_export else "battery_ok"
                                )
                            site_state["saved_export_rule"] = saved_export_rule
                            _LOGGER.info(
                                "Site %s: saved export rule: %s",
                                site_id,
                                saved_export_rule,
                            )

                            saved_grid_charging_enabled = (
                                _tesla_grid_charging_enabled_from_site_info(site_info)
                            )
                            saved_grid_charging_enabled = (
                                _remember_tesla_grid_charging_preference(
                                    site_id,
                                    saved_grid_charging_enabled,
                                )
                            )
                            site_state["saved_grid_charging_enabled"] = (
                                saved_grid_charging_enabled
                            )
                            _LOGGER.info(
                                "Site %s: saved grid charging: %s",
                                site_id,
                                saved_grid_charging_enabled,
                            )

                            # site_info is the documented Fleet API source and is
                            # authoritative for retailer-linked rate plans. The
                            # legacy tariff_rate endpoint can lag after an in-app
                            # provider import, so a valid site_info tariff must
                            # replace that fallback snapshot.
                            site_tariff = _extract_tesla_tariff_content(site_info)
                            site_info_saved_tariff = (
                                await _cache_restorable_tesla_tariff(
                                    site_tariff,
                                    f"site_info for site {site_id}",
                                )
                            )
                            if site_info_saved_tariff:
                                if (
                                    site_state.get("saved_tariff")
                                    and site_state["saved_tariff"]
                                    != site_info_saved_tariff
                                ):
                                    _LOGGER.info(
                                        "Replacing tariff_rate snapshot with authoritative "
                                        "site_info tariff for site %s",
                                        site_id,
                                    )
                                site_state["saved_tariff"] = site_info_saved_tariff
                                _LOGGER.info(
                                    "Saved authoritative tariff from site_info for site %s (name: %s)",
                                    site_id,
                                    site_info_saved_tariff.get("name", "unknown"),
                                )
                            elif not site_state.get("saved_tariff"):
                                if _cached_restorable_tesla_tariff():
                                    site_state["saved_tariff"] = (
                                        _cached_restorable_tesla_tariff()
                                    )
                                    _LOGGER.info(
                                        "Using cached Tesla tariff baseline for site %s (name: %s)",
                                        site_id,
                                        _tariff_display_name(
                                            site_state["saved_tariff"]
                                        ),
                                    )
                                else:
                                    _LOGGER.warning(
                                        "No tariff found in site_info either for site %s",
                                        site_id,
                                    )
                                    electricity_provider = entry.options.get(
                                        CONF_ELECTRICITY_PROVIDER,
                                        entry.data.get(
                                            CONF_ELECTRICITY_PROVIDER, "amber"
                                        ),
                                    )
                                    if electricity_provider == "globird":
                                        try:
                                            from .automations.actions import (
                                                _send_expo_push,
                                            )

                                            await _send_expo_push(
                                                hass,
                                                "Battery Warning",
                                                "Tariff not saved - may need reconfiguration",
                                            )
                                        except Exception as notify_err:
                                            _LOGGER.debug(
                                                f"Could not send notification: {notify_err}"
                                            )

                    # Always reapply battery_ok. Tesla/Fleet readback can report
                    # the intended export rule while the gateway still needs a
                    # fresh write before it allows battery export.
                    tesla_force_discharge_mutated = True
                    _LOGGER.info(
                        "Setting export rule to battery_ok for site %s...", site_id
                    )
                    async with session.post(
                        f"{api_base}/api/1/energy_sites/{site_id}/grid_import_export",
                        headers=headers,
                        json={"customer_preferred_export_rule": "battery_ok"},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            _LOGGER.info(
                                "Set export rule to battery_ok for site %s", site_id
                            )
                            try:
                                await update_cached_export_rule("battery_ok")
                            except Exception as cache_err:
                                _LOGGER.debug(
                                    "Could not cache force-discharge export rule: %s",
                                    cache_err,
                                )
                        else:
                            _LOGGER.warning(
                                "Could not set export rule for site %s: %s",
                                site_id,
                                response.status,
                            )

                    saved_states[site_id] = site_state

                    # Small delay between gateways to avoid rate limiting
                    if len(site_configs) > 1:
                        await asyncio.sleep(1)

                # Store per-site saved states
                force_discharge_state["saved_states"] = saved_states
                # Keep backward-compat keys from primary for restore_normal
                primary_state = saved_states.get(site_configs[0][0], {})
                force_discharge_state["saved_tariff"] = primary_state.get(
                    "saved_tariff"
                )
                force_discharge_state["saved_operation_mode"] = primary_state.get(
                    "saved_operation_mode"
                )
                force_discharge_state["saved_backup_reserve"] = primary_state.get(
                    "saved_backup_reserve"
                )
                force_discharge_state["saved_export_rule"] = primary_state.get(
                    "saved_export_rule"
                )
                force_discharge_state["saved_grid_charging_enabled"] = (
                    primary_state.get("saved_grid_charging_enabled")
                )

            # Force discharge is tariff-driven on Tesla. Grid charging must be
            # confirmed disabled before the high-export tariff is uploaded, or
            # the gateway could import and charge during the force window.
            tesla_force_discharge_mutated = True
            grid_result = await _tesla_force_apply_grid_charging(
                site_configs,
                False,
                reason="force discharge",
                is_current=lambda: _command_generation[0] == _restore_gen,
            )
            if not _tesla_force_result_all_confirmed(grid_result, site_configs):
                raise HomeAssistantError(
                    "Could not verify Tesla grid charging was disabled before force discharge"
                )

            # Step 3: Switch to autonomous mode locally first, with cloud
            # fallback per gateway when no paired local transport confirms it.
            tesla_force_discharge_mutated = True
            mode_result = await _tesla_force_apply_operation_mode(
                site_configs,
                "autonomous",
                reason="force discharge",
            )
            mode_success = _tesla_force_result_all_confirmed(
                mode_result,
                site_configs,
            )

            if not mode_success:
                _LOGGER.error(
                    "Force discharge autonomous mode did not verify after retries; "
                    "continuing to tariff upload so restore cleanup remains armed"
                )
                hass.async_create_task(
                    _notify_api_error(
                        hass,
                        "Force Discharge Warning",
                        "Could not verify Tesla Time-Based Control after retries",
                    )
                )

            # Step 4: Create and upload discharge tariff to all gateways
            discharge_tariff, actual_expiry = _create_discharge_tariff(tariff_duration)
            all_success = True
            accepted_sites: list[str] = []
            unconfirmed_sites: list[str] = []
            for site_id, current_token, provider in site_configs:
                upload_status: dict[str, bool] = {}
                success = await send_tariff_to_tesla(
                    hass,
                    site_id,
                    discharge_tariff,
                    current_token,
                    provider,
                    fleet_base_url=entry.data.get(CONF_FLEET_API_BASE_URL),
                    accepted_status=upload_status,
                )
                if upload_status.get("accepted"):
                    accepted_sites.append(site_id)
                if not success:
                    if upload_status.get("accepted"):
                        _LOGGER.warning(
                            "Force discharge tariff was accepted by Tesla for site %s "
                            "but readback did not confirm it; scheduling restore cleanup",
                            site_id,
                        )
                        unconfirmed_sites.append(site_id)
                    else:
                        _LOGGER.error(
                            "Failed to upload discharge tariff to site %s", site_id
                        )
                        all_success = False
                elif len(site_configs) > 1:
                    await asyncio.sleep(1)

            if all_success or accepted_sites:
                if all_success:
                    active_site_ids = {
                        site_id for site_id, _token, _provider in site_configs
                    }
                else:
                    active_site_ids = set(accepted_sites)

                active_site_configs = [
                    config for config in site_configs if config[0] in active_site_ids
                ]
                reserve_result = await _tesla_force_pulse_backup_reserve(
                    active_site_configs,
                    0,
                    reason="force discharge final reserve pulse",
                    is_current=lambda: (
                        _command_generation[0] == _restore_gen
                        and _tesla_reserve_generation[0] == _reserve_gen
                    ),
                )
                reserve_success = _tesla_force_result_all_confirmed(
                    reserve_result,
                    active_site_configs,
                )
                if not reserve_success:
                    if _command_generation[0] != _restore_gen:
                        _LOGGER.info(
                            "Force discharge final reserve pulse superseded; "
                            "skipping stale cleanup"
                        )
                        return {"success": False, "error": "force discharge superseded"}
                    if _tesla_reserve_generation[0] != _reserve_gen:
                        _LOGGER.info(
                            "Force discharge reserve pulse superseded by a "
                            "newer reserve command; restoring non-reserve "
                            "Tesla settings"
                        )
                        await _cleanup_failed_tesla_force_discharge(
                            "reserve pulse superseded after tariff upload",
                            preserve_newer_reserve=True,
                        )
                        return {
                            "success": False,
                            "error": "reserve command superseded force discharge",
                        }
                    _LOGGER.error(
                        "Force discharge tariff uploaded but final Tesla "
                        "reserve pulse did not verify"
                    )
                    await _cleanup_failed_tesla_force_discharge(
                        "final reserve pulse did not verify"
                    )
                    hass.async_create_task(
                        _notify_api_error(
                            hass,
                            "Force Discharge Failed",
                            "Tesla reserve transition did not verify",
                        )
                    )
                    return {
                        "success": False,
                        "error": "final reserve pulse did not verify",
                    }

                force_discharge_state["active"] = True
                force_discharge_state["source"] = source
                # Use the requested duration for the countdown timer, not the
                # period-aligned tariff expiry. The TOU tariff window extends to
                # the period boundary (Tesla API requirement) but the timer fires
                # after the user's requested duration and restore_normal reverts.
                requested_expiry = dt_util.now() + timedelta(minutes=duration)
                force_discharge_state["expires_at"] = requested_expiry.astimezone(
                    dt_util.UTC
                )
                force_discharge_state["hardware_expires_at"] = (
                    _tesla_force_retry_expiry(
                        force_discharge_state,
                        actual_expiry,
                        degraded=(
                            not mode_success
                            or not reserve_success
                            or bool(unconfirmed_sites)
                        ),
                    )
                )
                _LOGGER.info(
                    "FORCE DISCHARGE ACTIVE%s: Tariff uploaded to %d gateway(s), "
                    "expires in %dmin (tariff %dmin window to %s)",
                    " (optimizer)" if source == "optimizer" else "",
                    len(site_configs),
                    duration,
                    tariff_duration,
                    actual_expiry.strftime("%H:%M"),
                )
                if unconfirmed_sites:
                    _LOGGER.warning(
                        "FORCE DISCHARGE CLEANUP ARMED: Tesla accepted the tariff for %d "
                        "gateway(s), but %d gateway(s) did not confirm readback; restore "
                        "will still run after %dmin",
                        len(accepted_sites),
                        len(unconfirmed_sites),
                        duration,
                    )

                # Dispatch event for switch entity
                async_dispatcher_send(
                    hass,
                    f"{DOMAIN}_force_discharge_state",
                    {
                        "active": True,
                        "expires_at": force_discharge_state["expires_at"].isoformat(),
                        "duration": duration,
                    },
                )

                # Schedule auto-restore
                if force_discharge_state["cancel_expiry_timer"]:
                    force_discharge_state["cancel_expiry_timer"]()

                async def auto_restore(_now):
                    """Auto-restore normal operation when discharge expires."""
                    if _command_generation[0] != _restore_gen:
                        _LOGGER.debug(
                            "Tesla force discharge timer superseded — skipping restore"
                        )
                        return
                    current_expiry = force_discharge_state.get("expires_at")
                    if current_expiry and _now < current_expiry:
                        _LOGGER.debug(
                            "Tesla force discharge timer expiry was extended — skipping stale restore"
                        )
                        return
                    if force_discharge_state["active"]:
                        _LOGGER.info(
                            "Force discharge expired, auto-restoring normal operation"
                        )
                        await hass.services.async_call(
                            DOMAIN,
                            SERVICE_RESTORE_NORMAL,
                            {"_allow_monitoring_restore": True},
                            blocking=True,
                        )

                # Use async_track_point_in_utc_time for one-time expiry (not recurring daily)
                force_discharge_state["cancel_expiry_timer"] = (
                    async_track_point_in_utc_time(
                        hass,
                        auto_restore,
                        force_discharge_state["expires_at"],
                    )
                )

                # Persist state to survive HA restarts
                await persist_force_mode_state()
                return {"success": True, "error": None}
            else:
                _LOGGER.error(
                    "Failed to upload discharge tariff to one or more gateways"
                )
                await _cleanup_failed_tesla_force_discharge(
                    "tariff upload failed after prerequisite writes"
                )
                hass.async_create_task(
                    _notify_api_error(
                        hass,
                        "Force Discharge Failed",
                        "Could not upload discharge tariff to Tesla",
                    )
                )
                return {"success": False, "error": "tariff upload did not verify"}

        except HomeAssistantError as e:
            _LOGGER.error("Force discharge rejected: %s", e)
            if _command_generation[0] != _restore_gen:
                _LOGGER.info(
                    "Force discharge grid confirmation superseded; skipping stale cleanup"
                )
            elif tesla_force_discharge_mutated:
                await _cleanup_failed_tesla_force_discharge(str(e))
            else:
                force_discharge_state["active"] = False
            raise
        except Exception as e:
            _LOGGER.error(f"Error in force discharge: {e}", exc_info=True)
            if tesla_force_discharge_mutated:
                await _cleanup_failed_tesla_force_discharge(str(e))
            else:
                force_discharge_state["active"] = False
            return {"success": False, "error": str(e)}

    def _create_discharge_tariff(duration_minutes: int) -> tuple[dict, datetime]:
        """Create a Tesla tariff optimized for exporting (force discharge).

        Uses the standard Tesla tariff structure.

        Returns:
            Tuple of (tariff_dict, actual_expiry_datetime)
            The expiry datetime is aligned to the end of the tariff window,
            ensuring the timer doesn't fire before the tariff window ends.
        """

        # Temporary rates are user-configurable; normal tariff rates below
        # remain unchanged outside the force-discharge window.
        buy_rate_discharge, sell_rate_discharge = configured_force_discharge_prices(
            entry.data
        )
        sell_rate_normal = 0.08  # 8c/kWh normal feed-in

        # Normal outside discharge window.
        buy_rate_normal = 0.30  # 30c/kWh

        _LOGGER.info(
            f"Creating discharge tariff: sell=${sell_rate_discharge}/kWh, buy=${buy_rate_discharge}/kWh for {duration_minutes} min"
        )

        # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
        buy_rates = {}
        sell_rates = {}
        tou_periods = {}

        # Get current time to determine discharge window
        now = dt_util.now()
        current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)
        minutes_into_period = now.minute % 30

        # Calculate how many 30-min periods the discharge covers
        # We need to account for the time already elapsed in the current period
        # If we're 18 minutes into a period, we only have 12 minutes left of useful discharge time
        remaining_in_current = 30 - minutes_into_period

        # Total periods needed: current period + enough future periods to cover duration
        # We always include current period even if partially elapsed (Tesla will respond)
        # Then add enough additional periods to cover the remaining time
        remaining_duration = max(0, duration_minutes - remaining_in_current)
        additional_periods = (remaining_duration + 29) // 30  # Round up
        total_periods = 1 + additional_periods  # Current + additional

        discharge_start = current_period_index
        discharge_end = (current_period_index + total_periods) % 48

        # Calculate actual expiry time aligned to tariff window end
        # This ensures the timer doesn't fire before the tariff window ends
        end_period_hour = (discharge_end // 2) % 24
        end_period_minute = 30 if discharge_end % 2 else 0
        actual_expiry = now.replace(
            hour=end_period_hour, minute=end_period_minute, second=0, microsecond=0
        )

        # Handle midnight wrap-around
        if actual_expiry <= now:
            actual_expiry += timedelta(days=1)

        actual_duration = int((actual_expiry - now).total_seconds() / 60)
        _LOGGER.info(
            f"Discharge window: periods {discharge_start} to {discharge_end} ({total_periods} periods), "
            f"current time: {now.hour:02d}:{now.minute:02d}, "
            f"actual duration: {actual_duration}min (aligned to period boundary)"
        )

        for i in range(48):
            hour = i // 2
            minute = 30 if i % 2 else 0
            period_name = f"{hour:02d}:{minute:02d}"

            # Check if this period is in the discharge window
            is_discharge_period = False
            if discharge_start < discharge_end:
                is_discharge_period = discharge_start <= i < discharge_end
            else:  # Wrap around midnight
                is_discharge_period = i >= discharge_start or i < discharge_end

            # Set rates based on whether we're in discharge window
            if is_discharge_period:
                buy_rates[period_name] = buy_rate_discharge
                sell_rates[period_name] = sell_rate_discharge
            else:
                buy_rates[period_name] = buy_rate_normal
                sell_rates[period_name] = sell_rate_normal

            # Calculate end time (30 minutes later)
            if minute == 0:
                to_hour = hour
                to_minute = 30
            else:  # minute == 30
                to_hour = (hour + 1) % 24  # Wrap around at midnight
                to_minute = 0

            # TOU period definition for seasons
            tou_periods[period_name] = {
                "periods": [
                    {
                        "fromDayOfWeek": 0,
                        "toDayOfWeek": 6,
                        "fromHour": hour,
                        "fromMinute": minute,
                        "toHour": to_hour,
                        "toMinute": to_minute,
                    }
                ]
            }

        # Create Tesla tariff structure
        tariff = {
            "name": f"Force Discharge ({duration_minutes}min)",
            "utility": "Tesla v1r",
            "code": f"DISCHARGE_{duration_minutes}",
            "currency": currency_for_entry(entry, hass),
            "daily_charges": [{"name": "Supply Charge"}],
            "demand_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {},
                "Winter": {},
            },
            "energy_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {"rates": buy_rates},
                "Winter": {},
            },
            "seasons": {
                "Summer": {
                    "fromMonth": 1,
                    "toMonth": 12,
                    "fromDay": 1,
                    "toDay": 31,
                    "tou_periods": tou_periods,
                },
                "Winter": {
                    "fromDay": 0,
                    "toDay": 0,
                    "fromMonth": 0,
                    "toMonth": 0,
                    "tou_periods": {},
                },
            },
            "sell_tariff": {
                "name": f"Force Discharge Export ({duration_minutes}min)",
                "utility": "Tesla v1r",
                "daily_charges": [{"name": "Charge"}],
                "demand_charges": {
                    "ALL": {"rates": {"ALL": 0}},
                    "Summer": {},
                    "Winter": {},
                },
                "energy_charges": {
                    "ALL": {"rates": {"ALL": 0}},
                    "Summer": {"rates": sell_rates},
                    "Winter": {},
                },
                "seasons": {
                    "Summer": {
                        "fromMonth": 1,
                        "toMonth": 12,
                        "fromDay": 1,
                        "toDay": 31,
                        "tou_periods": tou_periods,
                    },
                    "Winter": {
                        "fromDay": 0,
                        "toDay": 0,
                        "fromMonth": 0,
                        "toMonth": 0,
                        "tou_periods": {},
                    },
                },
            },
        }

        _LOGGER.info(
            f"Created discharge tariff: buy=${buy_rate_discharge}/kWh, sell=${sell_rate_discharge}/kWh for {total_periods} periods, "
            f"expires at {actual_expiry.strftime('%H:%M')}"
        )

        return tariff, actual_expiry

    async def handle_force_charge(call: ServiceCall) -> dict[str, Any] | None:
        """Force charge mode - switches to autonomous with free import tariff."""

        # Warn if calibration suspected (don't block — user manual command)
        _fc_entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        if _fc_entry_data.get("calibration_suspected"):
            _LOGGER.warning(
                "Force charge called during suspected calibration — command may not stick"
            )

        # Log call context for debugging (helps identify if called by automation)
        context = call.context
        _LOGGER.info(
            f"🔌 Force charge service called (context: user_id={context.user_id}, parent_id={context.parent_id})"
        )

        raw_duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        _LOGGER.debug(
            f"Force charge raw duration from call.data: {raw_duration!r} (type: {type(raw_duration).__name__})"
        )
        source = _control_call_source(call)
        command_power_w = _resolve_force_command_power_w(
            "charge",
            call.data.get("power_w", 0),
        )

        # Convert to int if string (from HA service selector or button-card)
        try:
            duration = int(raw_duration)
        except (ValueError, TypeError):
            _LOGGER.warning(
                f"Could not convert duration {raw_duration!r} to int, using default {DEFAULT_DISCHARGE_DURATION}"
            )
            duration = DEFAULT_DISCHARGE_DURATION
        if source == "optimizer":
            if not (1 <= duration <= 1440):
                _LOGGER.warning(
                    "Optimizer force charge duration %s out of range, using default %d",
                    duration,
                    DEFAULT_DISCHARGE_DURATION,
                )
                duration = DEFAULT_DISCHARGE_DURATION
        elif duration not in DISCHARGE_DURATIONS:
            _LOGGER.warning(
                f"Duration {duration} not in allowed values {DISCHARGE_DURATIONS}, using default {DEFAULT_DISCHARGE_DURATION}"
            )
            duration = DEFAULT_DISCHARGE_DURATION

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would force charge for %d minutes (source=%s, power_w=%s) — blocked by monitoring mode",
                duration,
                source,
                command_power_w,
            )
            return

        extend_hardware = call.data.get("_extend_hardware", False)

        # Hardware-only path: fires for BOTH (a) optimizer-issued dispatch and
        # (b) explicit hardware extensions during an active user force mode.
        # See force_discharge for the full rationale.
        if source == "optimizer" or (
            extend_hardware and force_charge_state.get("active")
        ):
            _LOGGER.debug(
                "_extend_hardware: no direct coordinator found for source=%s, falling through to full handler",
                source,
            )

        # Anchor the Tesla requested expiry before any API/setup awaits.  A
        # slow site-info or tariff request must not extend the force-charge
        # window across a demand boundary (or the optimizer's requested
        # duration).
        tesla_requested_expiry = dt_util.now() + timedelta(minutes=duration)

        _LOGGER.info(
            f"🔌 FORCE CHARGE: Activating for {duration} minutes (source={source})"
        )

        # Block force charge while demand protection is active, including the
        # one-minute pre-arm before the exact demand window.

        tesla_preflight_sites = _get_tesla_site_configs(hass, entry)
        if tesla_preflight_sites and not force_charge_state.get("active"):
            inherited_grid_charging = None
            transitioning_from_force_discharge = force_discharge_state.get("active")
            if transitioning_from_force_discharge:
                inherited_grid_charging = _optional_bool(
                    force_discharge_state.get("saved_grid_charging_enabled")
                )
            await _require_tesla_force_grid_charging_baselines(
                tesla_preflight_sites,
                inherited_grid_charging,
                inheritance_required=bool(transitioning_from_force_discharge),
            )

        # Cancel any pending expiry timers and advance the generation counter
        # synchronously — before any await — so that a queued restore callback
        # from a previous command cannot fire during this command's I/O window.
        _cancel_all_force_timers("new force_charge command")
        _command_generation[0] += 1
        _restore_gen = _command_generation[0]
        _reserve_gen = _tesla_reserve_generation[0]
        force_charge_state["_skip_backup_reserve_restore"] = False
        if self_consumption_state.get("active"):
            _clear_self_consumption_state()

        # Mutual exclusion + baseline inheritance: if a force_discharge is
        # active when this command lands, transition directly without
        # re-saving the current (discharge-modified) state as the baseline.
        # Inherit force_discharge's saved_* values so the eventual auto-restore
        # reverts to the TRUE pre-force state. Also clears discharge state
        # here at the single chokepoint rather than relying on the per-battery
        # clearing scattered through each branch below (that stays in place as
        # a defence-in-depth belt-and-braces).
        _transitioning_from_force_discharge = force_discharge_state.get("active", False)
        if _transitioning_from_force_discharge:
            _LOGGER.info(
                "force_charge: transitioning from active force_discharge, "
                "inheriting saved baseline (tariff/mode/reserve)"
            )
            force_charge_state["saved_tariff"] = force_discharge_state.get(
                "saved_tariff"
            )
            force_charge_state["saved_operation_mode"] = force_discharge_state.get(
                "saved_operation_mode"
            )
            force_charge_state["saved_backup_reserve"] = force_discharge_state.get(
                "saved_backup_reserve"
            )
            force_charge_state["saved_grid_charging_enabled"] = (
                force_discharge_state.get("saved_grid_charging_enabled")
            )
            force_discharge_state["active"] = False
            force_discharge_state["saved_tariff"] = None
            force_discharge_state["saved_operation_mode"] = None
            force_discharge_state["saved_backup_reserve"] = None
            force_discharge_state["saved_export_rule"] = None
            force_discharge_state["saved_grid_charging_enabled"] = None
            force_discharge_state["expires_at"] = None
            force_discharge_state["hardware_expires_at"] = None

        # Set force charge state IMMEDIATELY so the optimizer sees it
        # before the async Modbus/API call completes.  Prevents a race
        # where the optimizer finishes its LP cycle during the Modbus
        # write and overrides the mode (e.g. FoxESS Backup → Self Use).
        # Each battery branch sets expires_at on success or clears
        # active on failure. Treating a discharge transition as
        # "already force charging" skips the per-branch re-save that would
        # otherwise overwrite our inherited baseline.
        was_already_force_charging = (
            force_charge_state.get("active", False)
            or _transitioning_from_force_discharge
        )
        force_charge_state["active"] = True
        force_charge_state["source"] = source
        force_charge_state["duration"] = duration
        force_charge_state["power_w"] = command_power_w

        tesla_force_charge_mutated = False
        try:
            # Get Tesla gateway config
            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                force_charge_state["active"] = False
                _LOGGER.error("Missing Tesla site ID or token for force charge")
                return {"success": False, "error": "missing Tesla site configuration"}

            session = async_get_clientsession(hass)

            async def _cleanup_failed_tesla_force_charge(
                reason: str,
                *,
                preserve_newer_reserve: bool = False,
            ) -> None:
                """Arm cleanup and immediately restore any partially applied charge state."""
                force_charge_state["_skip_backup_reserve_restore"] = (
                    preserve_newer_reserve
                )
                retry_expiry = dt_util.utcnow() + timedelta(minutes=15)
                force_charge_state["active"] = True
                force_charge_state["source"] = source
                force_charge_state["expires_at"] = dt_util.utcnow() + timedelta(
                    minutes=2
                )
                force_charge_state["hardware_expires_at"] = _tesla_force_retry_expiry(
                    force_charge_state,
                    retry_expiry,
                    degraded=True,
                )
                _LOGGER.warning(
                    "Force charge partially applied; restoring saved Tesla state (%s)",
                    reason,
                )
                try:
                    await persist_force_mode_state()
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RESTORE_NORMAL,
                        {
                            "source": "force_cleanup",
                            "_allow_monitoring_restore": True,
                            "_skip_backup_reserve_restore": preserve_newer_reserve,
                        },
                        blocking=True,
                    )
                except Exception as err:
                    # Preserve the active cleanup marker. restore_normal owns
                    # its bounded retry when any hardware restore remains incomplete.
                    force_charge_state["active"] = True
                    _LOGGER.error(
                        "Immediate Tesla force-charge cleanup failed; state remains armed: %s",
                        err,
                    )
                    await persist_force_mode_state()

            # Cancel active discharge mode if switching to charge
            if force_discharge_state["active"]:
                _LOGGER.info("Canceling active discharge mode to enable charge mode")
                if force_discharge_state.get("cancel_expiry_timer"):
                    force_discharge_state["cancel_expiry_timer"]()
                    force_discharge_state["cancel_expiry_timer"] = None
                force_discharge_state["active"] = False
                force_discharge_state["expires_at"] = None

            # Step 1: Save current tariff and state (if not already in charge mode)
            if not was_already_force_charging:
                saved_states = {}
                for site_id, current_token, provider in site_configs:
                    headers = {
                        "Authorization": f"Bearer {current_token}",
                        "Content-Type": "application/json",
                    }
                    api_base = get_tesla_api_base_url(
                        provider, entry.data.get(CONF_FLEET_API_BASE_URL)
                    )
                    site_state = {}

                    _LOGGER.info(
                        "Saving current tariff before force charge for site %s...",
                        site_id,
                    )
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/tariff_rate",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            resp = data.get("response", {})
                            tariff_candidate = _extract_tesla_tariff_content(resp)
                            saved_tariff = await _cache_restorable_tesla_tariff(
                                tariff_candidate,
                                f"tariff_rate for site {site_id}",
                            )
                            site_state["saved_tariff"] = saved_tariff
                            if saved_tariff:
                                _LOGGER.info(
                                    "Saved tariff for site %s (name: %s)",
                                    site_id,
                                    saved_tariff.get("name", "unknown"),
                                )
                            elif tariff_candidate:
                                _LOGGER.warning(
                                    "Current tariff for site %s is a Tesla v1r force tariff; not saving as restore tariff",
                                    site_id,
                                )
                            else:
                                _LOGGER.warning(
                                    "Could not extract tariff from tariff_rate for site %s - will try site_info",
                                    site_id,
                                )
                        else:
                            _LOGGER.warning(
                                "tariff_rate endpoint returned %s for site %s - will try site_info fallback",
                                response.status,
                                site_id,
                            )

                    # Get and save current operation mode, backup reserve, and tariff (fallback)
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            data = await response.json()
                            site_info = data.get("response", {})
                            site_state["saved_operation_mode"] = site_info.get(
                                "default_real_mode"
                            )
                            # Determine the user's real backup reserve to restore later.
                            # Priority: 1) optimizer's startup reserve (persisted from first boot — authoritative)
                            #           2) optimizer's pre-IDLE value (if IDLE elevated it)
                            #           3) Tesla API value (ONLY if not 100% — force charge sets it to 100%)
                            #           4) Default to 0% (safe — won't force charge)
                            api_reserve = site_info.get("backup_reserve_percent")
                            opt_coord = (
                                hass.data.get(DOMAIN, {})
                                .get(entry.entry_id, {})
                                .get("optimization_coordinator")
                            )
                            startup_reserve = (
                                getattr(opt_coord, "_startup_backup_reserve", None)
                                if opt_coord
                                else None
                            )
                            pre_idle = (
                                getattr(opt_coord, "_pre_idle_backup_reserve", None)
                                if opt_coord
                                else None
                            )
                            if startup_reserve is not None:
                                site_state["saved_backup_reserve"] = startup_reserve
                            elif pre_idle is not None:
                                site_state["saved_backup_reserve"] = pre_idle
                            else:
                                resolved_reserve = None
                                if opt_coord is not None and hasattr(
                                    opt_coord, "resolve_restore_target"
                                ):
                                    resolved_reserve = (
                                        await opt_coord.resolve_restore_target()
                                    )
                                if resolved_reserve is not None:
                                    site_state["saved_backup_reserve"] = (
                                        resolved_reserve
                                    )
                                elif api_reserve is not None and api_reserve < 100:
                                    site_state["saved_backup_reserve"] = api_reserve
                                else:
                                    site_state["saved_backup_reserve"] = 0
                            _LOGGER.info(
                                "Site %s: saved operation mode: %s, backup reserve: %s%% (api=%s%%, pre_idle=%s%%, startup=%s%%)",
                                site_id,
                                site_state["saved_operation_mode"],
                                site_state["saved_backup_reserve"],
                                api_reserve,
                                pre_idle,
                                startup_reserve,
                            )

                            saved_grid_charging_enabled = (
                                _tesla_grid_charging_enabled_from_site_info(site_info)
                            )
                            saved_grid_charging_enabled = (
                                _remember_tesla_grid_charging_preference(
                                    site_id,
                                    saved_grid_charging_enabled,
                                )
                            )
                            site_state["saved_grid_charging_enabled"] = (
                                saved_grid_charging_enabled
                            )
                            _LOGGER.info(
                                "Site %s: saved grid charging: %s",
                                site_id,
                                saved_grid_charging_enabled,
                            )

                            # Prefer the documented site_info snapshot over the
                            # legacy tariff_rate endpoint for linked rate plans.
                            site_tariff = _extract_tesla_tariff_content(site_info)
                            site_info_saved_tariff = (
                                await _cache_restorable_tesla_tariff(
                                    site_tariff,
                                    f"site_info for site {site_id}",
                                )
                            )
                            if site_info_saved_tariff:
                                if (
                                    site_state.get("saved_tariff")
                                    and site_state["saved_tariff"]
                                    != site_info_saved_tariff
                                ):
                                    _LOGGER.info(
                                        "Replacing tariff_rate snapshot with authoritative "
                                        "site_info tariff for site %s",
                                        site_id,
                                    )
                                site_state["saved_tariff"] = site_info_saved_tariff
                                _LOGGER.info(
                                    "Saved authoritative tariff from site_info for site %s (name: %s)",
                                    site_id,
                                    site_info_saved_tariff.get("name", "unknown"),
                                )
                            elif not site_state.get("saved_tariff"):
                                if _cached_restorable_tesla_tariff():
                                    site_state["saved_tariff"] = (
                                        _cached_restorable_tesla_tariff()
                                    )
                                    _LOGGER.info(
                                        "Using cached Tesla tariff baseline for site %s (name: %s)",
                                        site_id,
                                        _tariff_display_name(
                                            site_state["saved_tariff"]
                                        ),
                                    )
                                else:
                                    _LOGGER.warning(
                                        "No tariff found in site_info for site %s",
                                        site_id,
                                    )
                                    electricity_provider = entry.options.get(
                                        CONF_ELECTRICITY_PROVIDER,
                                        entry.data.get(
                                            CONF_ELECTRICITY_PROVIDER, "amber"
                                        ),
                                    )
                                    if electricity_provider == "globird":
                                        try:
                                            from .automations.actions import (
                                                _send_expo_push,
                                            )

                                            await _send_expo_push(
                                                hass,
                                                "Battery Warning",
                                                "Tariff not saved - may need reconfiguration",
                                            )
                                        except Exception as notify_err:
                                            _LOGGER.debug(
                                                f"Could not send notification: {notify_err}"
                                            )
                        else:
                            text = await response.text()
                            _LOGGER.error(
                                f"Failed to get site_info for site {site_id}: {response.status} - {text}"
                            )

                    saved_states[site_id] = site_state
                    if len(site_configs) > 1:
                        await asyncio.sleep(1)

                # Store per-site saved states
                force_charge_state["saved_states"] = saved_states
                primary_state = saved_states.get(site_configs[0][0], {})
                force_charge_state["saved_tariff"] = primary_state.get("saved_tariff")
                force_charge_state["saved_operation_mode"] = primary_state.get(
                    "saved_operation_mode"
                )
                force_charge_state["saved_backup_reserve"] = primary_state.get(
                    "saved_backup_reserve"
                )
                force_charge_state["saved_grid_charging_enabled"] = primary_state.get(
                    "saved_grid_charging_enabled"
                )

            # Step 3: Switch to autonomous mode and set reserve locally first.
            tesla_force_charge_mutated = True
            mode_result = await _tesla_force_apply_operation_mode(
                site_configs,
                "autonomous",
                reason="force charge",
            )
            mode_success = _tesla_force_result_all_confirmed(
                mode_result,
                site_configs,
            )
            if not mode_success:
                _LOGGER.error(
                    "Force charge failed before tariff upload: Tesla autonomous mode did not verify"
                )
                await _cleanup_failed_tesla_force_charge(
                    "autonomous mode did not verify"
                )
                hass.async_create_task(
                    _notify_api_error(
                        hass,
                        "Force Charge Failed",
                        "Could not verify Tesla Time-Based Control after retries",
                    )
                )
                return {"success": False, "error": "autonomous mode did not verify"}

            grid_result = await _tesla_force_apply_grid_charging(
                site_configs,
                True,
                reason="force charge",
                is_current=lambda: _command_generation[0] == _restore_gen,
            )
            grid_confirmed = _tesla_force_result_all_confirmed(
                grid_result,
                site_configs,
            )
            if not grid_confirmed and _tesla_force_result_all_grid_field_absent_safe(
                grid_result,
                site_configs,
            ):
                _LOGGER.warning(
                    "Force charge proceeding because Tesla accepted grid charging "
                    "and site_info did not report the grid-charging field"
                )
                grid_confirmed = True
            if not grid_confirmed:
                if _command_generation[0] != _restore_gen:
                    _LOGGER.info(
                        "Force charge grid confirmation superseded; skipping stale cleanup"
                    )
                    return {
                        "success": False,
                        "error": "force charge superseded",
                    }
                _LOGGER.error(
                    "Force charge failed before tariff upload: Tesla grid charging did not verify"
                )
                await _cleanup_failed_tesla_force_charge(
                    "grid charging enable did not verify"
                )
                if source == "optimizer":
                    # The coordinator deliberately retains its action marker
                    # and retries this fail-closed outcome.  Do not present a
                    # retryable readback ambiguity as a terminal failure.
                    hass.async_create_task(
                        _notify_api_error(
                            hass,
                            "Force Charge Retrying",
                            "Tesla grid charging could not be verified; Tesla v1r will retry",
                        )
                    )
                else:
                    hass.async_create_task(
                        _notify_api_error(
                            hass,
                            "Force Charge Failed",
                            "Could not verify Tesla grid charging was enabled",
                        )
                    )
                return {
                    "success": False,
                    "error": "grid charging enable did not verify",
                }

            reserve_result = await _tesla_force_apply_backup_reserve(
                site_configs,
                100,
                reason="force charge",
            )
            if not _tesla_force_result_all_confirmed(reserve_result, site_configs):
                _LOGGER.error(
                    "Force charge failed before tariff upload: Tesla backup reserve did not verify"
                )
                await _cleanup_failed_tesla_force_charge(
                    "backup reserve did not verify"
                )
                hass.async_create_task(
                    _notify_api_error(
                        hass,
                        "Force Charge Failed",
                        "Could not verify Tesla backup reserve after retries",
                    )
                )
                return {"success": False, "error": "backup reserve did not verify"}

            # Step 4: Create and upload charge tariff to all gateways
            charge_tariff, actual_expiry = _create_charge_tariff(duration)
            all_success = True
            accepted_sites: list[str] = []
            unconfirmed_sites: list[str] = []
            for site_id, current_token, provider in site_configs:
                upload_status: dict[str, bool] = {}
                success = await send_tariff_to_tesla(
                    hass,
                    site_id,
                    charge_tariff,
                    current_token,
                    provider,
                    fleet_base_url=entry.data.get(CONF_FLEET_API_BASE_URL),
                    accepted_status=upload_status,
                )
                if upload_status.get("accepted"):
                    accepted_sites.append(site_id)
                if not success:
                    all_success = False
                    if upload_status.get("accepted"):
                        _LOGGER.warning(
                            "Force charge tariff was accepted by Tesla for site %s "
                            "but readback did not confirm it; keeping cleanup armed",
                            site_id,
                        )
                        unconfirmed_sites.append(site_id)
                    else:
                        _LOGGER.error(
                            "Failed to upload charge tariff to site %s", site_id
                        )
                elif len(site_configs) > 1:
                    await asyncio.sleep(1)

            if all_success or accepted_sites:
                if all_success:
                    active_site_ids = {
                        site_id for site_id, _token, _provider in site_configs
                    }
                else:
                    active_site_ids = set(accepted_sites)
                active_site_configs = [
                    config for config in site_configs if config[0] in active_site_ids
                ]
                reserve_result = await _tesla_force_pulse_backup_reserve(
                    active_site_configs,
                    100,
                    reason="force charge final reserve pulse",
                    is_current=lambda: (
                        _command_generation[0] == _restore_gen
                        and _tesla_reserve_generation[0] == _reserve_gen
                    ),
                )
                if not _tesla_force_result_all_confirmed(
                    reserve_result,
                    active_site_configs,
                ):
                    if _command_generation[0] != _restore_gen:
                        _LOGGER.info(
                            "Force charge final reserve pulse superseded; "
                            "skipping stale cleanup"
                        )
                        return {
                            "success": False,
                            "error": "force charge superseded",
                        }
                    if _tesla_reserve_generation[0] != _reserve_gen:
                        _LOGGER.info(
                            "Force charge reserve pulse superseded by a newer "
                            "reserve command; restoring non-reserve Tesla settings"
                        )
                        await _cleanup_failed_tesla_force_charge(
                            "reserve pulse superseded after tariff upload",
                            preserve_newer_reserve=True,
                        )
                        return {
                            "success": False,
                            "error": "reserve command superseded force charge",
                        }
                    _LOGGER.error(
                        "Force charge tariff uploaded but final Tesla reserve "
                        "pulse did not verify"
                    )
                    await _cleanup_failed_tesla_force_charge(
                        "final reserve pulse did not verify"
                    )
                    return {
                        "success": False,
                        "error": "final reserve pulse did not verify",
                    }

                force_charge_state["active"] = True
                force_charge_state["source"] = source
                # Use the requested duration for the countdown timer, not the
                # period-aligned tariff expiry. The TOU tariff window extends to
                # the period boundary (Tesla API requirement) but the timer fires
                # after the user's requested duration and restore_normal reverts.
                force_charge_state["expires_at"] = tesla_requested_expiry.astimezone(
                    dt_util.UTC
                )
                force_charge_state["hardware_expires_at"] = _tesla_force_retry_expiry(
                    force_charge_state,
                    actual_expiry,
                    degraded=bool(unconfirmed_sites),
                )
                _LOGGER.info(
                    "FORCE CHARGE ACTIVE%s: Tariff uploaded to %d gateway(s), "
                    "expires in %dmin (tariff window to %s)",
                    " (optimizer)" if source == "optimizer" else "",
                    len(active_site_configs),
                    duration,
                    actual_expiry.strftime("%H:%M"),
                )
                if unconfirmed_sites:
                    _LOGGER.warning(
                        "FORCE CHARGE CLEANUP ARMED: Tesla accepted the tariff for %d "
                        "gateway(s), but %d gateway(s) did not confirm readback; restore "
                        "will still run after %dmin",
                        len(accepted_sites),
                        len(unconfirmed_sites),
                        duration,
                    )

                # Kick PW3 to ensure it starts charging immediately
                _schedule_tesla_charge_kick(
                    "force_charge",
                    allow_grid_field_absent_compatibility=True,
                    initial_delay_seconds=60,
                )

                # Dispatch event for UI
                async_dispatcher_send(
                    hass,
                    f"{DOMAIN}_force_charge_state",
                    {
                        "active": True,
                        "expires_at": force_charge_state["expires_at"].isoformat(),
                        "duration": duration,
                    },
                )

                # Schedule auto-restore
                if force_charge_state["cancel_expiry_timer"]:
                    force_charge_state["cancel_expiry_timer"]()

                async def auto_restore_charge(_now):
                    """Auto-restore normal operation when charge expires."""
                    if _command_generation[0] != _restore_gen:
                        _LOGGER.debug(
                            "Tesla force charge timer superseded — skipping restore"
                        )
                        return
                    current_expiry = force_charge_state.get("expires_at")
                    if current_expiry and _now < current_expiry:
                        _LOGGER.debug(
                            "Tesla force charge timer expiry was extended — skipping stale restore"
                        )
                        return
                    if force_charge_state["active"]:
                        _LOGGER.info(
                            "Force charge expired, auto-restoring normal operation"
                        )
                        await hass.services.async_call(
                            DOMAIN,
                            SERVICE_RESTORE_NORMAL,
                            {
                                "source": "force_timer",
                                "_allow_monitoring_restore": True,
                            },
                            blocking=True,
                        )

                # Use async_track_point_in_utc_time for one-time expiry (not recurring daily)
                force_charge_state["cancel_expiry_timer"] = (
                    async_track_point_in_utc_time(
                        hass,
                        auto_restore_charge,
                        force_charge_state["expires_at"],
                    )
                )

                # Persist state to survive HA restarts
                await persist_force_mode_state()
                return {"success": True, "error": None}
            else:
                _LOGGER.error("Failed to upload charge tariff to one or more gateways")
                await _cleanup_failed_tesla_force_charge(
                    "tariff upload failed after prerequisite writes"
                )
                hass.async_create_task(
                    _notify_api_error(
                        hass,
                        "Force Charge Failed",
                        "Could not upload charge tariff to Tesla",
                    )
                )
                return {"success": False, "error": "tariff upload did not verify"}

        except Exception as e:
            _LOGGER.error(f"Error in force charge: {e}", exc_info=True)
            if tesla_force_charge_mutated:
                await _cleanup_failed_tesla_force_charge(str(e))
            else:
                force_charge_state["active"] = False
            return {"success": False, "error": str(e)}

    def _create_charge_tariff(duration_minutes: int) -> tuple[dict, datetime]:
        """Create a Tesla tariff optimized for charging from grid (force charge).

        Uses the standard Tesla tariff structure.

        Returns:
            Tuple of (tariff_dict, actual_expiry_datetime)
            The expiry datetime is aligned to the end of the tariff window,
            ensuring the timer doesn't fire before the tariff window ends.
        """

        # Rates during charge window - free to buy, no sell incentive
        buy_rate_charge = 0.00  # $0/kWh - maximum incentive to charge
        sell_rate_charge = 0.00  # $0/kWh - no incentive to export

        # Rates outside charge window - expensive to buy, no sell
        buy_rate_normal = 10.00  # $10/kWh - huge disincentive to charge
        sell_rate_normal = 0.00  # $0/kWh - no incentive to export

        _LOGGER.info(
            f"Creating charge tariff: buy=${buy_rate_charge}/kWh during charge, ${buy_rate_normal}/kWh outside for {duration_minutes} min"
        )

        # Build rates dictionaries for all 48 x 30-minute periods (24 hours)
        buy_rates = {}
        sell_rates = {}
        tou_periods = {}

        # Get current time to determine charge window
        now = dt_util.now()
        current_period_index = (now.hour * 2) + (1 if now.minute >= 30 else 0)
        minutes_into_period = now.minute % 30

        # Calculate how many 30-min periods the charge covers
        # We need to account for the time already elapsed in the current period
        remaining_in_current = 30 - minutes_into_period

        # Total periods needed: current period + enough future periods to cover duration
        # We always include current period even if partially elapsed (Tesla will respond)
        remaining_duration = max(0, duration_minutes - remaining_in_current)
        additional_periods = (remaining_duration + 29) // 30  # Round up
        total_periods = 1 + additional_periods  # Current + additional

        charge_start = current_period_index
        charge_end = (current_period_index + total_periods) % 48

        # Calculate actual expiry time aligned to tariff window end
        # This ensures the timer doesn't fire before the tariff window ends
        end_period_hour = (charge_end // 2) % 24
        end_period_minute = 30 if charge_end % 2 else 0
        actual_expiry = now.replace(
            hour=end_period_hour, minute=end_period_minute, second=0, microsecond=0
        )

        # Handle midnight wrap-around
        if actual_expiry <= now:
            actual_expiry += timedelta(days=1)

        actual_duration = int((actual_expiry - now).total_seconds() / 60)
        _LOGGER.info(
            f"Charge window: periods {charge_start} to {charge_end} ({total_periods} periods), "
            f"current time: {now.hour:02d}:{now.minute:02d}, "
            f"actual duration: {actual_duration}min (aligned to period boundary)"
        )

        for i in range(48):
            hour = i // 2
            minute = 30 if i % 2 else 0
            period_name = f"{hour:02d}:{minute:02d}"

            # Check if this period is in the charge window
            is_charge_period = False
            if charge_start < charge_end:
                is_charge_period = charge_start <= i < charge_end
            else:  # Wrap around midnight
                is_charge_period = i >= charge_start or i < charge_end

            # Set rates based on whether we're in charge window
            if is_charge_period:
                buy_rates[period_name] = buy_rate_charge
                sell_rates[period_name] = sell_rate_charge
            else:
                buy_rates[period_name] = buy_rate_normal
                sell_rates[period_name] = sell_rate_normal

            # Calculate end time (30 minutes later)
            if minute == 0:
                to_hour = hour
                to_minute = 30
            else:  # minute == 30
                to_hour = (hour + 1) % 24  # Wrap around at midnight
                to_minute = 0

            # TOU period definition for seasons
            tou_periods[period_name] = {
                "periods": [
                    {
                        "fromDayOfWeek": 0,
                        "toDayOfWeek": 6,
                        "fromHour": hour,
                        "fromMinute": minute,
                        "toHour": to_hour,
                        "toMinute": to_minute,
                    }
                ]
            }

        # Create Tesla tariff structure
        tariff = {
            "name": f"Force Charge ({duration_minutes}min)",
            "utility": "Tesla v1r",
            "code": f"CHARGE_{duration_minutes}",
            "currency": currency_for_entry(entry, hass),
            "daily_charges": [{"name": "Supply Charge"}],
            "demand_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {},
                "Winter": {},
            },
            "energy_charges": {
                "ALL": {"rates": {"ALL": 0}},
                "Summer": {"rates": buy_rates},
                "Winter": {},
            },
            "seasons": {
                "Summer": {
                    "fromMonth": 1,
                    "toMonth": 12,
                    "fromDay": 1,
                    "toDay": 31,
                    "tou_periods": tou_periods,
                },
                "Winter": {
                    "fromDay": 0,
                    "toDay": 0,
                    "fromMonth": 0,
                    "toMonth": 0,
                    "tou_periods": {},
                },
            },
            "sell_tariff": {
                "name": f"Force Charge Export ({duration_minutes}min)",
                "utility": "Tesla v1r",
                "daily_charges": [{"name": "Charge"}],
                "demand_charges": {
                    "ALL": {"rates": {"ALL": 0}},
                    "Summer": {},
                    "Winter": {},
                },
                "energy_charges": {
                    "ALL": {"rates": {"ALL": 0}},
                    "Summer": {"rates": sell_rates},
                    "Winter": {},
                },
                "seasons": {
                    "Summer": {
                        "fromMonth": 1,
                        "toMonth": 12,
                        "fromDay": 1,
                        "toDay": 31,
                        "tou_periods": tou_periods,
                    },
                    "Winter": {
                        "fromDay": 0,
                        "toDay": 0,
                        "fromMonth": 0,
                        "toMonth": 0,
                        "tou_periods": {},
                    },
                },
            },
        }

        _LOGGER.info(
            f"Created charge tariff: buy=${buy_rate_charge}/kWh during charge, ${buy_rate_normal}/kWh outside for {total_periods} periods, "
            f"expires at {actual_expiry.strftime('%H:%M')}"
        )

        return tariff, actual_expiry

    async def handle_restore_normal(call: ServiceCall) -> None:
        """Restore normal operation - restore saved tariff."""
        # Log call context for debugging (helps identify if called by automation)
        context = call.context
        source = _control_call_source(call)
        _LOGGER.info(
            f"🔄 Restore normal service called (context: user_id={context.user_id}, parent_id={context.parent_id}, source={source})"
        )
        _LOGGER.info("🔄 RESTORE NORMAL: Restoring normal operation")
        force_restore = bool(call.data.get("_force_restore"))
        confirm_restore = bool(call.data.get("_confirm_restore"))
        skip_backup_reserve_restore = bool(
            call.data.get("_skip_backup_reserve_restore")
            or force_charge_state.get("_skip_backup_reserve_restore")
            or force_discharge_state.get("_skip_backup_reserve_restore")
        )
        optimizer_owned_restore = (
            source == "optimizer"
            or force_discharge_state.get("source") == "optimizer"
            or force_charge_state.get("source") == "optimizer"
        )
        restore_was_force_discharging = bool(force_discharge_state.get("active"))
        restore_was_force_charging = bool(force_charge_state.get("active"))
        restore_was_hold_soc = bool(hold_soc_state.get("active"))
        force_mode_cleanup_restore = (
            restore_was_force_discharging or restore_was_force_charging
        )
        allow_monitoring_restore = bool(
            call.data.get("_allow_monitoring_restore")
            and (optimizer_owned_restore or force_mode_cleanup_restore)
        )
        monitoring_restore_allowed = allow_monitoring_restore or force_restore

        if (
            _monitoring_mode_should_block_control(call)
            and not monitoring_restore_allowed
        ):
            _LOGGER.info(
                "[MONITORING] Would restore normal operation (source=%s) — blocked by monitoring mode",
                source,
            )
            return
        if _is_monitoring_mode():
            _LOGGER.info(
                "[MONITORING] Allowing Sigenergy native/VPP restore so Remote EMS is released"
            )
        elif _is_monitoring_mode() and monitoring_restore_allowed:
            _LOGGER.info(
                "[MONITORING] Allowing force-mode restore cleanup so existing hardware control is released"
            )

        native_coordinator = (
            hass.data.get(DOMAIN, {})
            .get(entry.entry_id, {})
            .get("battery_energy_coordinator")
        )
        if _uses_native_battery_integration(native_coordinator):
            readiness = getattr(native_coordinator, "startup_control_ready", None)
            if not callable(readiness) or not readiness():
                raise HomeAssistantError(
                    "Native Home Assistant battery integration is not ready; "
                    "restore has been preserved for retry"
                )

        # No cooldown: a Stop Charge / Stop Discharge press is a one-shot
        # override that washes through on the next 5-min LP cycle if the
        # plan still wants the same action. Users who want to disable the
        # optimizer for an extended period should toggle the enabled flag,
        # not rely on Restore to suppress force modes for 30 min.
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

        # Check if optimizer is active (and not in monitoring mode) — suppress routine notifications
        # (optimizer transitions between force modes frequently; AEMO spikes have their own notification)
        # Monitoring mode doesn't execute actions, so notifications should still fire.
        suppress_notification = False
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        opt_coordinator = entry_data.get("optimization_coordinator")
        if opt_coordinator and getattr(opt_coordinator, "_enabled", False):
            monitoring_mode = entry.options.get(
                CONF_MONITORING_MODE, entry.data.get(CONF_MONITORING_MODE, False)
            )
            if not monitoring_mode:
                suppress_notification = True

        _cancel_all_force_timers("restore_normal")
        _command_generation[0] += 1
        _restore_generation = _command_generation[0]
        _restore_reserve_generation = _tesla_reserve_generation[0]

        def _restore_superseded(stage: str) -> bool:
            """Return True if a newer force command started during this restore."""
            if _command_generation[0] == _restore_generation:
                return False
            _LOGGER.info(
                "Restore normal superseded by newer force command during %s; "
                "leaving active force state untouched",
                stage,
            )
            return True

        try:
            restore_retry_count = int(call.data.get("_restore_retry", 0) or 0)
        except (TypeError, ValueError):
            restore_retry_count = 0
        tesla_restore_failed = False

        def _mark_tesla_restore_failed(reason: str) -> None:
            nonlocal tesla_restore_failed
            tesla_restore_failed = True
            _LOGGER.warning("Tesla restore_normal did not fully complete: %s", reason)

        def _schedule_tesla_restore_retry(reason: str) -> bool:
            if restore_retry_count >= 3:
                _LOGGER.error(
                    "Tesla restore_normal still failing after %d retries; "
                    "leaving force state active for manual restore (%s)",
                    restore_retry_count,
                    reason,
                )
                return False

            retry_state = (
                force_charge_state
                if restore_was_force_charging or force_charge_state.get("active")
                else force_discharge_state
            )
            retry_at = dt_util.utcnow() + timedelta(seconds=60)
            next_retry = restore_retry_count + 1

            async def _retry_tesla_restore(_now):
                if not (
                    force_charge_state.get("active")
                    or force_discharge_state.get("active")
                ):
                    _LOGGER.debug(
                        "Tesla restore retry skipped; force state is no longer active"
                    )
                    return
                _LOGGER.warning(
                    "Retrying Tesla restore_normal after incomplete restore (%s, attempt %d)",
                    reason,
                    next_retry,
                )
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_RESTORE_NORMAL,
                    {
                        "_restore_retry": next_retry,
                        "_allow_monitoring_restore": True,
                        "_skip_backup_reserve_restore": (skip_backup_reserve_restore),
                    },
                    blocking=True,
                )

            if retry_state.get("cancel_expiry_timer"):
                retry_state["cancel_expiry_timer"]()
            retry_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                hass,
                _retry_tesla_restore,
                retry_at,
            )
            _LOGGER.warning(
                "Tesla restore_normal incomplete; retry %d scheduled in 60 seconds (%s)",
                next_retry,
                reason,
            )
            return True

        def _schedule_tesla_hold_restore_retry(reason: str) -> bool:
            """Keep Hold tracking until its saved reserve verifies restored."""
            if restore_retry_count >= 3:
                _LOGGER.error(
                    "Tesla Hold SoC cleanup still failing after %d retries; "
                    "leaving Hold state active for manual restore (%s)",
                    restore_retry_count,
                    reason,
                )
                return False

            retry_at = dt_util.utcnow() + timedelta(seconds=60)
            next_retry = restore_retry_count + 1
            retry_command_generation = _command_generation[0]
            retry_reserve_generation = _tesla_reserve_generation[0]

            async def _retry_tesla_hold_restore(_now):
                if not hold_soc_state.get("active"):
                    return
                if (
                    _command_generation[0] != retry_command_generation
                    or _tesla_reserve_generation[0] != retry_reserve_generation
                ):
                    _LOGGER.info(
                        "Tesla Hold SoC cleanup retry superseded by a newer "
                        "command; clearing stale Hold tracking"
                    )
                    _clear_hold_soc_state()
                    await persist_force_mode_state()
                    return
                _LOGGER.warning(
                    "Retrying Tesla Hold SoC cleanup (%s, attempt %d)",
                    reason,
                    next_retry,
                )
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_RESTORE_NORMAL,
                    {
                        "source": "hold_soc_cleanup",
                        "_force_restore": True,
                        "_restore_retry": next_retry,
                    },
                    blocking=True,
                )

            if hold_soc_state.get("cancel_expiry_timer"):
                hold_soc_state["cancel_expiry_timer"]()
            hold_soc_state["active"] = True
            hold_soc_state["pending"] = True
            hold_soc_state["expires_at"] = retry_at
            hold_soc_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                hass,
                _retry_tesla_hold_restore,
                retry_at,
            )
            _LOGGER.warning(
                "Tesla Hold SoC cleanup incomplete; retry %d scheduled in "
                "60 seconds (%s)",
                next_retry,
                reason,
            )
            return True

        def _saved_hold_soc_backup_reserve() -> int | None:
            saved = hold_soc_state.get("saved_backup_reserve")
            if saved is None:
                saved, _source = _disabled_optimizer_backup_reserve_target(entry)
            try:
                saved = int(saved)
            except (TypeError, ValueError):
                return None
            if 0 <= saved <= 100:
                return saved
            return None

        # Guard: if no force mode is active and no saved state exists, there's
        # nothing to restore. Skip to avoid false "tariff not restored" warnings,
        # wrong mode switches (autonomous instead of self_consumption), and
        # spurious push notifications.
        has_active_force = force_discharge_state.get(
            "active"
        ) or force_charge_state.get("active")
        restorable_saved_tariff = _select_restorable_tesla_tariff(
            force_discharge_state.get("saved_tariff"),
            force_charge_state.get("saved_tariff"),
            _cached_restorable_tesla_tariff(),
            _configured_restorable_tesla_tariff(),
        )
        saved_grid_charging_enabled = (
            force_discharge_state.get("saved_grid_charging_enabled")
            if force_discharge_state.get("saved_grid_charging_enabled") is not None
            else force_charge_state.get("saved_grid_charging_enabled")
        )
        has_saved_state = (
            restorable_saved_tariff
            or force_discharge_state.get("saved_operation_mode")
            or force_charge_state.get("saved_operation_mode")
            or force_discharge_state.get("saved_backup_reserve") is not None
            or force_charge_state.get("saved_backup_reserve") is not None
            or (restore_was_hold_soc and _saved_hold_soc_backup_reserve() is not None)
            or saved_grid_charging_enabled is not None
        )
        if not force_restore and not has_active_force and not has_saved_state:
            _LOGGER.info(
                "Restore normal: no force mode active and no saved state — nothing to restore"
            )
            if confirm_restore:
                return {"success": False, "error": "nothing to restore"}
            return

        try:
            hold_only_restore = (
                restore_was_hold_soc
                and not has_active_force
                and not restorable_saved_tariff
                and not force_discharge_state.get("saved_operation_mode")
                and not force_charge_state.get("saved_operation_mode")
                and saved_grid_charging_enabled is None
            )
            site_configs = _get_tesla_site_configs(hass, entry)
            if hold_only_restore:
                saved_backup_reserve = _saved_hold_soc_backup_reserve()
                if saved_backup_reserve is None:
                    _LOGGER.warning(
                        "Restore normal: Hold SoC had no saved backup reserve; "
                        "will not change current Tesla reserve"
                    )
                    _schedule_tesla_hold_restore_retry(
                        "saved backup reserve unavailable"
                    )
                    await persist_force_mode_state()
                    return
                if not site_configs:
                    _LOGGER.error("Missing Tesla site ID or token for Hold SoC cleanup")
                    _schedule_tesla_hold_restore_retry(
                        "Tesla site configuration unavailable"
                    )
                    await persist_force_mode_state()
                    return

                normalized_saved_reserve = _normalize_tesla_backup_reserve_percent(
                    saved_backup_reserve
                )
                session = async_get_clientsession(hass)

                def _hold_restore_still_current() -> bool:
                    return (
                        _command_generation[0] == _restore_generation
                        and _tesla_reserve_generation[0] == _restore_reserve_generation
                    )

                cleanup_verified = True
                for site_id, current_token, provider in site_configs:
                    site_config = (site_id, current_token, provider)
                    _LOGGER.info(
                        "Restore normal: restoring Hold SoC backup reserve "
                        "to %d%% for site %s",
                        normalized_saved_reserve,
                        site_id,
                    )
                    reserve_result = await _tesla_force_pulse_backup_reserve(
                        [site_config],
                        normalized_saved_reserve,
                        reason="Hold SoC cleanup reserve pulse",
                        prefer_local=(site_id == site_configs[0][0]),
                        is_current=_hold_restore_still_current,
                    )
                    if not _tesla_force_result_all_confirmed(
                        reserve_result,
                        [site_config],
                    ):
                        cleanup_verified = False
                        break
                    headers = {
                        "Authorization": f"Bearer {current_token}",
                        "Content-Type": "application/json",
                    }
                    api_base = get_tesla_api_base_url(
                        provider,
                        entry.data.get(CONF_FLEET_API_BASE_URL),
                    )
                    if not await _tesla_force_confirm_backup_reserve(
                        session,
                        api_base,
                        site_id,
                        headers,
                        normalized_saved_reserve,
                        is_current=_hold_restore_still_current,
                    ):
                        cleanup_verified = False
                        break

                if not _hold_restore_still_current():
                    _LOGGER.info(
                        "Tesla Hold SoC cleanup was superseded; preserving "
                        "the newer command"
                    )
                    _clear_hold_soc_state()
                    await persist_force_mode_state()
                    return
                if not cleanup_verified:
                    _schedule_tesla_hold_restore_retry("saved reserve did not verify")
                    await persist_force_mode_state()
                    return

                _clear_hold_soc_state()
                await persist_force_mode_state()
                _LOGGER.info(
                    "Tesla Hold SoC cleanup verified at %d%%",
                    normalized_saved_reserve,
                )
                return

            # Get Tesla gateway config
            if not site_configs:
                _LOGGER.error("Missing Tesla site ID or token for restore normal")
                if confirm_restore:
                    return {
                        "success": False,
                        "error": "Tesla site configuration unavailable",
                    }
                return

            session = async_get_clientsession(hass)

            # IMMEDIATELY switch to self_consumption on all gateways to stop any ongoing export/import
            if force_discharge_state.get("active") or force_charge_state.get("active"):
                _LOGGER.info(
                    "Immediately switching to self_consumption to stop forced charge/discharge"
                )
                handoff_result = await _tesla_force_apply_operation_mode(
                    site_configs,
                    "self_consumption",
                    reason="restore initial handoff",
                )
                if not _tesla_force_result_all_confirmed(
                    handoff_result,
                    site_configs,
                ):
                    _mark_tesla_restore_failed(
                        "initial self_consumption handoff failed"
                    )
                if _restore_superseded("initial mode handoff"):
                    return

            # Check if user is using dynamic pricing (restore via sync instead of saved tariff)
            electricity_provider = entry.options.get(
                CONF_ELECTRICITY_PROVIDER,
                entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber"),
            )

            # Find saved tariff (prefer discharge, then charge), but never use
            # a temporary Tesla v1r force tariff as the restore tariff.
            saved_tariff = _select_restorable_tesla_tariff(
                force_discharge_state.get("saved_tariff"),
                force_charge_state.get("saved_tariff"),
                _cached_restorable_tesla_tariff(),
                _configured_restorable_tesla_tariff(),
            )

            # Dynamic pricing providers should sync fresh prices, not restore stale saved tariff.
            # AEMO VPP is spike detection on top of the user's normal tariff, so it
            # must restore the saved tariff instead of calling sync_tou_schedule
            # (which intentionally skips aemo_vpp).
            dynamic_providers = ("amber", "flow_power", "octopus")
            if electricity_provider in dynamic_providers:
                # Dynamic pricing users - trigger a fresh sync to get current prices
                # (sync handler already loops over all site_ids)
                _LOGGER.info(
                    f"{electricity_provider} user - triggering sync to restore normal operation"
                )
                hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
                    "_suppress_force_mode_toggle_once"
                ] = "restore_normal is already controlling Tesla operation mode"
                if allow_monitoring_restore:
                    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
                        "_allow_monitoring_tou_sync_once"
                    ] = "optimizer shutdown restore is releasing an active force tariff"
                elif force_mode_cleanup_restore:
                    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
                        "_allow_monitoring_tou_sync_once"
                    ] = "restore normal is cleaning up an active force tariff"
                hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
                    "_allow_force_restore_tou_sync_once"
                ] = "restore_normal is replacing an active Tesla v1r force tariff"
                await hass.services.async_call(
                    DOMAIN, SERVICE_SYNC_TOU, {}, blocking=True
                )
                if _restore_superseded("TOU sync"):
                    return
            elif saved_tariff:
                # Non-dynamic users - restore saved tariffs per site
                _LOGGER.info("Restoring saved tariffs...")
                # Check for per-site saved states or fall back to global saved tariff
                discharge_saved = force_discharge_state.get("saved_states") or {}
                charge_saved = force_charge_state.get("saved_states") or {}
                for site_id, current_token, provider in site_configs:
                    site_tariff = _select_restorable_tesla_tariff(
                        discharge_saved.get(site_id, {}).get("saved_tariff") or None,
                        charge_saved.get(site_id, {}).get("saved_tariff") or None,
                        saved_tariff,  # Fall back to global saved tariff
                        _cached_restorable_tesla_tariff(),
                        _configured_restorable_tesla_tariff(),
                    )
                    if site_tariff:
                        success = await send_tariff_to_tesla(
                            hass,
                            site_id,
                            site_tariff,
                            current_token,
                            provider,
                            fleet_base_url=entry.data.get(CONF_FLEET_API_BASE_URL),
                        )
                        if success:
                            _LOGGER.info("Restored saved tariff for site %s", site_id)
                        else:
                            _LOGGER.error(
                                "Failed to restore saved tariff for site %s", site_id
                            )
                            _mark_tesla_restore_failed(
                                f"tariff restore failed for site {site_id}"
                            )
                    else:
                        _LOGGER.warning(
                            "No restorable saved tariff for site %s; refusing to restore a Tesla v1r force tariff",
                            site_id,
                        )
                    if len(site_configs) > 1:
                        await asyncio.sleep(1)
                    if _restore_superseded("tariff restore"):
                        return
            else:
                # No saved tariff - for tariff-backed spike users this is a problem
                # because sync_tou_schedule intentionally skips these providers.
                if force_restore:
                    _LOGGER.info(
                        "Restore normal: force restore requested without saved tariff; leaving current tariff unchanged"
                    )
                elif electricity_provider in ("agl", "globird", "aemo_vpp"):
                    _LOGGER.warning(
                        "No saved tariff to restore for %s user - tariff may need manual reconfiguration",
                        electricity_provider,
                    )
                    try:
                        from .automations.actions import _send_expo_push

                        await _send_expo_push(
                            hass,
                            "Battery Alert",
                            "Tariff not restored - check TOU schedule",
                        )
                    except Exception as notify_err:
                        _LOGGER.debug(f"Could not send notification: {notify_err}")
                else:
                    _LOGGER.warning("No saved tariff to restore, triggering sync")
                    await hass.services.async_call(
                        DOMAIN, SERVICE_SYNC_TOU, {}, blocking=True
                    )
                    if _restore_superseded("fallback TOU sync"):
                        return

            # Restore operation mode on all gateways. Safe reserve targets are
            # collected and applied as the final dispatch-waking step after
            # every other Tesla setting has been restored.
            restore_reserve_targets: list[tuple[tuple[str, str, str], int]] = []
            for site_id, current_token, provider in site_configs:
                headers = {
                    "Authorization": f"Bearer {current_token}",
                    "Content-Type": "application/json",
                }
                api_base = get_tesla_api_base_url(
                    provider, entry.data.get(CONF_FLEET_API_BASE_URL)
                )

                # Per-site saved mode, or fall back to global saved mode
                discharge_saved = force_discharge_state.get("saved_states") or {}
                charge_saved = force_charge_state.get("saved_states") or {}
                restore_mode = (
                    discharge_saved.get(site_id, {}).get("saved_operation_mode")
                    or charge_saved.get(site_id, {}).get("saved_operation_mode")
                    or force_discharge_state.get("saved_operation_mode")
                    or force_charge_state.get("saved_operation_mode")
                    or "autonomous"
                )
                if force_restore and restore_mode != "self_consumption":
                    _LOGGER.info(
                        "Force restore: leaving Tesla in self_consumption instead of restoring saved mode %s",
                        restore_mode,
                    )
                    restore_mode = "self_consumption"
                elif optimizer_owned_restore and restore_mode != "self_consumption":
                    _LOGGER.info(
                        "Optimizer restore: leaving Tesla in self_consumption "
                        "instead of restoring saved mode %s during tariff handoff",
                        restore_mode,
                    )
                    restore_mode = "self_consumption"
                mode_result = await _tesla_force_apply_operation_mode(
                    [(site_id, current_token, provider)],
                    restore_mode,
                    reason="restore normal",
                    prefer_local=(site_id == site_configs[0][0]),
                )
                mode_ok = _tesla_force_result_all_confirmed(
                    mode_result,
                    [(site_id, current_token, provider)],
                )
                if mode_ok:
                    if restore_mode == "self_consumption":
                        restore_entry_data = hass.data.setdefault(
                            DOMAIN, {}
                        ).setdefault(entry.entry_id, {})
                        restore_entry_data.pop("last_force_toggle_time", None)
                        restore_entry_data.pop("retoggle_attempted", None)
                else:
                    _LOGGER.warning(
                        "Could not restore operation mode for site %s after retries",
                        site_id,
                    )
                    _mark_tesla_restore_failed(
                        f"operation mode restore failed for site {site_id}"
                    )
                    try:
                        from .automations.actions import _send_expo_push

                        await _send_expo_push(
                            hass,
                            "Battery Alert",
                            "Mode restore failed - check settings",
                        )
                    except Exception as notify_err:
                        _LOGGER.debug(f"Could not send notification: {notify_err}")

                # Restore backup reserve per site
                saved_backup_reserve = discharge_saved.get(site_id, {}).get(
                    "saved_backup_reserve"
                )
                if saved_backup_reserve is None:
                    saved_backup_reserve = charge_saved.get(site_id, {}).get(
                        "saved_backup_reserve"
                    )
                if saved_backup_reserve is None:
                    saved_backup_reserve = force_discharge_state.get(
                        "saved_backup_reserve"
                    )
                if saved_backup_reserve is None:
                    saved_backup_reserve = force_charge_state.get(
                        "saved_backup_reserve"
                    )
                if saved_backup_reserve is None and restore_was_hold_soc:
                    saved_backup_reserve = _saved_hold_soc_backup_reserve()
                    if saved_backup_reserve is not None:
                        _LOGGER.info(
                            "Restore normal: restoring Hold SoC backup reserve to user reserve %d%%",
                            saved_backup_reserve,
                        )
                was_discharging = restore_was_force_discharging

                if saved_backup_reserve is None:
                    _LOGGER.warning(
                        "No saved backup reserve for site %s - will not change current setting",
                        site_id,
                    )
                else:
                    should_restore_reserve = True
                    if was_discharging:
                        try:
                            coordinator = hass.data[DOMAIN][entry.entry_id].get(
                                "coordinator"
                            )
                            if coordinator and coordinator.data:
                                current_soc = coordinator.data.get("battery_level", 100)
                                if current_soc < saved_backup_reserve:
                                    _LOGGER.warning(
                                        "SoC (%.1f%%) is below saved backup reserve (%d%%) - "
                                        "skipping reserve restore to prevent grid imports",
                                        current_soc,
                                        saved_backup_reserve,
                                    )
                                    should_restore_reserve = False
                                    try:
                                        from .automations.actions import _send_expo_push

                                        await _send_expo_push(
                                            hass,
                                            "Battery",
                                            f"Reserve at 0% (battery {current_soc:.0f}%) - set manually",
                                        )
                                    except Exception as notify_err:
                                        _LOGGER.debug(
                                            f"Could not send notification: {notify_err}"
                                        )
                                else:
                                    _LOGGER.info(
                                        "SoC (%.1f%%) above saved reserve (%d%%) - safe to restore",
                                        current_soc,
                                        saved_backup_reserve,
                                    )
                        except Exception as e:
                            _LOGGER.warning(
                                f"Could not check SoC for reserve restore: {e}"
                            )

                    if should_restore_reserve:
                        normalized_reserve = _normalize_tesla_backup_reserve_percent(
                            saved_backup_reserve
                        )
                        restore_reserve_targets.append(
                            (
                                (site_id, current_token, provider),
                                normalized_reserve,
                            )
                        )

                # Restore export rule per site
                saved_export_rule = discharge_saved.get(site_id, {}).get(
                    "saved_export_rule"
                )
                if saved_export_rule is None:
                    saved_export_rule = force_discharge_state.get("saved_export_rule")
                if saved_export_rule and saved_export_rule != "battery_ok":
                    _LOGGER.info(
                        "Restoring export rule to %s for site %s",
                        saved_export_rule,
                        site_id,
                    )
                    async with session.post(
                        f"{api_base}/api/1/energy_sites/{site_id}/grid_import_export",
                        headers=headers,
                        json={"customer_preferred_export_rule": saved_export_rule},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            _LOGGER.info(
                                "Restored export rule to %s for site %s",
                                saved_export_rule,
                                site_id,
                            )
                            try:
                                await update_cached_export_rule(saved_export_rule)
                            except Exception as cache_err:
                                _LOGGER.debug(
                                    "Could not cache restored export rule: %s",
                                    cache_err,
                                )
                        else:
                            _LOGGER.warning(
                                "Could not restore export rule for site %s: %s",
                                site_id,
                                response.status,
                            )
                            _mark_tesla_restore_failed(
                                f"export rule restore failed for site {site_id}"
                            )
                if _restore_superseded("mode/reserve restore"):
                    return

            # Restore grid charging to the user's pre-force setting unless
            # demand protection (including its one-minute pre-arm) requires it
            # to stay disabled.
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

            discharge_saved = force_discharge_state.get("saved_states") or {}
            charge_saved = force_charge_state.get("saved_states") or {}
            for site_id, current_token, provider in site_configs:
                target_grid_charging_enabled = _optional_bool(
                    discharge_saved.get(site_id, {}).get("saved_grid_charging_enabled")
                )
                if target_grid_charging_enabled is None:
                    target_grid_charging_enabled = _optional_bool(
                        charge_saved.get(site_id, {}).get("saved_grid_charging_enabled")
                    )
                if target_grid_charging_enabled is None:
                    target_grid_charging_enabled = _optional_bool(
                        force_discharge_state.get("saved_grid_charging_enabled")
                    )
                if target_grid_charging_enabled is None:
                    target_grid_charging_enabled = _optional_bool(
                        force_charge_state.get("saved_grid_charging_enabled")
                    )
                if target_grid_charging_enabled is None:
                    target_grid_charging_enabled = (
                        _remember_tesla_grid_charging_preference(site_id, None)
                    )

                if target_grid_charging_enabled is None:
                    _LOGGER.warning(
                        "No observable or remembered grid charging preference for "
                        "site %s; leaving the current Tesla setting unchanged",
                        site_id,
                    )
                    continue

                restore_grid_result = await _tesla_force_apply_grid_charging(
                    [(site_id, current_token, provider)],
                    target_grid_charging_enabled,
                    reason="restore normal",
                    prefer_local=(site_id == site_configs[0][0]),
                    is_current=lambda target_grid_charging_enabled=target_grid_charging_enabled: (
                        _command_generation[0] == _restore_generation
                        and not target_grid_charging_enabled
                    ),
                )
                if _restore_superseded("grid charging restore"):
                    return
                restore_grid_confirmed = _tesla_force_result_all_confirmed(
                    restore_grid_result,
                    [(site_id, current_token, provider)],
                )
                if (
                    not restore_grid_confirmed
                    and _tesla_force_result_all_grid_field_absent_safe(
                        restore_grid_result,
                        [(site_id, current_token, provider)],
                    )
                ):
                    restore_grid_confirmed = True
                    _LOGGER.warning(
                        "Tesla accepted grid charging restore for site %s and "
                        "site_info did not report the grid-charging field",
                        site_id,
                    )
                if restore_grid_confirmed:
                    _LOGGER.info(
                        "Restored grid charging to %s for site %s",
                        "enabled" if target_grid_charging_enabled else "disabled",
                        site_id,
                    )
                else:
                    _mark_tesla_restore_failed(
                        f"grid charging restore failed for site {site_id}"
                    )

            if skip_backup_reserve_restore and restore_reserve_targets:
                _LOGGER.info(
                    "Restore normal: preserving a newer backup reserve command; "
                    "skipping %d saved reserve target(s)",
                    len(restore_reserve_targets),
                )
                restore_reserve_targets = []

            # A real reserve transition is the final Tesla write. Multiple
            # firmware lines can acknowledge mode/settings changes while
            # retaining stale physical dispatch until reserve changes.
            for site_config, target_reserve in restore_reserve_targets:
                site_id = site_config[0]
                _LOGGER.info(
                    "Restoring backup reserve to %d%% for site %s with final "
                    "dispatch pulse",
                    target_reserve,
                    site_id,
                )
                reserve_result = await _tesla_force_pulse_backup_reserve(
                    [site_config],
                    target_reserve,
                    reason="restore normal final reserve pulse",
                    prefer_local=(site_id == site_configs[0][0]),
                    is_current=lambda: (
                        _command_generation[0] == _restore_generation
                        and _tesla_reserve_generation[0] == _restore_reserve_generation
                    ),
                )
                if _restore_superseded("final reserve pulse"):
                    return
                if _tesla_reserve_generation[0] != _restore_reserve_generation:
                    _LOGGER.info(
                        "Restore reserve target for site %s was superseded by "
                        "a newer reserve command; leaving the newer value in place",
                        site_id,
                    )
                    continue
                if _tesla_force_result_all_confirmed(
                    reserve_result,
                    [site_config],
                ):
                    _LOGGER.info(
                        "Restored backup reserve to %d%% for site %s",
                        target_reserve,
                        site_id,
                    )
                else:
                    _LOGGER.error(
                        "Failed to restore backup reserve for site %s",
                        site_id,
                    )
                    _mark_tesla_restore_failed(
                        f"backup reserve restore failed for site {site_id}"
                    )
                    try:
                        from .automations.actions import _send_expo_push

                        await _send_expo_push(
                            hass,
                            "Battery Alert",
                            f"Reserve restore failed ({target_reserve}%)",
                        )
                    except Exception as notify_err:
                        _LOGGER.debug(
                            "Could not send notification: %s",
                            notify_err,
                        )

            if tesla_restore_failed:
                await persist_pending_tesla_restore(
                    "one or more Tesla restore writes failed",
                    skip_backup_reserve_restore=skip_backup_reserve_restore,
                )
                if _schedule_tesla_restore_retry(
                    "one or more Tesla restore writes failed"
                ):
                    await persist_force_mode_state()
                if confirm_restore:
                    return {
                        "success": False,
                        "error": "one or more Tesla restore writes failed",
                    }
                return

            # Clear discharge state
            force_discharge_state["active"] = False
            force_discharge_state["saved_tariff"] = None
            force_discharge_state["saved_operation_mode"] = None
            force_discharge_state["saved_backup_reserve"] = None
            force_discharge_state["saved_export_rule"] = None
            force_discharge_state["saved_grid_charging_enabled"] = None
            force_discharge_state["saved_states"] = {}
            force_discharge_state["expires_at"] = None
            force_discharge_state["_skip_backup_reserve_restore"] = False

            # Clear charge state
            force_charge_state["active"] = False
            force_charge_state["saved_tariff"] = None
            force_charge_state["saved_operation_mode"] = None
            force_charge_state["saved_backup_reserve"] = None
            force_charge_state["saved_grid_charging_enabled"] = None
            force_charge_state["saved_states"] = {}
            force_charge_state["expires_at"] = None
            force_charge_state["_skip_backup_reserve_restore"] = False

            # A user Hold SoC can coexist with optimizer-owned force metadata.
            # In that mixed state ``hold_only_restore`` is false, so cleanup
            # reaches this full Tesla restore path instead.  Clear the hold
            # only after every restore write has verified; otherwise the
            # persistence write below re-saves the expired hold and reloads
            # keep presenting it as active.
            if restore_was_hold_soc:
                _clear_hold_soc_state()

            await persist_pending_tesla_restore(None)

            _LOGGER.info("NORMAL OPERATION RESTORED")

            # Send push notification for successful restore
            if not suppress_notification:
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(hass, "Battery", "Normal operation restored")
                except Exception as notify_err:
                    _LOGGER.debug(f"Could not send success notification: {notify_err}")

            # Dispatch events for UI
            async_dispatcher_send(
                hass,
                f"{DOMAIN}_force_discharge_state",
                {
                    "active": False,
                    "expires_at": None,
                    "duration": 0,
                },
            )
            async_dispatcher_send(
                hass,
                f"{DOMAIN}_force_charge_state",
                {
                    "active": False,
                    "expires_at": None,
                    "duration": 0,
                },
            )

            # Clear persisted state (no longer needed after restore)
            await persist_force_mode_state()

            if confirm_restore:
                return {"success": True, "error": None}

        except Exception as e:
            _LOGGER.error(f"Error in restore normal: {e}", exc_info=True)
            if confirm_restore:
                return {"success": False, "error": str(e)}

    # Per-brand capability matrix for Hold SoC. "supported=True" means the
    # brand has a primitive that can freeze the battery (block charge AND
    # discharge). "warning" is surfaced to the mobile user as an info alert
    # when the action is best-effort rather than a strict hardware lock.
    HOLD_SOC_CAPS = {
        "tesla": {
            "supported": True,
            "warning": (
                "Tesla holds SoC as a discharge floor, but firmware may still "
                "charge the battery from excess solar. Set Grid Export: "
                "Everything manually for a full lock."
            ),
        },
    }

    _hold_soc_transition_token = object()

    async def handle_hold_battery_soc(call: ServiceCall) -> None:
        """Hold current battery SoC for a duration.

        Suppresses battery movement using each brand's `set_backup_mode`
        coordinator method. Exact behavior is brand-specific: GoodWe EMS
        Conserve blocks on-grid discharge but may still accept excess solar.

        Duration-based with auto-restore via `restore_normal` on expiry.
        Source-aware: optimizer-sourced calls skip state management so the
        user-facing Controls screen never displays automated activity.
        """

        raw_duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        try:
            duration = int(raw_duration)
        except (ValueError, TypeError):
            duration = DEFAULT_DISCHARGE_DURATION
        if duration not in DISCHARGE_DURATIONS:
            duration = DEFAULT_DISCHARGE_DURATION

        source = _control_call_source(call)

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would hold battery SoC for %d minutes (source=%s) — blocked by monitoring mode",
                duration,
                source,
            )
            return

        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

        # Pick the first available coordinator. All brands expose a
        # set_backup_mode method that routes to the right inverter primitive.
        for coord_key, brand in (("tesla_coordinator", "tesla"),):
            coord = entry_data.get(coord_key)
            if coord:
                break
        else:
            _LOGGER.error("Hold SoC: no battery coordinator available")
            return

        _LOGGER.info(
            "🔒 HOLD SoC: activating for %d minutes on %s (source=%s)",
            duration,
            brand,
            source,
        )
        if source != "optimizer" and self_consumption_state.get("active"):
            _clear_self_consumption_state()

        async def _arm_hold_soc_tracking(
            locked_soc: float | None,
            expires_at: datetime,
            *,
            pending: bool,
        ) -> None:
            """Persist a crash-safe Hold state and arm its cleanup timer."""
            if hold_soc_state.get("cancel_expiry_timer"):
                try:
                    hold_soc_state["cancel_expiry_timer"]()
                except Exception:
                    pass

            hold_soc_state["active"] = True
            hold_soc_state["expires_at"] = expires_at
            hold_soc_state["locked_soc"] = locked_soc
            hold_soc_state["brand"] = brand
            hold_soc_state["pending"] = pending
            solaredge_generation = (
                coord.intent_generation if brand == "solaredge" else None
            )
            restore_generation = _command_generation[0]
            restore_reserve_generation = _tesla_reserve_generation[0]

            async def auto_restore_hold_soc(_now):
                if (
                    _command_generation[0] != restore_generation
                    or _tesla_reserve_generation[0] != restore_reserve_generation
                ):
                    _LOGGER.warning(
                        "Hold SoC timer superseded by a newer user control — "
                        "clearing stale tracking without restoring"
                    )
                    _clear_hold_soc_state()
                    await persist_force_mode_state()
                    return
                if hold_soc_state["active"]:
                    _LOGGER.info("⏰ Hold SoC expired, auto-restoring")
                    await hass.services.async_call(
                        DOMAIN,
                        SERVICE_RESTORE_NORMAL,
                        {
                            "source": "hold_soc_cleanup",
                            "_force_restore": True,
                            "_solaredge_generation": solaredge_generation,
                        },
                        blocking=True,
                    )

            hold_soc_state["cancel_expiry_timer"] = async_track_point_in_utc_time(
                hass,
                auto_restore_hold_soc,
                expires_at,
            )
            await persist_force_mode_state()

        tracking_armed = False
        hold_command_generation: int | None = None
        hold_reserve_generation: int | None = None

        # Tesla does not have a single 'set_backup_mode' coordinator
        # primitive — it doesn't expose any way to strictly lock SoC.
        # Closest equivalent is: set backup_reserve = current SoC (capped
        # at 80% — backup reserves above that aren't reliably honoured by
        # Tesla firmware due to BMS taper) AND switch to self_consumption
        # mode so backup_reserve acts as a hard discharge floor.
        # Solar excess can still charge the battery — surfaced as a
        # warning to the user (see HOLD_SOC_CAPS['tesla']).
        if brand == "tesla":
            current_soc = None
            if getattr(coord, "data", None):
                current_soc = coord.data.get("battery_level")
            if current_soc is None:
                _LOGGER.error(
                    "Hold SoC: Tesla SoC unavailable, cannot set backup_reserve"
                )
                return
            target_reserve = max(0, min(round(current_soc), 80))
            saved_backup_reserve, reserve_source = (
                _disabled_optimizer_backup_reserve_target(entry)
            )
            hold_soc_state["saved_backup_reserve"] = saved_backup_reserve
            _LOGGER.info(
                "Hold SoC (Tesla): setting backup_reserve=%d%% (current SoC=%.1f%%) + self_consumption mode; saved reserve=%s%% from %s",
                target_reserve,
                current_soc,
                saved_backup_reserve,
                reserve_source,
            )
            if source != "optimizer":
                await _arm_hold_soc_tracking(
                    current_soc,
                    dt_util.utcnow() + timedelta(seconds=60),
                    pending=True,
                )
                tracking_armed = True

            hold_command_generation = _command_generation[0]
            reserve_generation_before = _tesla_reserve_generation[0]
            hold_reserve_generation = reserve_generation_before + 1
            try:
                await hass.services.async_call(
                    DOMAIN,
                    SERVICE_SET_BACKUP_RESERVE,
                    {"percent": target_reserve, "source": "hold_soc"},
                    blocking=True,
                )
                if (
                    _command_generation[0] != hold_command_generation
                    or _tesla_reserve_generation[0] != hold_reserve_generation
                ):
                    _LOGGER.info(
                        "Hold SoC reserve command was superseded before mode "
                        "transition; preserving the newer command"
                    )
                    if tracking_armed:
                        _clear_hold_soc_state()
                        await persist_force_mode_state()
                    return
                await hass.services.async_call(
                    DOMAIN,
                    "set_self_consumption",
                    {
                        "source": "hold_soc",
                        "_reserve_restore_target": target_reserve,
                        "_hold_soc_transition_token": (_hold_soc_transition_token),
                    },
                    blocking=True,
                )
                if (
                    _command_generation[0] != hold_command_generation
                    or _tesla_reserve_generation[0] != hold_reserve_generation
                ):
                    _LOGGER.info(
                        "Hold SoC transition was superseded; preserving the "
                        "newer command"
                    )
                    if tracking_armed:
                        _clear_hold_soc_state()
                        await persist_force_mode_state()
                    return
                result = True
            except Exception as e:
                _LOGGER.error("Hold SoC failed on Tesla: %s", e, exc_info=True)
                if tracking_armed:
                    still_current = _command_generation[
                        0
                    ] == hold_command_generation and (
                        hold_reserve_generation is None
                        or _tesla_reserve_generation[0] == hold_reserve_generation
                    )
                    if still_current:
                        _LOGGER.warning(
                            "Hold SoC hardware state is unverified; cleanup "
                            "remains armed for 60 seconds"
                        )
                        await _arm_hold_soc_tracking(
                            current_soc,
                            dt_util.utcnow() + timedelta(seconds=60),
                            pending=True,
                        )
                    else:
                        _clear_hold_soc_state()
                        await persist_force_mode_state()
                return

        # Optimizer source: skip state / timer / dispatcher. The LP manages
        # its own lifecycle and we don't want automated activity polluting
        # the user's Controls screen.
        if source == "optimizer":
            _LOGGER.debug("Hold SoC optimizer-sourced — skipping state management")
            return

        # Snapshot current SoC for diagnostics
        soc = None
        if coord and getattr(coord, "data", None):
            soc = coord.data.get("battery_level")

        if brand == "tesla" and (
            _command_generation[0] != hold_command_generation
            or _tesla_reserve_generation[0] != hold_reserve_generation
        ):
            _LOGGER.info(
                "Hold SoC was superseded before activation; preserving the "
                "newer command"
            )
            if tracking_armed:
                _clear_hold_soc_state()
                await persist_force_mode_state()
            return

        await _arm_hold_soc_tracking(
            soc,
            dt_util.utcnow() + timedelta(minutes=duration),
            pending=False,
        )

        # Dispatch mobile-side state event so the Controls screen can show
        # the countdown bar.
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_hold_soc_state",
            {
                "active": True,
                "expires_at": hold_soc_state["expires_at"].isoformat(),
                "duration": duration,
                "locked_soc": soc,
                "warning": HOLD_SOC_CAPS.get(brand, {}).get("warning"),
            },
        )

        _LOGGER.info(
            "✅ Hold SoC ACTIVE for %d min on %s (locked_soc=%s)",
            duration,
            brand,
            soc,
        )

    async def handle_set_self_consumption(call: ServiceCall) -> None:
        """Set battery to pure self-consumption mode (no TOU optimization).

        Used by optimizer for CONSUME action and by the mobile Self-Use
        button. When the caller supplies source='optimizer' (LP executor),
        the handler executes the hardware write only and skips updating
        self_consumption_state — that keeps automated activity out of the
        user-facing Controls screen toggle. Manual calls (source='user' or a
        Home Assistant user context) also flip self_consumption_state['active']=True
        so the mobile UI can render the toggle on.

        Unlike restore_normal, this:
        - Sets mode to self_consumption (not autonomous)
        - Does NOT restore TOU tariff
        - Does NOT send push notifications
        """

        hold_soc_reserve_target = None
        raw_hold_target = call.data.get("_reserve_restore_target")
        try:
            if isinstance(raw_hold_target, bool):
                raise TypeError("Hold SoC reserve target is a boolean, expected int")
            hold_soc_reserve_target = _normalize_tesla_backup_reserve_percent(
                round(float(raw_hold_target))
            )
        except (TypeError, ValueError):
            raise HomeAssistantError("Hold SoC reserve target is missing or invalid")

        source = _control_call_source(call)
        raw_duration = call.data.get("duration", DEFAULT_DISCHARGE_DURATION)
        try:
            duration = int(raw_duration)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Could not convert self-consumption duration %r to int, using default %d",
                raw_duration,
                DEFAULT_DISCHARGE_DURATION,
            )
            duration = DEFAULT_DISCHARGE_DURATION
        if duration not in DISCHARGE_DURATIONS:
            _LOGGER.warning(
                "Self-consumption duration %s not in allowed values %s, using default %d",
                duration,
                DISCHARGE_DURATIONS,
                DEFAULT_DISCHARGE_DURATION,
            )
            duration = DEFAULT_DISCHARGE_DURATION

        _LOGGER.info(
            "Setting pure self-consumption mode for %d minutes (source=%s)",
            duration,
            source,
        )

        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        # Pick the first available coordinator. All brands expose a
        # set_backup_mode method that routes to the right inverter primitive.
        for coord_key, brand in (("tesla_coordinator", "tesla"),):
            coord = entry_data.get(coord_key)
            if coord:
                break
        else:
            _LOGGER.error("Hold SoC: no battery coordinator available")
            return

        # Tesla Powerwall
        try:
            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                raise HomeAssistantError(
                    "Tesla site configuration unavailable for Hold SoC"
                )
                _LOGGER.error(
                    "Missing Tesla site ID or token for self-consumption mode"
                )
                return
            self_consumption_generation = _command_generation[0]

            def _self_consumption_still_current() -> bool:
                return (
                    _command_generation[0] == self_consumption_generation
                    and _tesla_reserve_generation[0] == hold_soc_reserve_target
                )

            async def _write_tesla_self_consumption() -> dict[str, list[str]]:
                result = await _tesla_force_apply_operation_mode(
                    site_configs,
                    "self_consumption",
                    reason="self-consumption",
                    guard_write=hold_soc_reserve_target,
                )
                return result

            mode_result = await _write_tesla_self_consumption()
            mode_ok = _tesla_force_result_all_confirmed(
                mode_result,
                site_configs,
            )

            if mode_ok:
                target_reserve = hold_soc_reserve_target
                target_source = "Hold SoC target"
                self_entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(
                    entry.entry_id, {}
                )
                if target_reserve is None:
                    target_source = "configured reserve fallback"
                    opt_coord = self_entry_data.get("optimization_coordinator")
                    if opt_coord is not None and hasattr(
                        opt_coord,
                        "resolve_restore_target",
                    ):
                        target_reserve = await opt_coord.resolve_restore_target()
                        if target_reserve is not None:
                            target_source = "optimizer restore target"
                    if target_reserve is None:
                        target_reserve, target_source = (
                            _disabled_optimizer_backup_reserve_target(entry)
                        )
                if target_reserve is not None:
                    target_reserve = _normalize_tesla_backup_reserve_percent(
                        target_reserve
                    )
                    _LOGGER.info(
                        "Tesla self-consumption: waking dispatch and restoring "
                        "configured reserve to %d%% (%s)",
                        target_reserve,
                        target_source,
                    )
                    reserve_result = await _tesla_force_pulse_backup_reserve(
                        site_configs,
                        target_reserve,
                        reason="self-consumption final reserve pulse",
                        is_current=_self_consumption_still_current,
                    )
                    if not _tesla_force_result_all_confirmed(
                        reserve_result,
                        site_configs,
                    ):
                        _LOGGER.error(
                            "Tesla self-consumption mode verified but final "
                            "reserve pulse did not verify for every site"
                        )
                        raise HomeAssistantError(
                            "Tesla self-consumption reserve transition did not verify"
                        )
                self_entry_data.pop("last_force_toggle_time", None)
                self_entry_data.pop("retoggle_attempted", None)
                tesla_coord_for_cache = self_entry_data.get("tesla_coordinator")
                if tesla_coord_for_cache is not None:
                    tesla_coord_for_cache.invalidate_site_info_cache()
            else:
                _LOGGER.error(
                    "Tesla self-consumption mode did not verify for every site; "
                    "skipping reserve pulse"
                )
                raise HomeAssistantError("Tesla self-consumption mode did not verify")

            # Do NOT clear force_charge_state/force_discharge_state here.
            # If force charge/discharge is active, the expiry timer owns cleanup
            # (restoring backup_reserve, tariff, and mode). Clearing active=False
            # here would orphan the timer and leave backup_reserve stuck at 100%.

        except HomeAssistantError:
            raise
        except Exception as e:
            _LOGGER.error(f"Error setting self-consumption mode: {e}", exc_info=True)

    async def handle_set_autonomous(call: ServiceCall) -> None:
        """Set battery to autonomous (TOU) mode.

        Used by optimizer for IDLE action — Tesla needs autonomous mode for
        backup_reserve to act as a hard floor that prevents discharge.
        In self_consumption mode, backup_reserve alone is not reliably enforced.
        """
        source = _control_call_source(call)

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set autonomous mode (source=%s) — blocked by monitoring mode",
                source or "unknown",
            )
            return

        _autonomous_guard = (
            hass.data.get(DOMAIN, {})
            .get(entry.entry_id, {})
            .get("network_export_guard")
        )
        if (
            _autonomous_guard is not None
            and _autonomous_guard.manager.snapshot.mode != "off"
        ):
            _LOGGER.warning(
                "Autonomous/TOU mode blocked while the network envelope is %s; "
                "this tariff-driven mode cannot accept a watt-level export clamp",
                _autonomous_guard.manager.snapshot.mode,
            )
            return

        _LOGGER.info("Optimizer: Setting autonomous (TOU) mode")

        # Tesla Powerwall
        try:
            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                _LOGGER.error("Missing Tesla site ID or token for autonomous mode")
                return

            session = async_get_clientsession(hass)
            for site_id, current_token, provider in site_configs:
                headers = {
                    "Authorization": f"Bearer {current_token}",
                    "Content-Type": "application/json",
                }
                api_base = get_tesla_api_base_url(
                    provider, entry.data.get(CONF_FLEET_API_BASE_URL)
                )

                async with session.post(
                    f"{api_base}/api/1/energy_sites/{site_id}/operation",
                    headers=headers,
                    json={"default_real_mode": "autonomous"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status == 200:
                        _LOGGER.info(
                            "Tesla site %s set to autonomous (TOU) mode", site_id
                        )
                    else:
                        text = await response.text()
                        _LOGGER.warning(
                            "Could not set autonomous mode for site %s: %s - %s",
                            site_id,
                            response.status,
                            text,
                        )

        except Exception as e:
            _LOGGER.error(f"Error setting autonomous mode: {e}", exc_info=True)

    # ======================================================================
    # POWERWALL SETTINGS SERVICES (for mobile app Controls)
    # ======================================================================

    async def handle_set_backup_reserve(
        call: ServiceCall,
    ) -> dict[str, Any] | None:
        """Set the battery backup reserve percentage.

        Supports both Tesla Powerwall and SigEnergy systems.
        """
        response: dict[str, Any] | None = None
        percent = call.data.get("percent")
        if percent is None:
            _LOGGER.error("Missing 'percent' parameter for set_backup_reserve")
            return {"success": False, "error": "missing percent"}

        try:
            percent = int(percent)
            if percent < 0 or percent > 100:
                _LOGGER.error(
                    f"Invalid backup reserve percent: {percent}. Must be 0-100."
                )
                return {"success": False, "error": "invalid backup reserve percent"}
        except (ValueError, TypeError):
            _LOGGER.error(f"Invalid backup reserve percent: {percent}")
            return {"success": False, "error": "invalid backup reserve percent"}

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set backup reserve to %d%% — blocked by monitoring mode",
                percent,
            )
            return {"success": False, "error": "blocked by monitoring mode"}

        reserve_source = call.data.get("source")
        if reserve_source == "optimizer" and hold_soc_state.get("active"):
            _LOGGER.info(
                "Hold SoC active — skipping optimizer backup reserve write to %d%%",
                percent,
            )
            return {"success": False, "error": "blocked by hold SoC"}

        _tesla_reserve_generation[0] += 1
        backup_reserve_kick_generation = _supersede_tesla_charge_kick(
            "set backup reserve"
        )
        backup_reserve_command_generation = _command_generation[0]
        _LOGGER.info(f"🔋 Setting backup reserve to {percent}%")

        # Tesla reserve uses the verified user-scale cloud API.
        tesla_success = False
        try:
            # Tesla constraint (July 2025): only 0-80% and 100% are valid.
            # Values 81-99% are rejected and auto-clamped to 80%.
            if 81 <= percent <= 99:
                _LOGGER.info(
                    "Clamping backup reserve %d%% → 100%% (Tesla rejects 81-99%%)",
                    percent,
                )
                percent = 100

            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                _LOGGER.error("Missing Tesla site ID or token for set_backup_reserve")
                reserve_result = {
                    "confirmed_sites": [],
                    "accepted_sites": [],
                    "failed_sites": [],
                }
            else:
                reserve_result = await _tesla_force_apply_backup_reserve(
                    site_configs,
                    percent,
                    reason="set backup reserve",
                )
            tesla_success = _tesla_force_result_all_confirmed(
                reserve_result,
                site_configs,
            )
            if tesla_success:
                _tesla_coord_for_cache = (
                    hass.data.get(DOMAIN, {})
                    .get(entry.entry_id, {})
                    .get("tesla_coordinator")
                )
                if _tesla_coord_for_cache is not None:
                    _tesla_coord_for_cache.invalidate_site_info_cache()
                await refresh_powerwall_local_after_settings_write("set_backup_reserve")
                if percent == 100:
                    if (
                        _tesla_charge_kick_generation[0]
                        == backup_reserve_kick_generation
                        and _command_generation[0] == backup_reserve_command_generation
                    ):
                        _schedule_tesla_charge_kick(
                            "backup_reserve_100",
                            allow_grid_field_absent_compatibility=True,
                        )
            else:
                _LOGGER.error(
                    "Tesla backup reserve %d%% was not confirmed for every site",
                    percent,
                )
            response = (
                {"success": True, "error": None}
                if tesla_success
                else {
                    "success": False,
                    "error": "backup reserve write did not verify",
                }
            )

        except Exception as e:
            _LOGGER.error(f"Error setting Tesla backup reserve: {e}", exc_info=True)
            response = {"success": False, "error": str(e)}

        # Persist the user's chosen backup reserve so the optimizer knows
        # the correct restore value (survives HA restarts and IDLE cycles).
        # Skip optimizer-originated writes — LP actions may temporarily adjust
        # backup_reserve to hold or align SOC and must not overwrite the real value.
        if isinstance(response, dict) and response.get("success") is not True:
            _LOGGER.debug("Skipping backup reserve persistence after unconfirmed write")
            return response
        try:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            opt_coord = entry_data.get("optimization_coordinator")
            optimizer_is_idle = opt_coord is not None and getattr(
                opt_coord, "_idle_reserve_adjustment", False
            )
            optimizer_write = (
                reserve_source
                in (
                    "optimizer",
                    "automation_preserve_charge",
                    "hold_soc",
                    "hold_soc_restore",
                )
                or optimizer_is_idle
            )
            if not optimizer_write:
                if opt_coord:
                    opt_coord._startup_backup_reserve = percent
                    if hasattr(opt_coord, "_sync_brand_restore_targets"):
                        opt_coord._sync_brand_restore_targets(percent)
                    optimizer_model = getattr(opt_coord, "_optimizer", None)
                    if optimizer_model:
                        optimizer_model.update_hardware_reserve(percent / 100)
                new_data = dict(entry.data)
                new_opts = dict(entry.options)
                hardware_reserve = percent / 100
                new_data[CONF_HARDWARE_BACKUP_RESERVE] = hardware_reserve
                new_opts[CONF_HARDWARE_BACKUP_RESERVE] = hardware_reserve
                new_opts.pop("_user_backup_reserve", None)
                if new_data != dict(entry.data) or new_opts != dict(entry.options):
                    entry_data["_skip_reload"] = True
                hass.config_entries.async_update_entry(
                    entry,
                    data=new_data,
                    options=new_opts,
                )
                _LOGGER.info(
                    "Persisted canonical hardware backup reserve: %d%%",
                    percent,
                )
            else:
                _LOGGER.debug(
                    "Skipping backup reserve persistence (source=%s, idle_adjustment=%s, percent=%d%%)",
                    reserve_source or "unknown",
                    optimizer_is_idle,
                    percent,
                )
        except Exception as e:
            _LOGGER.debug("Could not persist backup reserve: %s", e)

        return response

    async def handle_set_operation_mode(call: ServiceCall) -> None:
        """Set the Powerwall operation mode.

        Local V1R first when paired; cloud Fleet API as fallback.
        """
        mode = call.data.get("mode")
        if mode not in ("autonomous", "self_consumption", "backup"):
            _LOGGER.error(
                f"Invalid operation mode: {mode}. Must be 'autonomous', 'self_consumption', or 'backup'."
            )
            return

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set operation mode to %s — blocked by monitoring mode",
                mode,
            )
            return

        _supersede_tesla_charge_kick(
            "set operation mode",
            owns_operation_mode=True,
        )
        _LOGGER.info(f"⚙️ Setting operation mode to {mode}")

        from .const import CONF_POWERWALL_LOCAL_DIN
        from .powerwall_local.dispatch import dispatch_powerwall_write

        async def _local(transport) -> bool:
            din = entry.data.get(CONF_POWERWALL_LOCAL_DIN)
            if not din:
                return False
            # default_real_mode lives at the top level of config.json, not under site_info.
            if not await transport.write_config(din, {"default_real_mode": mode}):
                return False
            for attempt in range(1, 4):
                if attempt > 1:
                    await asyncio.sleep(2)
                config = await transport.read_config(din)
                observed_mode = (
                    config.get("default_real_mode")
                    if isinstance(config, dict)
                    else None
                )
                if observed_mode == mode:
                    _LOGGER.info(
                        "Confirmed local Tesla operation mode %s for DIN %s (attempt %d/3)",
                        mode,
                        din,
                        attempt,
                    )
                    return True
                _LOGGER.warning(
                    "Local Tesla operation mode readback for DIN %s is %s, expected %s (attempt %d/3)",
                    din,
                    observed_mode,
                    mode,
                    attempt,
                )
            return False

        async def _cloud() -> bool:
            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                _LOGGER.error("Missing Tesla site ID or token for set_operation_mode")
                return False

            any_ok = False
            session = async_get_clientsession(hass)

            async def _post_mode(
                api_base: str,
                site_id: str,
                headers: dict[str, str],
                requested_mode: str,
            ) -> tuple[bool, int | None, str]:
                try:
                    async with session.post(
                        f"{api_base}/api/1/energy_sites/{site_id}/operation",
                        headers=headers,
                        json={"default_real_mode": requested_mode},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        text = await response.text()
                        return response.status == 200, response.status, text
                except asyncio.TimeoutError:
                    return False, None, "timeout"

            async def _read_mode(
                api_base: str,
                site_id: str,
                headers: dict[str, str],
            ) -> tuple[str | None, bool, bool]:
                try:
                    async with session.get(
                        f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status != 200:
                            text = await response.text()
                            _LOGGER.warning(
                                "Tesla operation mode readback failed for site %s: %s - %s",
                                site_id,
                                response.status,
                                text[:200],
                            )
                            return None, False, False
                        data = await response.json()
                        site_info = (
                            data.get("response", data)
                            if isinstance(data, dict)
                            else None
                        )
                        if not isinstance(
                            site_info, dict
                        ) or not tesla_site_info_has_structure(site_info):
                            return None, False, False
                        return (
                            site_info.get("default_real_mode"),
                            "default_real_mode" in site_info,
                            True,
                        )
                except Exception as err:
                    _LOGGER.warning(
                        "Tesla operation mode readback error for site %s: %s",
                        site_id,
                        err,
                    )
                    return None, False, False

            async def _confirm_mode(
                api_base: str,
                site_id: str,
                headers: dict[str, str],
                expected_mode: str,
                *,
                attempts: int = 4,
                delay_seconds: float = 2.0,
            ) -> str:
                valid_site_info_reads = 0
                field_absent_reads = 0
                invalid_site_info_read = False
                for attempt in range(1, attempts + 1):
                    if attempt > 1:
                        await asyncio.sleep(delay_seconds)
                    (
                        observed_mode,
                        field_present,
                        valid_site_info,
                    ) = await _read_mode(api_base, site_id, headers)
                    if valid_site_info:
                        valid_site_info_reads += 1
                        if not field_present:
                            field_absent_reads += 1
                    else:
                        invalid_site_info_read = True
                    if observed_mode == expected_mode:
                        _LOGGER.info(
                            "Confirmed Tesla operation mode %s for site %s (attempt %d/%d)",
                            expected_mode,
                            site_id,
                            attempt,
                            attempts,
                        )
                        return "confirmed"
                    _LOGGER.warning(
                        "Tesla operation mode readback for site %s is %s, expected %s (attempt %d/%d)",
                        site_id,
                        observed_mode,
                        expected_mode,
                        attempt,
                        attempts,
                    )
                if (
                    expected_mode == "self_consumption"
                    and valid_site_info_reads >= 2
                    and field_absent_reads == valid_site_info_reads
                    and not invalid_site_info_read
                ):
                    return "accepted_field_absent"
                return "unconfirmed"

            async def _bounce_to_autonomous(
                api_base: str,
                site_id: str,
                headers: dict[str, str],
            ) -> bool:
                ok, status, text = await _post_mode(
                    api_base,
                    site_id,
                    headers,
                    "self_consumption",
                )
                if not ok:
                    _LOGGER.warning(
                        "Tesla autonomous recovery bounce could not set self_consumption for site %s: %s - %s",
                        site_id,
                        status,
                        text[:200],
                    )
                await asyncio.sleep(5)
                ok, status, text = await _post_mode(
                    api_base,
                    site_id,
                    headers,
                    "autonomous",
                )
                if not ok:
                    _LOGGER.warning(
                        "Tesla autonomous recovery bounce could not set autonomous for site %s: %s - %s",
                        site_id,
                        status,
                        text[:200],
                    )
                    return False
                return (
                    await _confirm_mode(
                        api_base,
                        site_id,
                        headers,
                        "autonomous",
                    )
                    == "confirmed"
                )

            for site_id, current_token, provider in site_configs:
                headers = {
                    "Authorization": f"Bearer {current_token}",
                    "Content-Type": "application/json",
                }
                api_base = get_tesla_api_base_url(
                    provider,
                    entry.data.get(CONF_FLEET_API_BASE_URL),
                )

                # Retry up to 3 times for operation mode
                for attempt in range(1, 4):
                    ok, status, text = await _post_mode(
                        api_base, site_id, headers, mode
                    )
                    if ok:
                        _LOGGER.info(
                            "Operation mode set to %s for site %s", mode, site_id
                        )
                        confirmation = await _confirm_mode(
                            api_base,
                            site_id,
                            headers,
                            mode,
                        )
                        if confirmation == "accepted_field_absent":
                            _LOGGER.warning(
                                "Tesla accepted self_consumption for site %s "
                                "and every valid site_info readback omitted "
                                "default_real_mode",
                                site_id,
                            )
                        if confirmation in (
                            "confirmed",
                            "accepted_field_absent",
                        ):
                            any_ok = True
                            break
                        if mode == "autonomous":
                            _LOGGER.warning(
                                "Tesla site %s did not stay in autonomous after direct write; trying mode bounce",
                                site_id,
                            )
                            if await _bounce_to_autonomous(api_base, site_id, headers):
                                any_ok = True
                                break
                        _LOGGER.error(
                            "Failed to verify operation mode %s for site %s after Tesla accepted the write",
                            mode,
                            site_id,
                        )
                        hass.async_create_task(
                            _notify_api_error(
                                hass,
                                "Mode Change Failed",
                                "Tesla accepted the mode change but readback did not verify",
                            )
                        )
                        break
                    if status in (429, 500, 502, 503, 504):
                        _LOGGER.warning(
                            "Tesla operation mode attempt %d/3 failed for site %s: %s",
                            attempt,
                            site_id,
                            status or text,
                        )
                        if attempt < 3:
                            await asyncio.sleep(2**attempt)
                        else:
                            _LOGGER.error(
                                "Failed to set operation mode for site %s after 3 attempts: %s - %s",
                                site_id,
                                status,
                                text[:200],
                            )
                            hass.async_create_task(
                                _notify_api_error(
                                    hass,
                                    "Mode Change Failed",
                                    f"Could not change Tesla operation mode after 3 attempts - API {status}",
                                )
                            )
                    else:
                        _LOGGER.error(
                            "Failed to set operation mode for site %s: %s - %s",
                            site_id,
                            status,
                            text[:200],
                        )
                        hass.async_create_task(
                            _notify_api_error(
                                hass,
                                "Mode Change Failed",
                                "Could not change Tesla operation mode - API error",
                            )
                        )
                        break
            return any_ok

        try:
            success = await dispatch_powerwall_write(
                hass,
                entry,
                local_call=_local,
                cloud_call=_cloud,
                label="set_operation_mode",
            )
            if success:
                _tesla_coord_for_cache = (
                    hass.data.get(DOMAIN, {})
                    .get(entry.entry_id, {})
                    .get("tesla_coordinator")
                )
                if _tesla_coord_for_cache is not None:
                    _tesla_coord_for_cache.invalidate_site_info_cache()
                if mode == "self_consumption":
                    if entry.entry_id in hass.data[DOMAIN]:
                        hass.data[DOMAIN][entry.entry_id].pop(
                            "last_force_toggle_time", None
                        )
                        _LOGGER.debug(
                            "Cleared last_force_toggle_time (user set self_consumption)"
                        )
                hass.async_create_task(
                    refresh_powerwall_local_after_settings_write("set_operation_mode")
                )
            else:
                raise HomeAssistantError(
                    f"Could not verify Tesla operation mode changed to {mode}"
                )
        except Exception as e:
            _LOGGER.error(f"Error setting operation mode: {e}", exc_info=True)
            raise

    async def handle_set_grid_export(call: ServiceCall) -> None:
        """Set the grid export rule."""
        rule = call.data.get("rule")
        if rule not in ("never", "pv_only", "battery_ok"):
            _LOGGER.error(
                f"Invalid grid export rule: {rule}. Must be 'never', 'pv_only', or 'battery_ok'."
            )
            return

        # A permissive export-rule write can remove an inverter-side cap and
        # increase both managed battery and unmanaged PV export.  Flexible
        # Exports therefore permits only the fail-closed `never` transition;
        # the certified site controller remains the sole authority for
        # raising a connection limit.
        _grid_export_entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        _grid_export_manager = _grid_export_entry_data.get("network_envelope_manager")
        if (
            rule != "never"
            and _grid_export_manager is not None
            and _grid_export_manager.snapshot.mode != "off"
        ):
            _LOGGER.warning(
                "Grid export rule %s blocked while the network envelope is %s",
                rule,
                _grid_export_manager.snapshot.mode,
            )
            return

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set grid export rule to %s — blocked by monitoring mode",
                rule,
            )
            return

        _LOGGER.info(f"📤 Setting grid export rule to {rule}")

        try:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

            # AlphaESS: map rule to export limit register (REG_MAX_FEED_INTO_GRID_PERCENT)
            alphaess_coord = entry_data.get("alphaess_coordinator")
            if (
                alphaess_coord
                and hasattr(alphaess_coord, "_controller")
                and alphaess_coord._controller
            ):
                controller = alphaess_coord._controller
                if rule == "never":
                    success = await controller.curtail()
                    if success:
                        entry_data["alphaess_curtailment_state"] = "curtailed"
                        _LOGGER.info(
                            "AlphaESS: grid export set to never (0%% feed-in limit)"
                        )
                    else:
                        _LOGGER.error("AlphaESS: failed to set zero export")
                else:
                    # battery_ok and pv_only both restore unlimited export
                    success = await controller.restore()
                    if success:
                        entry_data["alphaess_curtailment_state"] = "normal"
                        _LOGGER.info(
                            "AlphaESS: grid export set to %s (export limit restored)",
                            rule,
                        )
                    else:
                        _LOGGER.error("AlphaESS: failed to restore export limit")
                return

            # GoodWe: map rule to export limit register
            gw_coord = entry_data.get("goodwe_coordinator")
            if gw_coord and hasattr(gw_coord, "_controller") and gw_coord._controller:
                controller = gw_coord._controller
                if rule == "never":
                    success = await controller.curtail()
                    if success:
                        entry_data["goodwe_curtailment_state"] = "curtailed"
                        _LOGGER.info(
                            "GoodWe: grid export set to never (export limit = 0W)"
                        )
                    else:
                        _LOGGER.error("GoodWe: failed to set zero export")
                else:
                    # battery_ok and pv_only both map to unrestricted export —
                    # GoodWe has no hardware-level PV-only export restriction
                    success = await controller.restore()
                    if success:
                        entry_data["goodwe_curtailment_state"] = "normal"
                        _LOGGER.info(
                            "GoodWe: grid export set to %s (export limit removed)", rule
                        )
                    else:
                        _LOGGER.error("GoodWe: failed to restore export limit")
                return

            from .const import CONF_POWERWALL_LOCAL_DIN
            from .powerwall_local.dispatch import dispatch_powerwall_write

            async def _local(transport) -> bool:
                din = entry.data.get(CONF_POWERWALL_LOCAL_DIN)
                if not din:
                    return False
                return await transport.write_config(
                    din, {"site_info.customer_preferred_export_rule": rule}
                )

            async def _cloud() -> bool:
                site_configs = _get_tesla_site_configs(hass, entry)
                if not site_configs:
                    _LOGGER.debug(
                        "set_grid_export: no Tesla site config (non-Tesla system)"
                    )
                    return False

                any_ok = False
                session = async_get_clientsession(hass)
                for site_id, current_token, provider in site_configs:
                    headers = {
                        "Authorization": f"Bearer {current_token}",
                        "Content-Type": "application/json",
                    }
                    api_base = get_tesla_api_base_url(
                        provider, entry.data.get(CONF_FLEET_API_BASE_URL)
                    )

                    async with session.post(
                        f"{api_base}/api/1/energy_sites/{site_id}/grid_import_export",
                        headers=headers,
                        json={"customer_preferred_export_rule": rule},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as response:
                        if response.status == 200:
                            _LOGGER.info(
                                "Grid export rule set to %s for site %s", rule, site_id
                            )
                            any_ok = True
                        else:
                            text = await response.text()
                            _LOGGER.error(
                                "Failed to set grid export rule for site %s: %s - %s",
                                site_id,
                                response.status,
                                text,
                            )
                return any_ok

            success = await dispatch_powerwall_write(
                hass,
                entry,
                local_call=_local,
                cloud_call=_cloud,
                label="set_grid_export",
            )
            if success:
                entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(
                    entry.entry_id, {}
                )
                local_coord = (entry_data.get("powerwall_local") or {}).get(
                    "coordinator"
                )
                local_snapshot = getattr(local_coord, "data", None)
                if local_snapshot is not None:
                    local_snapshot.grid_export_rule = rule
                _tesla_coord_for_cache = (
                    hass.data.get(DOMAIN, {})
                    .get(entry.entry_id, {})
                    .get("tesla_coordinator")
                )
                if _tesla_coord_for_cache is not None:
                    _tesla_coord_for_cache.invalidate_site_info_cache()
                solar_curtailment_enabled = entry.options.get(
                    CONF_BATTERY_CURTAILMENT_ENABLED,
                    entry.data.get(CONF_BATTERY_CURTAILMENT_ENABLED, False),
                )
                if solar_curtailment_enabled:
                    entry_data["manual_export_override"] = True
                    entry_data["manual_export_rule"] = rule
                    _LOGGER.info("Manual export override enabled: %s", rule)
                await update_cached_export_rule(rule)
                hass.async_create_task(
                    refresh_powerwall_local_after_settings_write("set_grid_export")
                )
                if solar_curtailment_enabled:
                    # Persist so the override survives HA restarts / config reloads
                    try:
                        _store = entry_data.get("store")
                        if _store:
                            _sd = await _store.async_load() or {}
                            _sd["manual_export_override"] = True
                            _sd["manual_export_rule"] = rule
                            await _store.async_save(_sd)
                    except Exception as _persist_err:
                        _LOGGER.debug(
                            "Could not persist manual_export_override: %s", _persist_err
                        )

        except Exception as e:
            _LOGGER.error(f"Error setting grid export rule: {e}", exc_info=True)

    async def handle_set_grid_export_auto(call: ServiceCall) -> None:
        """Clear manual export override and return to automatic control."""
        _LOGGER.info("🔄 Clearing manual export override - returning to auto control")
        try:
            entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
            entry_data["manual_export_override"] = False
            entry_data["manual_export_rule"] = None
            # Clear persisted override so it doesn't come back after a reload
            try:
                _store = entry_data.get("store")
                if _store:
                    _sd = await _store.async_load() or {}
                    _sd["manual_export_override"] = False
                    _sd["manual_export_rule"] = None
                    await _store.async_save(_sd)
            except Exception as _persist_err:
                _LOGGER.debug(
                    "Could not clear persisted manual_export_override: %s", _persist_err
                )
            _LOGGER.info("✅ Manual export override cleared")
        except Exception as e:
            _LOGGER.error(f"Error clearing manual export override: {e}", exc_info=True)

    async def handle_set_grid_charging(
        call: ServiceCall,
    ) -> dict[str, Any] | None:
        """Enable or disable grid charging, preferring paired local V1R."""
        enabled = call.data.get("enabled")
        if enabled is None:
            raise HomeAssistantError(
                "Missing 'enabled' parameter for set_grid_charging"
            )

        # Convert to bool (HA may pass True/False or "true"/"false")
        if isinstance(enabled, str):
            enabled = enabled.lower() == "true"
        enabled = bool(enabled)

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set grid charging to %s — blocked by monitoring mode",
                "enabled" if enabled else "disabled",
            )
            return {"success": False, "error": "blocked by monitoring mode"}

        _supersede_tesla_charge_kick("set grid charging")
        _LOGGER.info(
            f"🔌 Setting grid charging to {'enabled' if enabled else 'disabled'}"
        )

        try:
            site_configs = _get_tesla_site_configs(hass, entry)
            if not site_configs:
                raise HomeAssistantError(
                    "Missing Tesla site ID or token for set_grid_charging"
                )
            result = await _tesla_force_apply_grid_charging(
                site_configs,
                enabled,
                reason="set grid charging",
            )
            source = _control_call_source(call)
            grid_confirmed = _tesla_force_result_all_confirmed(
                result,
                site_configs,
            )
            if not grid_confirmed and _tesla_force_result_all_grid_field_absent_safe(
                result,
                site_configs,
            ):
                grid_confirmed = True
                _LOGGER.warning(
                    "Tesla %s grid charging command accepted and site_info did "
                    "not report the grid-charging field",
                    source,
                )
            if not grid_confirmed:
                raise HomeAssistantError(
                    "Tesla accepted the grid charging command but the setting did not verify"
                )

            await _persist_tesla_grid_charging_preference(
                site_configs,
                enabled,
                source=f"{source} set_grid_charging",
            )
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            local_coord = (entry_data.get("powerwall_local") or {}).get("coordinator")
            local_snapshot = getattr(local_coord, "data", None)
            if local_snapshot is not None:
                local_snapshot.grid_charging_enabled = enabled
            tesla_coord = entry_data.get("tesla_coordinator")
            if tesla_coord is not None:
                tesla_coord.invalidate_site_info_cache()
            hass.async_create_task(
                refresh_powerwall_local_after_settings_write("set_grid_charging")
            )
            return {"success": True, "error": None}
        except HomeAssistantError:
            raise
        except Exception as e:
            _LOGGER.error(f"Error setting grid charging: {e}", exc_info=True)
            raise HomeAssistantError(f"Could not set Tesla grid charging: {e}") from e

    def _get_tesla_coordinator_for_service(service_name: str):
        """Return the Tesla energy coordinator for this entry, or None with a log."""
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        coord = entry_data.get("tesla_coordinator")
        if coord is None:
            _LOGGER.error("%s: Tesla energy coordinator not available", service_name)
        return coord

    async def handle_set_storm_watch(call: ServiceCall) -> None:
        """Enable or disable Tesla Storm Watch for the energy site.

        Cloud-only — storm_mode_enabled isn't stored in the gateway's local
        config.json (verified empirically against PW3 firmware 26.2.1). Tesla
        keeps storm mode as cloud-side state.
        """
        enabled = call.data.get("enabled")
        if enabled is None:
            _LOGGER.error("Missing 'enabled' parameter for set_storm_watch")
            return
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in ("true", "1", "yes", "on")
        enabled = bool(enabled)

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set Storm Watch to %s — blocked by monitoring mode",
                "enabled" if enabled else "disabled",
            )
            return

        coord = _get_tesla_coordinator_for_service("set_storm_watch")
        if coord is None:
            return
        if not coord.tesla_capabilities.get("storm_mode", True):
            _LOGGER.warning("set_storm_watch: site does not support storm mode")
            return

        _LOGGER.info(
            "⛈️ Setting Storm Watch to %s", "enabled" if enabled else "disabled"
        )
        try:
            await coord.async_set_storm_watch(enabled)
        except Exception as e:
            _LOGGER.error("Error setting storm watch: %s", e, exc_info=True)

    async def handle_set_off_grid_ev_reserve(call: ServiceCall) -> None:
        """Set the off-grid vehicle charging reserve percentage."""
        percent = call.data.get("percent")
        if percent is None:
            _LOGGER.error("Missing 'percent' parameter for set_off_grid_ev_reserve")
            return
        try:
            percent = int(percent)
        except (ValueError, TypeError):
            _LOGGER.error("Invalid off-grid EV reserve percent: %r", percent)
            return
        if percent < 0 or percent > 100:
            _LOGGER.error("Off-grid EV reserve percent out of range: %d", percent)
            return

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would set off-grid EV reserve to %d%% — blocked by monitoring mode",
                percent,
            )
            return

        coord = _get_tesla_coordinator_for_service("set_off_grid_ev_reserve")
        if coord is None:
            return
        if not coord.tesla_capabilities.get("off_grid_vehicle_charging_reserve", True):
            _LOGGER.warning(
                "set_off_grid_ev_reserve: site does not support this feature"
            )
            return

        _LOGGER.info("🔋 Setting off-grid EV reserve to %d%%", percent)
        try:
            await coord.async_set_off_grid_ev_reserve(percent)
        except Exception as e:
            _LOGGER.error("Error setting off-grid EV reserve: %s", e, exc_info=True)

    async def handle_set_vpp_enrollment(call: ServiceCall) -> None:
        """Enroll or unenroll the site in a Tesla VPP / grid-services program."""
        program_id = call.data.get("program_id")
        enrolled = call.data.get("enrolled")
        if not program_id or enrolled is None:
            _LOGGER.error("set_vpp_enrollment requires 'program_id' and 'enrolled'")
            return
        if isinstance(enrolled, str):
            enrolled = enrolled.strip().lower() in ("true", "1", "yes", "on")
        enrolled = bool(enrolled)

        if _monitoring_mode_should_block_control(call):
            _LOGGER.info(
                "[MONITORING] Would %s VPP program %s — blocked by monitoring mode",
                "enroll in" if enrolled else "unenroll from",
                program_id,
            )
            return

        coord = _get_tesla_coordinator_for_service("set_vpp_enrollment")
        if coord is None:
            return
        if not coord.tesla_capabilities.get("vpp_programs", True):
            _LOGGER.warning("set_vpp_enrollment: site is not eligible for VPP programs")
            return

        _LOGGER.info(
            "📡 %s VPP program %s",
            "Enrolling in" if enrolled else "Unenrolling from",
            program_id,
        )
        try:
            await coord.async_set_vpp_enrollment(str(program_id), enrolled)
        except Exception as e:
            _LOGGER.error("Error setting VPP enrollment: %s", e, exc_info=True)

    async def _persist_max_backup_schedule(payload: dict | None) -> None:
        """Write or clear the max_backup_schedule key in this entry's Store.

        ``payload=None`` clears the key. Survives HA restart so an in-flight
        schedule can resume; without this the Powerwall would stay at 100%
        indefinitely after a reboot mid-window.
        """
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        store = entry_data.get("store")
        if store is None:
            return
        try:
            data = await store.async_load() or {}
            if payload is None:
                data.pop("max_backup_schedule", None)
            else:
                data["max_backup_schedule"] = payload
            await store.async_save(data)
        except Exception as err:
            _LOGGER.warning("Failed to persist max_backup_schedule: %s", err)

    async def _restore_max_backup_window(
        end_ts: float,
        saved_reserve: int | None,
        local_event: bool = False,
    ) -> None:
        """Re-arm the restoration timer for an already-active window.

        End time and saved reserve come from the persisted Store payload.
        If the window already expired while HA was down, restore immediately;
        otherwise schedule the restore for the remaining seconds.
        """
        import time as _time

        from homeassistant.helpers.event import async_call_later

        now = _time.time()
        remaining = max(0.0, end_ts - now)
        entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        entry_data["max_backup_saved_reserve"] = saved_reserve
        entry_data["max_backup_end_ts"] = end_ts
        entry_data["max_backup_local_event"] = local_event

        if remaining <= 0:
            _LOGGER.info(
                "schedule_max_backup: window expired during downtime — restoring reserve to %s%% now",
                saved_reserve,
            )
            await _max_backup_restore(None)
            return

        cancel = async_call_later(hass, remaining, _max_backup_restore)
        entry_data["max_backup_cancel"] = cancel
        _LOGGER.info(
            "🛡️ Resumed max backup window — restoring reserve to %s%% in %d min",
            saved_reserve,
            int(remaining // 60),
        )

    async def _max_backup_restore(_now) -> None:
        """One-shot callback that restores the saved reserve and clears storage.

        Bound to ``async_call_later`` from both fresh schedules and on-startup
        re-arm — the implementation is identical so we share the closure.
        """
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        target = data.pop("max_backup_saved_reserve", None)
        local_event = bool(data.pop("max_backup_local_event", False))
        data.pop("max_backup_cancel", None)
        data.pop("max_backup_end_ts", None)
        await _persist_max_backup_schedule(None)
        if local_event:
            client = (data.get("powerwall_local") or {}).get("client")
            if client is not None:
                try:
                    await client.cancel_max_backup()
                except Exception as err:
                    _LOGGER.warning(
                        "schedule_max_backup: local event cleanup failed: %s", err
                    )
            _LOGGER.info("🛡️ Local max backup event ended")
            return
        if target is None:
            _LOGGER.warning("schedule_max_backup: no saved reserve to restore")
            return
        _LOGGER.info("🛡️ Max backup window ended — restoring reserve to %s%%", target)
        try:
            await hass.services.async_call(
                DOMAIN,
                "set_backup_reserve",
                {"percent": int(target), "source": "user"},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("schedule_max_backup: restore failed: %s", err)

    async def handle_schedule_max_backup(call: ServiceCall) -> None:
        """Charge to 100% for a window, then restore the previous reserve.

        Saves the current backup_reserve_percent, raises it to 100, and
        schedules a one-shot restoration after ``duration_minutes``. If a
        prior schedule is still running it is cancelled and the original
        saved reserve is preserved (so back-to-back schedules don't lose
        the user's baseline). The schedule is persisted to the per-entry
        Store and resumes after HA restart.
        """
        duration_minutes = call.data.get("duration_minutes")
        if duration_minutes is None:
            _LOGGER.error("schedule_max_backup requires 'duration_minutes'")
            return
        try:
            duration_minutes = int(duration_minutes)
        except (ValueError, TypeError):
            _LOGGER.error("schedule_max_backup: invalid duration: %r", duration_minutes)
            return
        if duration_minutes < 1 or duration_minutes > 1440:
            _LOGGER.error(
                "schedule_max_backup: duration_minutes out of range (1-1440): %d",
                duration_minutes,
            )
            return

        from homeassistant.helpers.event import async_call_later

        entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        local_runtime = entry_data.get("powerwall_local") or {}
        local_client = local_runtime.get("client")
        coord = entry_data.get("tesla_coordinator")
        if coord is None and local_client is None:
            coord = _get_tesla_coordinator_for_service("schedule_max_backup")
        if coord is None and local_client is None:
            _LOGGER.error("schedule_max_backup: no Tesla cloud or local coordinator")
            return
        opt_coord = (
            hass.data.get(DOMAIN, {})
            .get(entry.entry_id, {})
            .get("optimization_coordinator")
        )
        if opt_coord is not None and hasattr(opt_coord, "resolve_restore_target"):
            saved_reserve = await opt_coord.resolve_restore_target()
        else:
            local_snapshot = getattr(local_runtime.get("coordinator"), "data", None)
            saved_reserve = getattr(local_snapshot, "backup_reserve_percent", None)
            if saved_reserve is None:
                site_info = getattr(coord, "_site_info_cache", None) or {}
                saved_reserve = site_info.get("backup_reserve_percent")

        prior_cancel = entry_data.get("max_backup_cancel")
        if prior_cancel is not None:
            try:
                prior_cancel()
            except Exception:
                pass
            saved_reserve = entry_data.get("max_backup_saved_reserve", saved_reserve)
            _LOGGER.info("schedule_max_backup: replacing existing schedule")

        import time as _time

        entry_data["max_backup_saved_reserve"] = saved_reserve
        end_ts = _time.time() + duration_minutes * 60
        entry_data["max_backup_end_ts"] = end_ts

        _LOGGER.info(
            "🛡️ Scheduling max backup for %d min (current reserve: %s%%)",
            duration_minutes,
            saved_reserve,
        )

        local_event = False
        if local_client is not None:
            try:
                local_event = bool(
                    await local_client.schedule_max_backup(duration_minutes * 60)
                )
            except Exception as err:
                _LOGGER.warning(
                    "schedule_max_backup: local event failed; using reserve fallback: %s",
                    err,
                )
        entry_data["max_backup_local_event"] = local_event

        if local_event:
            _LOGGER.info("🛡️ Max backup scheduled via local V1R event")
        else:
            if saved_reserve is None:
                entry_data.pop("max_backup_saved_reserve", None)
                entry_data.pop("max_backup_end_ts", None)
                entry_data.pop("max_backup_local_event", None)
                await _persist_max_backup_schedule(None)
                _LOGGER.error(
                    "schedule_max_backup: native event unavailable or failed and no trustworthy "
                    "reserve restore target is available; refusing reserve fallback"
                )
                return
            await hass.services.async_call(
                DOMAIN,
                "set_backup_reserve",
                {"percent": 100, "source": "user"},
                blocking=True,
            )

        cancel = async_call_later(hass, duration_minutes * 60, _max_backup_restore)
        entry_data["max_backup_cancel"] = cancel
        await _persist_max_backup_schedule(
            {
                "end_ts": end_ts,
                "saved_reserve": saved_reserve,
                "local_event": local_event,
            }
        )

    async def handle_refresh_calibration(call: ServiceCall) -> None:
        """Clear Tesla v1r's calibration_suspected flag.

        Use after a Powerwall calibration completes (or to retry mode toggles
        sooner than the optimiser's natural recovery window). Does not touch
        the Powerwall itself — purely resets the integration's local guard.
        """
        entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        transition = clear_calibration_sources(entry_data)
        dispatch_calibration_state(hass, entry.entry_id)
        entry_data["_mode_stick_failures"] = []
        _LOGGER.info(
            "🔄 Calibration flag cleared (was %s)",
            "set" if transition.was_active else "already clear",
        )

    # Register force discharge, force charge, and restore normal services
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_DISCHARGE,
        handle_force_discharge,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_CHARGE,
        handle_force_charge,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_HOLD_BATTERY_SOC, handle_hold_battery_soc
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESTORE_NORMAL,
        handle_restore_normal,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "set_self_consumption", handle_set_self_consumption
    )
    hass.services.async_register(DOMAIN, "set_autonomous", handle_set_autonomous)

    # Now that handle_restore_normal is defined, schedule expired force mode restore
    if persisted_force_state:
        hass.async_create_task(restore_force_mode_from_persistence())

    # Register Powerwall settings services
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BACKUP_RESERVE,
        handle_set_backup_reserve,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_OPERATION_MODE, handle_set_operation_mode
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_GRID_EXPORT, handle_set_grid_export
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_GRID_CHARGING,
        handle_set_grid_charging,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "set_grid_export_auto", handle_set_grid_export_auto
    )
    hass.services.async_register(DOMAIN, "set_storm_watch", handle_set_storm_watch)
    hass.services.async_register(
        DOMAIN, "set_off_grid_ev_reserve", handle_set_off_grid_ev_reserve
    )
    hass.services.async_register(
        DOMAIN, "set_vpp_enrollment", handle_set_vpp_enrollment
    )
    hass.services.async_register(
        DOMAIN, "schedule_max_backup", handle_schedule_max_backup
    )
    hass.services.async_register(
        DOMAIN, "refresh_calibration", handle_refresh_calibration
    )

    # Resume an in-flight max_backup window if HA restarted mid-schedule.
    async def _resume_max_backup_if_persisted() -> None:
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        store = entry_data.get("store")
        if store is None:
            return
        try:
            data = await store.async_load() or {}
        except Exception:
            return
        schedule = data.get("max_backup_schedule")
        if not isinstance(schedule, dict):
            return
        end_ts = schedule.get("end_ts")
        saved_reserve = schedule.get("saved_reserve")
        local_event = bool(schedule.get("local_event", False))
        if not isinstance(end_ts, (int, float)):
            await _persist_max_backup_schedule(None)
            return
        await _restore_max_backup_window(
            float(end_ts), saved_reserve, local_event=local_event
        )

    hass.async_create_task(_resume_max_backup_if_persisted())

    _LOGGER.info(
        "🔋 Force charge/discharge, restore, and Powerwall settings services registered"
    )

    # ======================================================================
    # CALENDAR HISTORY SERVICE (for mobile app energy summaries)
    # ======================================================================

    async def handle_get_calendar_history(call: ServiceCall) -> dict:
        """Handle get_calendar_history service call - returns energy history data."""
        period = call.data.get("period", "day")

        # Validate period
        valid_periods = ["day", "week", "month", "year"]
        if period not in valid_periods:
            _LOGGER.error(f"Invalid period '{period}'. Must be one of: {valid_periods}")
            return {
                "success": False,
                "error": f"Invalid period. Must be one of: {valid_periods}",
            }

        _LOGGER.info(f"📊 Calendar history requested for period: {period}")

        # Get Tesla coordinator
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        tesla_coordinator = entry_data.get("tesla_coordinator")
        if not tesla_coordinator:
            summary_system, summary_coordinator, summary_entry_id = (
                _find_calendar_energy_summary_source(
                    hass,
                    entry.entry_id,
                )
            )
            if summary_coordinator:
                _LOGGER.info(
                    "Calendar history service using %s daily energy summary",
                    summary_system,
                )
                return await _calendar_result_from_energy_summary(
                    hass,
                    period,
                    None,
                    summary_coordinator,
                    summary_entry_id,
                    source_system=summary_system,
                )

            _LOGGER.error("No calendar history coordinator available")
            return {
                "success": False,
                "error": "No calendar history coordinator available",
            }

        # Fetch calendar history
        history = await tesla_coordinator.async_get_calendar_history(period=period)

        if not history:
            _LOGGER.error("Failed to fetch calendar history")
            return {
                "success": False,
                "error": "Failed to fetch calendar history from Tesla API",
            }

        # Transform time_series to match mobile app format
        # Include both normalized fields AND detailed Tesla breakdown fields
        time_series = []
        for entry_data in history.get("time_series", []):
            time_series.append(
                {
                    "timestamp": entry_data.get("timestamp", ""),
                    # Normalized fields for compatibility
                    "solar_generation": entry_data.get("solar_energy_exported", 0),
                    "battery_discharge": entry_data.get("battery_energy_exported", 0),
                    "battery_charge": entry_data.get("battery_energy_imported", 0),
                    "grid_import": entry_data.get("grid_energy_imported", 0),
                    "grid_export": entry_data.get("grid_energy_exported_from_solar", 0)
                    + entry_data.get("grid_energy_exported_from_battery", 0),
                    "home_consumption": entry_data.get(
                        "consumer_energy_imported_from_grid", 0
                    )
                    + entry_data.get("consumer_energy_imported_from_solar", 0)
                    + entry_data.get("consumer_energy_imported_from_battery", 0),
                    # Detailed breakdown fields from Tesla API (for detail screens)
                    "solar_energy_exported": entry_data.get("solar_energy_exported", 0),
                    "battery_energy_exported": entry_data.get(
                        "battery_energy_exported", 0
                    ),
                    "battery_energy_imported_from_grid": entry_data.get(
                        "battery_energy_imported_from_grid", 0
                    ),
                    "battery_energy_imported_from_solar": entry_data.get(
                        "battery_energy_imported_from_solar", 0
                    ),
                    "consumer_energy_imported_from_grid": entry_data.get(
                        "consumer_energy_imported_from_grid", 0
                    ),
                    "consumer_energy_imported_from_solar": entry_data.get(
                        "consumer_energy_imported_from_solar", 0
                    ),
                    "consumer_energy_imported_from_battery": entry_data.get(
                        "consumer_energy_imported_from_battery", 0
                    ),
                    "grid_energy_exported_from_solar": entry_data.get(
                        "grid_energy_exported_from_solar", 0
                    ),
                    "grid_energy_exported_from_battery": entry_data.get(
                        "grid_energy_exported_from_battery", 0
                    ),
                }
            )

        result = {
            "success": True,
            "period": period,
            "time_series": time_series,
            "serial_number": history.get("serial_number"),
            "installation_date": history.get("installation_date"),
        }

        _LOGGER.info(
            f"✅ Calendar history returned: {len(time_series)} records for period '{period}'"
        )
        return result

    # Register with response support (HA 2024.1+)
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_CALENDAR_HISTORY,
        handle_get_calendar_history,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.info("📊 Calendar history service registered")

    # Preload protobuf C extension off the event loop before the import chain runs.
    await hass.async_add_executor_job(_preload_powerwall_local_modules)

    # Register Powerwall local pairing + off-grid HTTP endpoints
    from .powerwall_local.services import (
        register_services as _register_powerwall_local_services,
    )
    from .powerwall_local.views import register_views as _register_powerwall_local_views

    _register_powerwall_local_views(hass)
    _register_powerwall_local_services(hass)
    _LOGGER.info(
        "🔌 Powerwall local control endpoints + services registered "
        "(pair/status/cancel/unpair/off_grid/local_status)"
    )

    # Warm up the local coordinator if this entry is already paired.
    from .powerwall_local.views import ensure_coordinator as _ensure_pwlocal_coordinator

    try:
        await _ensure_pwlocal_coordinator(hass, entry)
    except Exception as _err:
        _LOGGER.debug("Powerwall local coordinator warmup skipped: %s", _err)

    # ======================================================================
    # FETCH TESLA TARIFF ON STARTUP (for non-Amber users like Globird)
    # ======================================================================
    # For users who rely on Tesla's built-in tariff schedule (set in the Tesla app),
    # we need to fetch the tariff on startup to populate tariff_schedule with TOU periods.
    # This enables the EV charging planner to correctly identify cheap/free periods.
    electricity_provider = entry.options.get(
        CONF_ELECTRICITY_PROVIDER, entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
    )
    if should_fetch_tesla_tariff_on_startup(
        electricity_provider,
        bool(entry.data.get(CONF_TESLA_ENERGY_SITE_ID)),
        token_getter,
    ):
        _LOGGER.info(
            f"📊 Fetching Tesla tariff schedule for {electricity_provider} user..."
        )
        try:
            # If force charge/discharge is/was active, Tesla API may still have
            # the fake tariff. Use the saved real tariff instead.
            tariff_data = None
            force_state = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            fd_state = force_state.get("force_discharge_state", {})
            fc_state = force_state.get("force_charge_state", {})
            saved_tariff = None
            if fd_state.get("active") and fd_state.get("saved_tariff"):
                saved_tariff = _select_restorable_tesla_tariff(fd_state["saved_tariff"])
                if saved_tariff:
                    _LOGGER.info(
                        "Force discharge active - using saved tariff for schedule instead of Tesla API"
                    )
                else:
                    _LOGGER.warning(
                        "Force discharge saved tariff is a Tesla v1r force tariff; not using it as tariff schedule"
                    )
            elif fc_state.get("active") and fc_state.get("saved_tariff"):
                saved_tariff = _select_restorable_tesla_tariff(fc_state["saved_tariff"])
                if saved_tariff:
                    _LOGGER.info(
                        "Force charge active - using saved tariff for schedule instead of Tesla API"
                    )
                else:
                    _LOGGER.warning(
                        "Force charge saved tariff is a Tesla v1r force tariff; not using it as tariff schedule"
                    )
            elif persisted_force_state and persisted_force_state.get("saved_tariff"):
                # Force mode expired during restart — restore_normal is uploading
                # the real tariff but may not have completed yet. Use saved tariff
                # so the optimizer gets correct prices immediately.
                saved_tariff = _select_restorable_tesla_tariff(
                    persisted_force_state["saved_tariff"]
                )
                if saved_tariff:
                    _LOGGER.info(
                        "Force mode recently expired - using saved tariff to avoid stale force-charge tariff"
                    )
                else:
                    _LOGGER.warning(
                        "Persisted force-mode saved tariff is a Tesla v1r force tariff; not using it as tariff schedule"
                    )

            if saved_tariff:
                tariff_data = convert_custom_tariff_to_schedule(
                    saved_tariff,
                    currency=currency_for_entry(entry, hass),
                )
                if tariff_data:
                    hass.data[DOMAIN][entry.entry_id]["tariff_schedule"] = tariff_data
            else:
                tariff_data = await fetch_tesla_tariff_schedule(hass, entry)
                if not tariff_data:
                    cached_tariff = _select_restorable_tesla_tariff(
                        force_state.get("last_restorable_tesla_tariff")
                    )
                    if cached_tariff:
                        _LOGGER.info(
                            "Tesla tariff fetch unavailable or force-tainted; using cached Tesla tariff baseline"
                        )
                        tariff_data = convert_custom_tariff_to_schedule(
                            cached_tariff,
                            currency=currency_for_entry(entry, hass),
                        )
                        if tariff_data:
                            hass.data[DOMAIN][entry.entry_id]["tariff_schedule"] = (
                                tariff_data
                            )
            if tariff_data:
                tou_count = len(tariff_data.get("tou_periods", {}))
                _LOGGER.info(
                    f"✅ Tesla tariff initialized: {tariff_data.get('plan_name', 'Unknown')} "
                    f"with {tou_count} TOU periods"
                )
                # Log the rates for each period
                buy_rates = tariff_data.get("buy_rates", {})
                for period_name, rate in buy_rates.items():
                    rate_cents = rate * 100
                    _LOGGER.info(f"  💰 {period_name}: {rate_cents:.1f}c/kWh")
            else:
                _LOGGER.warning(
                    "⚠️ Could not fetch Tesla tariff on startup. "
                    "EV charging planner may not have TOU schedule available. "
                    "Ensure your tariff is configured in the Tesla app."
                )
        except Exception as e:
            _LOGGER.error(f"Error fetching Tesla tariff on startup: {e}")

    # ======================================================================
    # SYNC BATTERY HEALTH SERVICE (from mobile app TEDAPI scans)
    # ======================================================================

    async def handle_sync_battery_health(call: ServiceCall) -> dict:
        """Handle sync_battery_health service call - receives battery health from mobile app."""
        original_capacity_wh = call.data.get("original_capacity_wh")
        current_capacity_wh = call.data.get("current_capacity_wh")
        degradation_percent = call.data.get("degradation_percent")
        battery_count = call.data.get("battery_count", 1)
        scanned_at = call.data.get("scanned_at", dt_util.now().isoformat())
        individual_batteries = call.data.get(
            "individual_batteries"
        )  # Optional per-battery data

        # Validate required fields
        if (
            original_capacity_wh is None
            or current_capacity_wh is None
            or degradation_percent is None
        ):
            _LOGGER.error("Missing required battery health fields")
            return {
                "success": False,
                "error": "Missing required fields: original_capacity_wh, current_capacity_wh, degradation_percent",
            }

        # Calculate health percentage (can be > 100% if batteries have more capacity than spec)
        health_percent = (
            round((current_capacity_wh / original_capacity_wh) * 100, 1)
            if original_capacity_wh > 0
            else 0
        )

        _LOGGER.info(
            f"🔋 Battery health received: {health_percent}% health ({current_capacity_wh}Wh / {original_capacity_wh}Wh, {battery_count} units)"
        )

        # Build battery health data
        battery_health_data = {
            "original_capacity_wh": original_capacity_wh,
            "current_capacity_wh": current_capacity_wh,
            "degradation_percent": degradation_percent,
            "battery_count": battery_count,
            "scanned_at": scanned_at,
        }

        # Include individual battery data if provided
        if individual_batteries:
            battery_health_data["individual_batteries"] = individual_batteries
            _LOGGER.info(f"  → Individual batteries: {len(individual_batteries)} units")

        # Store in hass.data for sensor to read on startup
        hass.data[DOMAIN][entry.entry_id]["battery_health"] = battery_health_data

        # Persist to storage
        store = hass.data[DOMAIN][entry.entry_id].get("store")
        if store:
            stored_data = await store.async_load() or {}
            stored_data["battery_health"] = battery_health_data
            await store.async_save(stored_data)
            _LOGGER.debug("Battery health persisted to storage")

        # Notify sensor via dispatcher
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_battery_health_update_{entry.entry_id}",
            battery_health_data,
        )

        return {
            "success": True,
            "message": f"Battery health synced: {health_percent}% health",
            "data": battery_health_data,
        }

    # Register with response support
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC_BATTERY_HEALTH,
        handle_sync_battery_health,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.info("🔋 Battery health sync service registered")

    # Reload integration when options change (e.g. optimizer toggled in config flow)
    # A token refresh can persist during initial setup, before this listener exists.
    # Discard that one-shot suppression so it cannot swallow the next real update.
    hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).pop("_skip_reload", None)
    entry.async_on_unload(entry.add_update_listener(_async_options_update_listener))

    _LOGGER.info("=" * 60)
    _LOGGER.info("Tesla v1r integration setup complete!")
    _LOGGER.info("Domain '%s' registered successfully", DOMAIN)
    _LOGGER.info("Mobile app should now detect the integration")
    _LOGGER.info("=" * 60)
    return True


async def _async_options_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload integration when options change (unless API-driven)."""
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get(entry.entry_id, {})
    if entry_data.get("_skip_reload"):
        entry_data.pop("_skip_reload", None)
        _LOGGER.info("Config entry options updated via API — skipping reload")
        return
    _LOGGER.info("Config entry options updated — reloading Tesla v1r integration")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Tesla v1r integration")

    # Tear down TOU sync hooks (AEMO dispatch subscriber + dispatch-trigger
    # coordinator + cron fallback + optional Octopus cron)
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    # Fence detached explanation work before unload/reload can replace the
    # entry. It has no optimizer or hardware side effects, but must not cache
    # a result under a replacement provider configuration.
    if isinstance(entry_data, dict):
        if auto_unsub := entry_data.pop("_optimization_ai_summary_auto_unsub", None):
            auto_unsub()
        if ai_service := entry_data.get("_optimization_ai_summary_service"):
            await ai_service.async_shutdown()

    # Let every in-flight Tesla reserve pulse finish its verified exact restore
    # before a replacement setup receives a new lock/generation namespace.
    if isinstance(entry_data, dict):
        entry_data["tesla_reserve_pulse_stopping"] = True
        current_task = asyncio.current_task()
        reserve_write_tasks = tuple(
            task
            for task in entry_data.get("tesla_reserve_write_tasks", ())
            if task is not current_task and not task.done()
        )
        if reserve_write_tasks:
            await asyncio.gather(*reserve_write_tasks, return_exceptions=True)
        entry_data["tesla_reserve_write_tasks"] = set()

    # Fence callbacks from this setup before any await below.  A dispatcher
    # callback can already be queued on the event loop (or be waiting on the
    # settled-price delay); cancelling and joining the tracked tasks here
    # prevents them from touching removed data or a replacement generation.
    if isinstance(entry_data, dict):
        entry_data["aemo_dispatch_stopping"] = True
        if started_unsub := entry_data.get("aemo_dispatch_started_unsub"):
            started_unsub()
            entry_data["aemo_dispatch_started_unsub"] = None
        dispatch_tasks = tuple(entry_data.get("aemo_dispatch_tasks", ()))
        for task in dispatch_tasks:
            if not task.done():
                task.cancel()
        if dispatch_tasks:
            # Completion callbacks consume and report task exceptions.  Keep
            # return_exceptions here so a pre-existing failed task cannot
            # abort the rest of unload or duplicate that report.
            await asyncio.gather(
                *dispatch_tasks,
                return_exceptions=True,
            )
        entry_data["aemo_dispatch_tasks"] = set()

    if tesla_stream_coordinator := entry_data.get("tesla_coordinator"):
        try:
            await tesla_stream_coordinator.async_shutdown()
        except Exception as err:
            _LOGGER.debug("Tesla Energy Site stream shutdown error: %s", err)
    if covau_runtime := entry_data.get("covau_quota_runtime"):
        try:
            await covau_runtime.async_stop()
        except Exception as err:
            _LOGGER.debug("CovaU quota runtime shutdown error: %s", err)
        entry_data["covau_quota_runtime"] = None
    if network_listener_unsub := entry_data.get("network_envelope_listener_unsub"):
        network_listener_unsub()
        entry_data["network_envelope_listener_unsub"] = None
    if network_retry_task := entry_data.get("network_envelope_reoptimization_task"):
        if not network_retry_task.done():
            network_retry_task.cancel()
        await asyncio.gather(network_retry_task, return_exceptions=True)
        entry_data["network_envelope_reoptimization_task"] = None
    if network_envelope_manager := entry_data.get("network_envelope_manager"):
        try:
            await network_envelope_manager.async_stop()
        except Exception as err:
            _LOGGER.debug("Network export envelope shutdown error: %s", err)
        entry_data["network_envelope_manager"] = None
        entry_data["network_export_guard"] = None
        _LOGGER.debug("Stopped network export envelope manager")
    if aemo_unsub := entry_data.get("aemo_dispatch_unsub"):
        aemo_unsub()
        _LOGGER.debug("Unsubscribed AEMO new-dispatch listener")
    if trigger_listener_unsub := entry_data.get("aemo_dispatch_trigger_listener_unsub"):
        # Removing the no-op listener tells the coordinator to stop scheduling
        # the next refresh after the in-flight one (if any) finishes.
        trigger_listener_unsub()
        entry_data["aemo_dispatch_trigger_listener_unsub"] = None
    if trigger_coord := entry_data.get("aemo_dispatch_trigger_coordinator"):
        # Belt-and-braces: also clear update_interval so the coordinator can't
        # reschedule, and call async_shutdown if HA exposes it.
        trigger_coord.update_interval = None
        if hasattr(trigger_coord, "async_shutdown"):
            try:
                await trigger_coord.async_shutdown()
            except Exception as e:
                _LOGGER.debug("AEMO dispatch-trigger coordinator shutdown error: %s", e)
        entry_data["aemo_dispatch_trigger_coordinator"] = None
        _LOGGER.debug("Stopped AEMO dispatch-trigger coordinator")
    if fallback_cancel := entry_data.get("dispatch_fallback_cron_cancel"):
        fallback_cancel()
        _LOGGER.debug("Cancelled NEM TOU sync periodic fallback")
    if octopus_cancel := entry_data.get("octopus_sync_cancel"):
        octopus_cancel()
        _LOGGER.debug("Cancelled Octopus :00/:30 sync cron")

    # Cancel Tesla v1r HACS auto-update scheduler if it exists
    if auto_update_cancel := entry_data.get("auto_update_cancel"):
        auto_update_cancel()
        _LOGGER.debug("Cancelled Tesla v1r auto-update scheduler")

    # Cancel the curtailment timer if it exists
    if curtailment_cancel := entry_data.get("curtailment_cancel"):
        curtailment_cancel()
        _LOGGER.debug("Cancelled curtailment timer")

    # Cancel the load-following timer if it exists
    if load_following_cancel := entry_data.get("load_following_cancel"):
        load_following_cancel()
        _LOGGER.debug("Cancelled load-following timer")

    # The cached AC-inverter controller can hold an Enphase HTTP session while
    # load following is active.  Closing it before the entry data is removed
    # prevents a reload from leaving that session attached to the Envoy.
    if solaredge_controller := entry_data.pop("solaredge_controller", None):
        try:
            await solaredge_controller.disconnect()
        except Exception as err:
            _LOGGER.debug("Could not close SolarEdge curtailment connection: %s", err)

    if inverter_controller := entry_data.pop("inverter_controller", None):
        try:
            await inverter_controller.disconnect()
        except Exception as err:
            _LOGGER.debug("AC inverter controller shutdown error: %s", err)

    # Cancel Flow Power v2 tariff timers
    if fp_cancel := entry_data.get("fp_tariff_cancel"):
        fp_cancel()
        _LOGGER.debug("Cancelled Flow Power tariff refresh timer")
    if fp_mid_cancel := entry_data.get("fp_midnight_cancel"):
        fp_mid_cancel()
        _LOGGER.debug("Cancelled Flow Power midnight tariff recalc timer")

    if globird_coordinator := entry_data.get("globird_coordinator"):
        try:
            await globird_coordinator.async_shutdown()
        except Exception as e:
            _LOGGER.debug("GloBird coordinator shutdown error: %s", e)
        entry_data["globird_coordinator"] = None
        _LOGGER.debug("Closed GloBird portal coordinator")

    # Cancel Zaptec state polling and close client
    if unsub_zaptec := entry_data.get("unsub_zaptec_poll"):
        unsub_zaptec()
        _LOGGER.debug("Cancelled Zaptec state polling")
    if zaptec_client := entry_data.get("zaptec_client"):
        await zaptec_client.close()
        _LOGGER.debug("Closed Zaptec Cloud client")

    # Stop the Tesla v1r Cloud flow reporter if it was started
    if cloud_flow_reporter := entry_data.get("cloud_flow_reporter"):
        await cloud_flow_reporter.stop()
        entry_data["cloud_flow_reporter"] = None
        _LOGGER.debug("Stopped Tesla v1r cloud flow reporter")

    # Cancel OCPP session polling
    if unsub_ocpp := entry_data.get("unsub_ocpp_poll"):
        unsub_ocpp()
        _LOGGER.debug("Cancelled OCPP session polling")

    if pack_sensor_unsub := entry_data.get("powerwall_pack_sensor_unsub"):
        pack_sensor_unsub()
        entry_data["powerwall_pack_sensor_unsub"] = None
        _LOGGER.debug("Unsubscribed Powerwall pack sensor listener")

    if solar_string_sensor_unsub := entry_data.get(
        "powerwall_solar_string_sensor_unsub"
    ):
        solar_string_sensor_unsub()
        entry_data["powerwall_solar_string_sensor_unsub"] = None
        _LOGGER.debug("Unsubscribed Powerwall solar string sensor listener")

    if bms_poll_cancel := entry_data.get("powerwall_bms_health_poll_cancel"):
        bms_poll_cancel()
        entry_data["powerwall_bms_health_poll_cancel"] = None
        _LOGGER.debug("Cancelled Powerwall BMS health polling")

    if solar_strings_poll_cancel := entry_data.get(
        "powerwall_solar_strings_poll_cancel"
    ):
        solar_strings_poll_cancel()
        entry_data["powerwall_solar_strings_poll_cancel"] = None
        _LOGGER.debug("Cancelled Powerwall solar string polling")

    # Cancel the AEMO spike timer if it exists
    if aemo_spike_cancel := entry_data.get("aemo_spike_cancel"):
        aemo_spike_cancel()
        _LOGGER.debug("Cancelled AEMO spike timer")

    # Cancel the generic AEMO spike timer if it exists
    if generic_aemo_spike_cancel := entry_data.get("generic_aemo_spike_cancel"):
        generic_aemo_spike_cancel()
        _LOGGER.debug("Cancelled generic AEMO spike timer")

    # Cancel the VPP AEMO spike timer / first-run task if they exist
    if vpp_aemo_cancel := entry_data.get("vpp_aemo_cancel"):
        vpp_aemo_cancel()
        _LOGGER.debug("Cancelled VPP AEMO spike timer")
    if vpp_aemo_initial_task := entry_data.get("vpp_aemo_initial_task"):
        vpp_aemo_initial_task.cancel()
        _LOGGER.debug("Cancelled initial VPP AEMO spike check")

    # Cancel the saving session timer if it exists
    if saving_session_cancel := entry_data.get("saving_session_cancel"):
        saving_session_cancel()
        _LOGGER.debug("Cancelled saving session timer")

    # Cancel the demand period grid charging timer if it exists
    if demand_charging_cancel := entry_data.get("demand_charging_cancel"):
        demand_charging_cancel()
        _LOGGER.debug("Cancelled demand period grid charging timer")

    if demand_charge_coordinator := entry_data.get("demand_charge_coordinator"):
        try:
            await demand_charge_coordinator.async_save()
        except Exception as err:
            _LOGGER.debug("Could not save Demand Charge peak on unload: %s", err)

    # Cancel the Tesla calibration-recovery check timer if it exists
    if calibration_check_unsub := entry_data.get("_calibration_check_unsub"):
        calibration_check_unsub()
        entry_data["_calibration_check_unsub"] = None
        _LOGGER.debug("Cancelled Tesla calibration-recovery check timer")

    # Re-enable grid charging if it was disabled for demand period
    # (e.g. user disabled demand charges or enabled demand_allow_grid_charging mid-window)
    if entry_data.get("grid_charging_disabled_for_demand", False):
        ts_coordinator = entry_data.get("tesla_coordinator")
        if ts_coordinator:
            try:
                success = await ts_coordinator.set_grid_charging_enabled(True)
                if success:
                    _LOGGER.info(
                        "Re-enabled grid charging on unload (was disabled for demand period)"
                    )
                else:
                    _LOGGER.warning("Failed to re-enable grid charging on unload")
            except Exception as err:
                _LOGGER.warning("Error re-enabling grid charging on unload: %s", err)

    # Cancel the automation evaluation timer if it exists
    if automation_cancel := entry_data.get("automation_cancel"):
        automation_cancel()
        _LOGGER.debug("Cancelled automation evaluation timer")

    # Cancel any pending EV boost stop timer. Ownership diagnostics are
    # persisted below so setup can recover without restoring a stale lease.
    if ev_boost_cancel := entry_data.get("ev_boost_cancel"):
        ev_boost_cancel()
        entry_data["ev_boost_cancel"] = None
        _LOGGER.debug("Cancelled EV boost timer")

    if observed_tesla_cancel := entry_data.get("observed_tesla_session_cancel"):
        observed_tesla_cancel()
        _LOGGER.debug("Cancelled observed Tesla session polling")

    # Flush active EV charging sessions so they're not lost
    if session_manager := entry_data.get("session_manager"):
        for vehicle_id in list(session_manager.active_sessions.keys()):
            try:
                await session_manager.end_session(vehicle_id, "ha_restart")
                _LOGGER.info(f"Flushed EV charging session for {vehicle_id} on unload")
            except Exception as e:
                _LOGGER.debug(f"Could not flush session for {vehicle_id}: {e}")

    # Persist EV ownership diagnostics before unload. Active leases are only a
    # snapshot; setup restores the last-command context and clears stale owners.
    try:
        from .automations.ev_ownership import persist_ev_runtime_state

        if automation_store := entry_data.get("automation_store"):
            await persist_ev_runtime_state(hass, entry, automation_store)
    except Exception as err:
        _LOGGER.debug("EV runtime persist on unload failed: %s", err)

    # Invalidate this entry's dynamic EV callbacks after persistence and
    # before the hass.data mirror is removed.  The cleanup is command-neutral:
    # it cancels local timers only and never releases another/newer owner.
    try:
        from .automations.actions import cleanup_dynamic_ev_entry

        cleanup_dynamic_ev_entry(hass, entry.entry_id)
    except Exception as err:
        _LOGGER.debug("Dynamic EV runtime cleanup on unload failed: %s", err)

    # Save Flow Power TWAP history on unload
    if twap_tracker := entry_data.get("flow_power_twap_tracker"):
        try:
            await twap_tracker.async_save()
            _LOGGER.debug("Saved Flow Power TWAP history")
        except Exception as e:
            _LOGGER.debug(f"Error saving TWAP history: {e}")

    if fp_account_cancel := entry_data.get("fp_account_cancel"):
        fp_account_cancel()

    # Stop Amber usage coordinator if it exists
    if usage_coord := entry_data.get("amber_usage_coordinator"):
        try:
            await usage_coord.async_stop()
            _LOGGER.info("Amber usage coordinator stopped")
        except Exception as e:
            _LOGGER.debug(f"Error stopping Amber usage coordinator: {e}")

    # Stop optimization coordinator if it exists
    if opt_coordinator := entry_data.get("optimization_coordinator"):
        try:
            await opt_coordinator.disable()
            _LOGGER.info("Optimization coordinator stopped")
        except Exception as e:
            _LOGGER.error(f"Error stopping optimization coordinator: {e}")

    # Stop WebSocket client if it exists
    if ws_client := entry_data.get("ws_client"):
        try:
            await ws_client.stop()
            _LOGGER.info("🔌 WebSocket client stopped")
        except Exception as e:
            _LOGGER.error(f"Error stopping WebSocket client: {e}")

    # Stop Tesla signaling WebSocket if it exists
    pw_local = entry_data.get("powerwall_local", {})
    if signaling := pw_local.get("signaling"):
        try:
            await signaling.stop()
            _LOGGER.info("Tesla signaling WebSocket stopped")
        except Exception as e:
            _LOGGER.error(f"Error stopping Tesla signaling WebSocket: {e}")

    # Stop the local Powerwall (TEDAPI) poller if it exists. A keep-alive
    # no-op listener is anchored at construction time (see
    # PowerwallLocalCoordinator.__init__ / self._keepalive_unsub) so the
    # coordinator's periodic schedule stays armed even with zero entity
    # listeners — just popping entry_data does not stop the 2s poll timer,
    # leaking one TEDAPI poller per reload. Mirrors the teardown pattern in
    # powerwall_local/views.py ensure_coordinator().
    if pw_local_coordinator := pw_local.get("coordinator"):
        try:
            pw_local_coordinator.update_interval = None
        except Exception as e:
            _LOGGER.debug("Error clearing powerwall_local update_interval: %s", e)
        keepalive_unsub = getattr(pw_local_coordinator, "_keepalive_unsub", None)
        if callable(keepalive_unsub):
            try:
                keepalive_unsub()
            except Exception as e:
                _LOGGER.debug(
                    "Error unsubscribing powerwall_local keepalive listener: %s", e
                )
        if hasattr(pw_local_coordinator, "async_shutdown"):
            try:
                await pw_local_coordinator.async_shutdown()
            except Exception as e:
                _LOGGER.debug("Error shutting down powerwall_local coordinator: %s", e)
        pw_local["coordinator"] = None
        _LOGGER.debug("Stopped Powerwall local (TEDAPI) coordinator")

    # Flush energy accumulator so the next restore has the latest values
    # (prevents total_increasing sensors from going backwards after reload)
    for coord_key in (
        "tesla_coordinator",
        "sigenergy_coordinator",
        "sungrow_coordinator",
        "foxess_coordinator",
        "goodwe_coordinator",
        "alphaess_coordinator",
        "solax_coordinator",
        "saj_h2_coordinator",
        "fronius_reserva_coordinator",
        "neovolt_coordinator",
        "solaredge_coordinator",
        "anker_solix_coordinator",
        "custom_energy_coordinator",
    ):
        coord = entry_data.get(coord_key)
        if coord and hasattr(coord, "_energy_acc"):
            try:
                await coord._energy_acc.async_flush()
            except Exception as e:
                _LOGGER.debug(
                    "Failed to flush energy accumulator for %s: %s", coord_key, e
                )
        if coord and hasattr(coord, "async_flush_lifetime_totals"):
            try:
                await coord.async_flush_lifetime_totals()
            except Exception as e:
                _LOGGER.debug(
                    "Failed to flush lifetime totals for %s: %s", coord_key, e
                )

    # Shut down every brand coordinator's controller connection on unload.
    # Each of these holds an AsyncModbusTcpClient (or equivalent) that is
    # never closed unless we call async_shutdown() explicitly — without this,
    # a reload orphans one connection per configured brand until the
    # inverter's connection pool exhausts ("Failed to connect"). AlphaESS's
    # async_shutdown() also releases forced dispatch (0722H) first since it
    # has no auto-revert; guard each brand independently so one failure
    # doesn't block the others from shutting down.
    for coord_key in (
        "sigenergy_coordinator",
        "sungrow_coordinator",
        "foxess_coordinator",
        "goodwe_coordinator",
        "alphaess_coordinator",
        "esy_sunhome_coordinator",
        "solax_coordinator",
        "saj_h2_coordinator",
        "fronius_reserva_coordinator",
        "neovolt_coordinator",
        "solaredge_coordinator",
        "anker_solix_coordinator",
    ):
        coord = entry_data.get(coord_key)
        if coord and hasattr(coord, "async_shutdown"):
            try:
                await coord.async_shutdown()
                entry_data[coord_key] = None
                _LOGGER.debug("Stopped %s", coord_key)
            except Exception as e:
                _LOGGER.debug("%s shutdown error: %s", coord_key, e)

    # OB-7: cancel any pending force-mode / Hold SoC expiry + hardware-
    # refresh timers before tearing down the entry. These callbacks close
    # over this setup's hass/entry/state-dict closures; a reload rebuilds
    # fresh force_charge_state / force_discharge_state / hold_soc_state
    # dicts (and a fresh persist/restore pass — see OB-5), so an orphaned
    # pre-reload timer left running here would fire later against the *new*
    # setup's state, coordinators, or a config entry that's already gone.
    for _state_key in (
        "force_charge_state",
        "force_discharge_state",
        "hold_soc_state",
        "self_consumption_state",
    ):
        _state = entry_data.get(_state_key)
        if not _state:
            continue
        for _timer_key in ("cancel_expiry_timer", "cancel_hardware_refresh_timer"):
            _cancel = _state.get(_timer_key)
            if callable(_cancel):
                try:
                    _cancel()
                except Exception as e:
                    _LOGGER.debug(
                        "Error cancelling %s.%s on unload: %s",
                        _state_key,
                        _timer_key,
                        e,
                    )
                _state[_timer_key] = None
    _LOGGER.debug("Cancelled force/hold expiry timers on unload")

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Remove services if this is the last entry
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_TOU)
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_NOW)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
