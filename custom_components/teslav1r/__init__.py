"""The Tesla v1r integration."""

import asyncio
import copy
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .settings_metadata import optimizer_settings_groups
from .tesla_calibration import (
    CALIBRATION_SOURCE_LOCAL_ALERT as CALIBRATION_SOURCE_LOCAL_ALERT,
)
from .tesla_calibration import (
    clear_calibration_sources,
    dispatch_calibration_state,
)

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
from homeassistant.helpers.storage import Store

from .battery_backend.profiles import resolve_connection_profile
from .const import (
    BATTERY_SYSTEM_CUSTOM,
    CONF_BATTERY_CURTAILMENT_ENABLED,
    # Battery system selection
    CONF_BATTERY_SYSTEM,
    CONF_DEMAND_ALLOW_GRID_CHARGING,
    CONF_FLEET_API_BASE_URL,
    CONF_HARDWARE_BACKUP_RESERVE,
    CONF_OPTIMIZATION_BACKUP_RESERVE,
    CONF_OPTIMIZATION_MANUAL_RESERVE,
    CONF_OPTIMIZATION_MAX_CHARGE_W,
    CONF_OPTIMIZATION_MAX_DISCHARGE_W,
    CONF_OPTIMIZATION_MAX_GRID_EXPORT_W,
    CONF_POWERWALL_LOCAL_PAIRED,
    CONF_TESLA_API_PROVIDER,
    CONF_TESLA_ENERGY_SITE_ID,
    DEFAULT_OPTIMIZATION_BACKUP_RESERVE,
    DOMAIN,
    SERVICE_SET_GRID_EXPORT,
    SERVICE_SET_OPERATION_MODE,
    SERVICE_SYNC_BATTERY_HEALTH,
    SERVICE_SYNC_NOW,
    SERVICE_SYNC_TOU,
    # Tesla integrations for device discovery
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
    TESLA_PROVIDER_FLEET_API,
    get_tesla_api_base_url,
)
from .coordinator import (
    TeslaEnergyCoordinator,
)
from .sensitive_logging import obfuscate_log_arg, obfuscate_vin_tokens
from .tesla_grid_control import (
    tesla_grid_charging_enabled_from_site_info,
    tesla_site_info_has_structure,
)


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

    Probably not needed.  Keeping just in case. Should fall through to Tesla
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


def get_tesla_api_token(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[str | None, str]:
    """
    Get the current Tesla API token and provider for this entry.

    Honors the user's configured CONF_TESLA_API_PROVIDER:
    - fleet_api: returns a fresh access token from the tesla_fleet HA integration

    The tesla_fleet integration handles token refresh internally and updates its
    config entry data. We always fetch the latest token.

    Returns:
        tuple: (token, provider) where provider is  'fleet_api'
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


_API_ERROR_COOLDOWN_SECONDS = 5 * 60  # 5 minutes
_last_api_error_notification: dict[str, float] = {}


async def _notify_api_error(hass, title: str, message: str) -> None:
    """Send push notification for API errors with cooldown to prevent spam.

    Tesla's Fleet API can return 504 Gateway Timeout in bursts (e.g. at the
    top of each hour). Without cooldown, the user gets multiple identical
    notifications within seconds. This deduplicates by title — same error
    title is suppressed for 5 minutes after the first notification.

    TODO: Replace with Home Assistant's built-in notification service.
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

        # try:
        #    from .automations.actions import _send_expo_push
        #    await _send_expo_push(hass, f"⚠️ {title}", message)
        _last_api_error_notification[title] = now
    # except Exception:
    #    pass  # Don't let notification failures cascade


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

    # Register the parent (hub) device.
    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or "Tesla v1r",
        manufacturer="Tesla v1r",
        model="Hub",
    )

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

    # Restore manual export override
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
        async_dispatcher_send(hass, f"tesla_v1r_curtailment_updated_{entry.entry_id}")

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
            from .automations.live_status import coordinator_data_to_live_status

            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            for coord_key in ("tesla_coordinator",):
                coordinator = entry_data.get(coord_key)
                data = getattr(coordinator, "data", None)
                if not data:
                    continue
                live_status = coordinator_data_to_live_status(data)
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

    def _control_call_source(call: ServiceCall) -> str:
        """Return the normalized source for a battery control service call."""
        source = str(call.data.get("source", "")).lower()
        if source:
            return source
        if getattr(call.context, "user_id", None):
            return "user"
        return "unknown"

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
        return round(parsed)

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

    # ======================================================================
    # POWERWALL SETTINGS SERVICES (for mobile app Controls)
    # ======================================================================

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
                if mode == "self_consumption" and entry.entry_id in hass.data[DOMAIN]:
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

        _LOGGER.info(f"📤 Setting grid export rule to {rule}")

        try:
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

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

    def _get_tesla_coordinator_for_service(service_name: str):
        """Return the Tesla energy coordinator for this entry, or None with a log."""
        entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        coord = entry_data.get("tesla_coordinator")
        if coord is None:
            _LOGGER.error("%s: Tesla energy coordinator not available", service_name)
        return coord

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

    # Register Powerwall settings services
    hass.services.async_register(
        DOMAIN, SERVICE_SET_OPERATION_MODE, handle_set_operation_mode
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_GRID_EXPORT, handle_set_grid_export
    )
    hass.services.async_register(
        DOMAIN, "set_grid_export_auto", handle_set_grid_export_auto
    )
    hass.services.async_register(
        DOMAIN, "refresh_calibration", handle_refresh_calibration
    )

    _LOGGER.info(
        "🔋 Force charge/discharge, restore, and Powerwall settings services registered"
    )

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
