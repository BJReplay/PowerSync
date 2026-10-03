"""Data update coordinators for PowerSync with improved error handling."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    FLEET_API_BASE_URL,
    TESLA_PROVIDER_FLEET_API,
    TESLA_SITE_INFO_CACHE_TTL_SECONDS,
    TESLA_V1R_USER_AGENT,
    UPDATE_INTERVAL_ENERGY,
)
from .sensitive_logging import obfuscate_log_arg, obfuscate_vin_tokens
from .tesla_grid_control import async_set_tesla_grid_charging_confirmed

ENERGY_ACC_STORE_VERSION = 1
ENERGY_ACC_SAVE_DELAY = 300  # Flush at most every 5 minutes
ENERGY_ACC_PRICE_COVERAGE_SCHEMA = 5
TESLA_OUTAGE_NOTIFY_FAILURES = 5
TESLA_OUTAGE_NOTIFY_MIN_SECONDS = 300
LIFETIME_TOTALS_STORE_VERSION = 1
LIFETIME_TOTAL_KEYS = (
    "lifetime_solar_kwh",
    "lifetime_grid_import_kwh",
    "lifetime_grid_export_kwh",
    "lifetime_battery_charged_kwh",
    "lifetime_battery_discharged_kwh",
    "lifetime_home_kwh",
)

_TERMINAL_GRID_STATUS_VALUES = {
    "active": "Active",
    "systemgridconnected": "SystemGridConnected",
    "inactive": "Inactive",
    "islanded": "Islanded",
    "off-grid": "Off-Grid",
    "systemislandedactive": "SystemIslandedActive",
}

def _terminal_grid_status(value: Any) -> str | None:
    """Return a canonical terminal grid status, or None while state is unknown."""
    if not isinstance(value, str):
        return None
    return _TERMINAL_GRID_STATUS_VALUES.get(value.strip().lower())

def _grid_status_is_off_grid(value: Any) -> bool | None:
    """Return the terminal grid mode without collapsing unknown transitions."""
    status = _terminal_grid_status(value)
    if status is None:
        return None
    return status in {"Inactive", "Islanded", "Off-Grid", "SystemIslandedActive"}

def normalize_custom_power_kw(value: Any, unit: str = "") -> float | None:
    """Normalize custom HA power telemetry to finite kW."""
    if value is None:
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric_value):
        return None

    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit in ("w", "watt", "watts"):
        return numeric_value / 1000.0
    if normalized_unit in ("mw", "megawatt", "megawatts"):
        return numeric_value * 1000.0
    if normalized_unit in ("kw", "kilowatt", "kilowatts"):
        return numeric_value
    return numeric_value / 1000.0 if abs(numeric_value) > 100 else numeric_value

def _finite_float(value: Any) -> float | None:
    """Return a finite numeric value, preserving missing/invalid telemetry."""
    if isinstance(value, bool):
        return None
    try:
        numeric_value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric_value if math.isfinite(numeric_value) else None

def _inverter_poll_datetime(attrs: dict[str, Any]) -> datetime | None:
    """Parse the AC inverter poll timestamp when one is available."""
    raw_value = attrs.get("last_poll")
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

def _is_night_for_solar_telemetry(hass: HomeAssistant) -> bool:
    """Return whether real solar telemetry should be impossible or near-zero."""
    try:
        sun_state = getattr(hass, "states", None).get("sun.sun")
        if sun_state is not None:
            if sun_state.state == "below_horizon":
                return True
            if sun_state.state == "above_horizon":
                return False
    except Exception:
        pass

    local_hour = dt_util.now().hour
    return local_hour >= 18 or local_hour < 6

def _stored_battery_health_capacity_kwh(
    hass: HomeAssistant, entry_id: str
) -> float | None:
    """Return the latest BMS-scanned current Powerwall capacity in kWh."""
    health = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("battery_health") or {}
    capacity_wh = health.get("current_capacity_wh")
    try:
        capacity_kwh = float(capacity_wh) / 1000.0
    except (TypeError, ValueError):
        return None
    return round(capacity_kwh, 2) if capacity_kwh > 0 else None


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

def _parse_retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Parse Retry-After header from an HTTP response.

    Returns delay in seconds, or None if header is missing/invalid.
    Supports both delta-seconds and HTTP-date formats.
    """
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        # Try delta-seconds first (e.g. "30")
        return max(1.0, min(float(retry_after), 300.0))  # Clamp 1-300s
    except (ValueError, TypeError):
        pass
    try:
        # Try HTTP-date format (e.g. "Tue, 11 Feb 2026 03:00:00 GMT")
        from email.utils import parsedate_to_datetime

        retry_date = parsedate_to_datetime(retry_after)
        from homeassistant.util import dt as dt_util

        delay = (retry_date - dt_util.utcnow()).total_seconds()
        return max(1.0, min(delay, 300.0))  # Clamp 1-300s
    except (ValueError, TypeError):
        return None

async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    max_retries: int = 3,
    timeout_seconds: int = 60,
    raise_auth_failed: bool = True,
    **kwargs,
) -> dict[str, Any]:
    """Fetch data with exponential backoff retry logic.

    Respects Retry-After headers from 429/503 responses. Retries on
    5xx server errors and 429 rate limits; fails immediately on other 4xx.

    Args:
        session: aiohttp client session
        url: URL to fetch
        headers: Request headers
        max_retries: Maximum number of retry attempts (default: 3)
        timeout_seconds: Request timeout in seconds (default: 60)
        raise_auth_failed: Whether 401 responses should raise
            ConfigEntryAuthFailed instead of UpdateFailed
        **kwargs: Additional arguments to pass to session.get()

    Returns:
        JSON response data

    Raises:
        UpdateFailed: If all retries fail
    """
    last_error = None
    retry_after_delay = None  # Set by Retry-After header

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                # Use Retry-After delay if available, otherwise exponential backoff
                wait_time = retry_after_delay or (2**attempt)
                retry_after_delay = None  # Reset for next attempt
                _LOGGER.info(
                    "Retry attempt %d/%d after %.0fs delay",
                    attempt + 1,
                    max_retries,
                    wait_time,
                )
                await asyncio.sleep(wait_time)

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                **kwargs,
            ) as response:
                if response.status == 200:
                    return await response.json()

                error_text = await response.text()

                if response.status == 429:
                    # Rate limited — retry with Retry-After if provided
                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Rate limited 429 (attempt %d/%d): %s (retry-after: %s)",
                        attempt + 1,
                        max_retries,
                        error_text[:200],
                        f"{retry_after_delay:.0f}s" if retry_after_delay else "not set",
                    )
                    last_error = UpdateFailed("Rate limited: 429")
                    continue

                if response.status >= 500:
                    # Server error — retry, respect Retry-After if present
                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Server error (attempt %d/%d): %s - %s",
                        attempt + 1,
                        max_retries,
                        response.status,
                        error_text[:200],
                    )
                    last_error = UpdateFailed(f"Server error: {response.status}")
                    continue

                # 401 → token expired/revoked. Direct token providers should
                # trigger HA reauth. Fleet API tokens are owned/refreshed by
                # the separate tesla_fleet integration, so callers can treat
                # them as transient stale-token failures instead.
                if response.status == 401:
                    if raise_auth_failed:
                        _LOGGER.warning(
                            "Authentication failed (401) — triggering reauth: %s",
                            error_text[:200],
                        )
                        raise ConfigEntryAuthFailed(
                            f"Token rejected by upstream: {error_text[:200]}"
                        )
                    _LOGGER.warning(
                        "Authentication failed (401) — token may be refreshing upstream: %s",
                        error_text[:200],
                    )
                    raise UpdateFailed(
                        f"Authentication failed: 401 - {error_text[:200]}"
                    )

                # Other 4xx client errors — don't retry
                raise UpdateFailed(f"Client error {response.status}: {error_text}")

        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Network error (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                err,
            )
            last_error = UpdateFailed(f"Network error: {err}")
            continue

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timeout error (attempt %d/%d): Request exceeded %ds",
                attempt + 1,
                max_retries,
                timeout_seconds,
            )
            last_error = UpdateFailed(f"Timeout after {timeout_seconds}s")
            continue

    # All retries failed
    raise last_error or UpdateFailed("All retry attempts failed")

class TeslaEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Tesla energy data from Tesla API (Fleet API)."""

    def __init__(
        self,
        hass: HomeAssistant,
        site_id: str,
        api_token: str,
        api_provider: str = TESLA_PROVIDER_FLEET_API,
        token_getter: callable | None = None,
        entry_id: str = "",
        fleet_base_url: str | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            site_id: Tesla energy site ID
            api_token: Initial API token (used if token_getter not provided)
            api_provider: API provider (teslemetry or fleet_api)
            token_getter: Optional callable that returns (token, provider) tuple.
                          If provided, this is called before each request to get fresh token.
            entry_id: Config entry ID for price lookups
            fleet_base_url: Regional Fleet API base URL override (EU/AP users).
                            Stored in entry.data[CONF_FLEET_API_BASE_URL].
        """
        self.site_id = site_id
        self._api_token = api_token  # Fallback token
        self._token_getter = token_getter  # Callable to get fresh token
        self.api_provider = api_provider
        self._entry_id = entry_id
        self._fleet_base_url = fleet_base_url  # Per-entry regional URL override
        self.session = async_get_clientsession(hass)
        self._site_info_cache = (
            None  # Cache site_info (normally refreshed every 6 hours)
        )
        self._site_info_last_fetch: float = 0  # Timestamp of last successful fetch
        self._site_info_fetch_failed = (
            False  # Negative cache to avoid retrying on every sync cycle
        )
        self._firmware = None  # Extracted from site_info gateways
        self._last_valid_battery_level_pct: float | None = None

        # Tesla Energy Site capability detection (populated by probe on first site_info fetch).
        # Keys: storm_mode, off_grid_vehicle_charging_reserve, vpp_programs.
        # Value True means the feature is supported by this site; False means unsupported
        # (either Tesla returned 4xx on probe, or the feature is not available in this country).
        self.tesla_capabilities: dict[str, bool] = {}
        self._capabilities_probed = False
        self._site_country: str | None = (
            None  # From site_info (used to gate region-locked features)
        )

        # Cached current-state values for new energy-site controls (populated opportunistically)
        self._storm_mode_enabled: bool | None = None
        self._off_grid_reserve_percent: int | None = None
        self._vpp_programs_cache: list[dict] | None = None

        # Grid status tracking (off-grid / islanding detection)
        self._last_grid_status: str | None = None

        # Tesla server outage tracking
        self._consecutive_failures: int = 0
        self._failure_streak_start: float = 0  # monotonic timestamp
        self._outage_notified: bool = False
        self._outage_start: float = 0  # monotonic timestamp
        self._last_outage_notification: float = 0  # monotonic timestamp (cooldown)
        self._auth_expiry_notified = False

        # Lifetime energy totals (refreshed hourly from calendar_history period=lifetime)
        self._lifetime_totals: dict[str, float] | None = None
        self._lifetime_last_fetch: float = 0
        self._lifetime_fetch_failed: bool = False
        self._lifetime_totals_restored: bool = False
        self._lifetime_totals_store = Store(
            hass,
            LIFETIME_TOTALS_STORE_VERSION,
            f"power_sync.lifetime_totals.{entry_id or site_id}",
        )

        # Determine API base URL based on provider
        if api_provider == TESLA_PROVIDER_FLEET_API:
            self.api_base_url = fleet_base_url or FLEET_API_BASE_URL
            _LOGGER.info(
                f"TeslaEnergyCoordinator initialized with Fleet API for site {site_id} (base: {self.api_base_url})"
            )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tesla_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    def _resolve_battery_level_pct(self, live_status: dict[str, Any]) -> float | None:
        """Return Tesla SOC, preserving the last valid value when omitted."""
        raw_soc = live_status.get("percentage_charged")
        if raw_soc is not None:
            try:
                soc = float(raw_soc)
            except (TypeError, ValueError):
                soc = None
            if soc is not None and 0 <= soc <= 100:
                self._last_valid_battery_level_pct = soc
                return soc

        if self._last_valid_battery_level_pct is not None:
            _LOGGER.debug(
                "Tesla live_status omitted percentage_charged; keeping last valid SOC %.1f%%",
                self._last_valid_battery_level_pct,
            )
            return self._last_valid_battery_level_pct

        _LOGGER.debug(
            "Tesla live_status omitted percentage_charged and no cached SOC is available"
        )
        return None

    def _record_tesla_update_failure(self, now: float) -> tuple[bool, float]:
        """Record a Tesla update failure and return whether to send outage notice."""
        self._consecutive_failures += 1
        if self._consecutive_failures == 1 or not self._failure_streak_start:
            self._failure_streak_start = now
        failure_duration = now - self._failure_streak_start
        should_notify = (
            self._consecutive_failures >= TESLA_OUTAGE_NOTIFY_FAILURES
            and failure_duration >= TESLA_OUTAGE_NOTIFY_MIN_SECONDS
            and not self._outage_notified
        )
        return should_notify, failure_duration

    def _local_powerwall_energy_data(self) -> dict[str, Any] | None:
        """Return energy data from the paired local Powerwall coordinator."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        local_runtime = entry_data.get("powerwall_local") or {}
        local_coordinator = local_runtime.get("coordinator")
        snap = getattr(local_coordinator, "data", None)
        if snap is None:
            return None

        def _kw(value: Any) -> float:
            try:
                return round(float(value or 0.0) / 1000.0, 3)
            except (TypeError, ValueError):
                return 0.0

        solar_kw = _kw(getattr(snap, "solar_w", None))
        grid_kw = _kw(getattr(snap, "grid_w", None))
        battery_kw = _kw(getattr(snap, "battery_w", None))
        raw_load_kw = _kw(getattr(snap, "load_w", None))

        # The raw gateway load (snap.load_w) includes EV charging power (eg a
        # Tesla Wall Connector), same as Tesla cloud's live_status.load_power
        # above. The main cloud path subtracts ev_power_kw before it ever
        # reaches the load estimator (see load_kw computation earlier in this
        # method) — mirror that here using the same "observed EV power"
        # signal PowerwallLocalCoordinator.snapshot_as_api() subtracts via
        # its _observed_ev_power_w() (powerwall_local/coordinator.py), so a
        # Tesla cloud outage with a car charging doesn't poison home_load's
        # recorder history with EV draw. Defensive: EV power may be
        # unavailable (older/duck-typed coordinator) — treat as 0 and never
        # let load go negative.
        observed_ev_power_w = getattr(local_coordinator, "_observed_ev_power_w", None)
        ev_power_kw = 0.0
        ev_load_complete = True
        if callable(observed_ev_power_w):
            try:
                observed = observed_ev_power_w()
                if isinstance(observed, tuple):
                    observed, ev_load_complete = observed
                ev_power_kw = _kw(observed)
            except Exception:
                ev_power_kw = 0.0
                ev_load_complete = False
        load_kw = max(0.0, raw_load_kw - ev_power_kw) if ev_load_complete else None

        if load_kw is not None:
            self._energy_acc.update(
                max(0, solar_kw), grid_kw, battery_kw, load_kw, 0.0, 0.0
            )

        local_is_off_grid = _grid_status_is_off_grid(getattr(snap, "grid_status", None))
        grid_status = (
            None
            if local_is_off_grid is None
            else "Off-Grid"
            if local_is_off_grid
            else "Active"
        )
        soc_pct = getattr(snap, "soc", None)
        if soc_pct is not None:
            try:
                soc_pct = float(soc_pct)
            except (TypeError, ValueError):
                soc_pct = None
            else:
                self._last_valid_battery_level_pct = soc_pct

        total_pack_kwh: float | None = None
        total_pack_wh = getattr(snap, "total_pack_full_wh", None)
        if total_pack_wh is not None:
            try:
                total_pack_kwh = round(float(total_pack_wh) / 1000.0, 2)
            except (TypeError, ValueError):
                total_pack_kwh = None
        if total_pack_kwh is None:
            total_pack_kwh = _stored_battery_health_capacity_kwh(
                self.hass, self._entry_id
            )

        energy_left_kwh: float | None = None
        remaining_wh = getattr(snap, "total_pack_remaining_wh", None)
        if remaining_wh is not None:
            try:
                energy_left_kwh = round(float(remaining_wh) / 1000.0, 2)
            except (TypeError, ValueError):
                energy_left_kwh = None
        if (
            energy_left_kwh is None
            and total_pack_kwh is not None
            and soc_pct is not None
        ):
            energy_left_kwh = round(total_pack_kwh * (soc_pct / 100.0), 2)

        backup_hours: float | None = None
        if energy_left_kwh is not None and load_kw and load_kw > 0.05:
            backup_hours = round(min(999.0, energy_left_kwh / load_kw), 1)

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "raw_home_load_power": raw_load_kw,
            "home_load_basis": ("excludes_ev" if ev_load_complete else "unknown"),
            "home_load_normalization_quality": (
                "complete" if ev_load_complete else "incomplete"
            ),
            "battery_level": soc_pct,
            "grid_status": grid_status,
            "ev_power": ev_power_kw,
            "last_update": dt_util.utcnow(),
            "energy_summary": self._energy_acc.as_dict(),
            "firmware": self._firmware,
            "battery_max_charge_power": None,
            "battery_max_discharge_power": None,
            "battery_max_charge_power_w": None,
            "battery_max_discharge_power_w": None,
            "total_pack_energy_kwh": total_pack_kwh,
            "energy_left_kwh": energy_left_kwh,
            "backup_time_remaining_hours": backup_hours,
            "grid_services_active": False,
            "grid_services_power_kw": 0.0,
            "lifetime_totals": self._lifetime_totals,
            "data_source": "powerwall_local",
        }

    def _get_current_token(self) -> str | None:
        """Get the current API token, fetching fresh if token_getter is available.

        Returns None if token_getter is set but returned no token — callers must
        treat this as a transient failure and raise UpdateFailed rather than
        falling back to the potentially stale startup token.
        """
        if self._token_getter:
            try:
                token, provider = self._token_getter()
                if token:
                    # Update provider and base URL if it changed
                    if provider != self.api_provider:
                        self.api_provider = provider
                        if provider == TESLA_PROVIDER_FLEET_API:
                            self.api_base_url = (
                                self._fleet_base_url or FLEET_API_BASE_URL
                            )
                        _LOGGER.debug("Token provider changed to %s", provider)
                    return token
                # token_getter returned None — fleet integration may be mid-refresh
                _LOGGER.warning(
                    "Token getter returned no token (fleet integration may be refreshing) — skipping poll"
                )
                return None
            except Exception as e:
                _LOGGER.warning("Token getter failed — skipping poll: %s", e)
                return None
        return self._api_token

    def _coerce_lifetime_totals(self, data: Any) -> dict[str, float]:
        """Extract persisted lifetime totals as floats."""
        if not isinstance(data, dict):
            return {}
        totals: dict[str, float] = {}
        for key in LIFETIME_TOTAL_KEYS:
            value = data.get(key)
            if value is None:
                continue
            try:
                totals[key] = float(value)
            except (TypeError, ValueError):
                continue
        return totals

    def _clamp_lifetime_totals(self, totals: dict[str, float]) -> dict[str, float]:
        """Keep lifetime counters monotonic for total_increasing sensors."""
        previous = self._lifetime_totals or {}
        if not previous:
            return totals

        clamped = dict(totals)
        for key, value in totals.items():
            previous_value = previous.get(key)
            if previous_value is None or value >= previous_value:
                continue
            clamped[key] = previous_value
            _LOGGER.debug(
                "Keeping %s monotonic: Tesla reported %.3f kWh after %.3f kWh",
                key,
                value,
                previous_value,
            )
        return clamped

    async def async_restore_lifetime_totals(self) -> None:
        """Restore persisted lifetime totals before the first coordinator state."""
        if self._lifetime_totals_restored:
            return
        self._lifetime_totals_restored = True

        if not hasattr(self._lifetime_totals_store, "async_load"):
            return
        try:
            data = await self._lifetime_totals_store.async_load()
        except Exception as err:
            _LOGGER.warning("Failed to load persisted lifetime totals: %s", err)
            return

        totals = self._coerce_lifetime_totals(data)
        if not totals:
            return

        self._lifetime_totals = totals
        _LOGGER.info("Restored Tesla lifetime totals from storage")

    async def async_flush_lifetime_totals(self) -> None:
        """Persist lifetime totals so recorder-safe maxima survive restarts."""
        if not self._lifetime_totals or not hasattr(
            self._lifetime_totals_store, "async_save"
        ):
            return
        await self._lifetime_totals_store.async_save(
            {key: round(value, 3) for key, value in self._lifetime_totals.items()}
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Tesla API (TFleet API)."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()
        if not self._lifetime_totals_restored:
            await self.async_restore_lifetime_totals()

        current_token = self._get_current_token()
        if not current_token:
            raise UpdateFailed(
                "Tesla token temporarily unavailable — will retry next poll"
            )
        headers = self._tesla_headers(current_token)

        try:
            # Fleet API stream continue through the proven REST retry path.
            data = await _fetch_with_retry(
                self.session,
                f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/live_status",
                headers,
                max_retries=3,
                timeout_seconds=60,
                raise_auth_failed=self.api_provider != TESLA_PROVIDER_FLEET_API,
            )
            live_status = data.get("response") or {}
            _LOGGER.debug("Tesla API live_status response: %s", live_status)

            # Tesla returns {"response": null} occasionally during transient failures
            # or right after a token mint when the account state is still propagating.
            # Treat null/missing response as a temporary outage to avoid crashing.
            if not live_status:
                local_energy_data = self._local_powerwall_energy_data()
                if local_energy_data is not None:
                    _LOGGER.warning(
                        "Tesla returned empty live_status response; using paired "
                        "Powerwall local snapshot for energy telemetry"
                    )
                    return local_energy_data
                raise UpdateFailed("Tesla returned empty live_status response")

            sample_observed_at = dt_util.utcnow()

            # A partial non-empty Tesla response is not a valid battery sample.
            # In particular, treating an omitted battery_power as 0 W makes a
            # missing measurement look like an observed idle Powerwall and lets
            # it enter optimizer planning.  Preserve an explicit numeric zero.
            battery_power_w = _finite_float(live_status.get("battery_power"))
            if battery_power_w is None:
                raise UpdateFailed(
                    "Tesla live_status response omitted valid battery_power"
                )

            # Map Teslemetry API response to our data structure.
            solar_kw = live_status.get("solar_power", 0) / 1000
            grid_kw = live_status.get("grid_power", 0) / 1000
            battery_kw = battery_power_w / 1000
            raw_load_kw = live_status.get("load_power", 0) / 1000
            load_kw = max(0.0, raw_load_kw)

            # Tesla caches the site aggregate upstream for ~60s while the wall
            # connector reading in the same payload refreshes every poll. Record
            # the EV power observed when the aggregate last moved so consumers
            # can keep both terms the same vintage; splicing a newer EV number
            # into an older aggregate breaks its energy balance. Discord #284.
            aggregate_sample = (solar_kw, grid_kw, battery_kw, raw_load_kw)
            if aggregate_sample != getattr(self, "_site_aggregate_sample", None):
                self._site_aggregate_sample = aggregate_sample

            # Fetch site_info periodically to detect firmware updates (every 6 hours)
            _site_info_stale = (
                time.monotonic() - self._site_info_last_fetch
            ) > TESLA_SITE_INFO_CACHE_TTL_SECONDS
            if _site_info_stale and not self._site_info_fetch_failed:
                try:
                    await self.async_get_site_info()
                except Exception:
                    pass  # Non-critical, don't fail the update

            grid_status = _terminal_grid_status(live_status.get("grid_status"))

            # Detect grid status transitions and send push notifications.
            # Unknown/intermediate states neither notify nor replace the last
            # terminal state. The first terminal sample establishes baseline.
            is_off_grid = _grid_status_is_off_grid(grid_status)
            prev_is_off_grid = _grid_status_is_off_grid(self._last_grid_status)
            if grid_status is not None:
                self._last_grid_status = grid_status
            if (
                is_off_grid is not None
                and prev_is_off_grid is not None
                and is_off_grid != prev_is_off_grid
            ):
                try:
                    from .automations.actions import _send_expo_push

                    if is_off_grid:
                        _LOGGER.warning(
                            "Grid outage detected — Powerwall off-grid (site %s)",
                            self.site_id,
                        )
                        await _send_expo_push(
                            self.hass,
                            "Grid Outage Detected",
                            "Your Powerwall is running off-grid. Grid power is unavailable.",
                        )
                    else:
                        _LOGGER.info(
                            "Grid restored — Powerwall back on-grid (site %s)",
                            self.site_id,
                        )
                        await _send_expo_push(
                            self.hass,
                            "Grid Power Restored",
                            "Grid power has been restored. Your Powerwall is back on-grid.",
                        )
                except Exception:
                    pass

            # Derive the per-site nameplate power from cached site_info
            # (refreshed every 6 hours). Powerwall 2 is 5 kW continuous and
            # Powerwall 3 is 11.5 kW continuous; nameplate_power on Tesla's
            # /live_status payload is the total site rating in watts so it
            # covers single- and multi-unit installs. Both charge and
            # discharge use the same ceiling.
            nameplate_w = None
            if self._site_info_cache:
                nameplate_w = self._site_info_cache.get("nameplate_power")
            nameplate_kw = round(nameplate_w / 1000.0, 2) if nameplate_w else None

            # Total pack energy (nameplate Wh) and energy_left (stored Wh) come
            # from live_status when Tesla supplies them. When live_status omits
            # pack capacity, prefer the BMS-scanned Battery Health capacity over
            # the static battery_count × per-unit nameplate fallback.
            total_pack_kwh: float | None = None
            tpe_w = live_status.get("total_pack_energy")
            if tpe_w is not None:
                try:
                    total_pack_kwh = round(float(tpe_w) / 1000.0, 2)
                except (TypeError, ValueError):
                    total_pack_kwh = None
            if total_pack_kwh is None:
                total_pack_kwh = _stored_battery_health_capacity_kwh(
                    self.hass,
                    self._entry_id,
                )
            if total_pack_kwh is None and self._site_info_cache:
                # Last-resort fallback when no BMS scan has populated live
                # capacity yet.
                count = (self._site_info_cache.get("components") or {}).get(
                    "battery_count"
                ) or self._site_info_cache.get("battery_count")
                if count:
                    try:
                        total_pack_kwh = round(int(count) * 13.5, 2)
                    except (TypeError, ValueError):
                        pass

            soc_pct = self._resolve_battery_level_pct(live_status)
            energy_left_kwh: float | None = None
            el_w = live_status.get("energy_left")
            if el_w is not None:
                try:
                    energy_left_kwh = round(float(el_w) / 1000.0, 2)
                except (TypeError, ValueError):
                    energy_left_kwh = None
            if (
                energy_left_kwh is None
                and total_pack_kwh is not None
                and soc_pct is not None
            ):
                energy_left_kwh = round(total_pack_kwh * (soc_pct / 100.0), 2)

            # Backup time remaining (hours): stored kWh / current home load.
            # Caps at 999 to keep the UI sane when load drops near zero.
            backup_hours: float | None = None
            if energy_left_kwh is not None and load_kw and load_kw > 0.05:
                backup_hours = round(min(999.0, energy_left_kwh / load_kw), 1)

            # Grid services / VPP — present in live_status when site is enrolled.
            # When the site has no VPP the field is typically absent or 0;
            # default the power reading to 0 so the sensor reads a real value
            # ("0 W") rather than "Unknown" — much more useful for graphs.
            grid_services_active = bool(live_status.get("grid_services_active", False))
            grid_services_power_kw: float = 0.0
            gsp = live_status.get("grid_services_power")
            if gsp is not None:
                try:
                    grid_services_power_kw = round(float(gsp) / 1000.0, 3)
                except (TypeError, ValueError):
                    grid_services_power_kw = 0.0

            energy_data = {
                "solar_power": solar_kw,
                "grid_power": grid_kw,
                "battery_power": battery_kw,
                "load_power": load_kw,
                "raw_home_load_power": raw_load_kw,
                "site_load_power": max(0.0, raw_load_kw),
                "battery_level": soc_pct,
                "grid_status": grid_status,
                "last_update": sample_observed_at,
                "energy_summary": self._energy_acc.as_dict(),
                "firmware": self._firmware,
                # BMS ceiling for the mobile force-mode picker's Max chip
                "battery_max_charge_power": nameplate_kw,
                "battery_max_discharge_power": nameplate_kw,
                "battery_max_charge_power_w": nameplate_w,
                "battery_max_discharge_power_w": nameplate_w,
                # Powerwall extended fields
                "total_pack_energy_kwh": total_pack_kwh,
                "energy_left_kwh": energy_left_kwh,
                "backup_time_remaining_hours": backup_hours,
                "grid_services_active": grid_services_active,
                "grid_services_power_kw": grid_services_power_kw,
                "lifetime_totals": self._lifetime_totals,
            }

            # Refresh lifetime totals once per hour (best-effort, never fails the poll)
            _lifetime_stale = (time.monotonic() - self._lifetime_last_fetch) > 3600
            if _lifetime_stale and not self._lifetime_fetch_failed:
                try:
                    await self.async_refresh_lifetime_totals()
                    energy_data["lifetime_totals"] = self._lifetime_totals
                except Exception as err:
                    _LOGGER.debug("Lifetime totals refresh failed: %s", err)

            # Tesla API recovered — send recovery notification if we were in outage
            if self._outage_notified:
                outage_mins = int((time.monotonic() - self._outage_start) / 60)
                _LOGGER.warning(
                    "Tesla API recovered after %d min outage (site %s)",
                    outage_mins,
                    self.site_id,
                )
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        self.hass,
                        "Tesla Server Recovered",
                        f"Tesla API is back online after {outage_mins} min outage",
                    )
                except Exception:
                    pass
            self._consecutive_failures = 0
            self._failure_streak_start = 0
            self._outage_notified = False

            return energy_data

        except ConfigEntryAuthFailed:
            # Home Assistant opens the repair/reauth flow, but that is easy to
            # miss when the user primarily monitors PowerSync from the mobile
            # app.  Notify every registered app device once per coordinator
            # before handing control back to HA.  Notification failures must
            # never mask or delay the reauth exception.
            if not self._auth_expiry_notified:
                self._auth_expiry_notified = True
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        self.hass,
                        "PowerSync Connection Expired",
                        "Re-authenticate in Home Assistant Settings > Repairs. "
                        "Automations and battery control may be unavailable.",
                    )
                except Exception as notify_err:
                    _LOGGER.debug(
                        "Could not send PowerSync authentication-expired notification: %s",
                        notify_err,
                    )
            # Don't retry — let HA's reauth flow take over.
            raise
        except (UpdateFailed, Exception) as err:
            now = time.monotonic()
            should_notify, failure_duration = self._record_tesla_update_failure(now)

            # Notify only after a sustained failure window. Refreshes can be
            # requested faster than the normal update interval, so attempt
            # count alone can report a short Tesla empty-response burst as a
            # server outage.
            if should_notify:
                self._outage_notified = True
                self._outage_start = self._failure_streak_start
                self._last_outage_notification = now
                _LOGGER.error(
                    "Tesla server outage detected: %d consecutive failures over %.0fs (site %s)",
                    self._consecutive_failures,
                    failure_duration,
                    self.site_id,
                )
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        self.hass,
                        "Tesla Server Outage",
                        f"Tesla API unreachable — optimization paused. Error: {err}",
                    )
                except Exception:
                    pass
            elif (
                self._outage_notified and (now - self._last_outage_notification) > 1800
            ):
                # Repeat notification every 30 min during ongoing outage
                outage_mins = int((now - self._outage_start) / 60)
                self._last_outage_notification = now
                try:
                    from .automations.actions import _send_expo_push

                    await _send_expo_push(
                        self.hass,
                        "Tesla Server Outage",
                        f"Tesla API still unreachable after {outage_mins} min",
                    )
                except Exception:
                    pass

            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(
                f"Unexpected error fetching Tesla energy data: {err}"
            ) from err

    async def async_get_site_info(
        self,
        max_age: float | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch site_info from Tesla API (Fleet API).

        Includes installation_time_zone which is critical for correct TOU schedule alignment.
        Results are cached since site info (especially timezone) doesn't change.

        Returns:
            Site info dict containing installation_time_zone, or None if fetch fails
        """
        cache_ttl = (
            TESLA_SITE_INFO_CACHE_TTL_SECONDS
            if max_age is None
            else max(0, float(max_age))
        )

        # Return cached value if still fresh.
        if (
            self._site_info_cache
            and (time.monotonic() - self._site_info_last_fetch) <= cache_ttl
        ):
            _LOGGER.debug("Returning cached site_info")
            return self._site_info_cache

        # Don't retry if a previous fetch already failed (avoids spamming logs every sync cycle)
        if self._site_info_fetch_failed:
            return None

        current_token = self._get_current_token()
        headers = self._tesla_headers(current_token)

        try:
            _LOGGER.info(f"Fetching site_info for site {self.site_id}")

            data = await _fetch_with_retry(
                self.session,
                f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/site_info",
                headers,
                max_retries=3,
                timeout_seconds=60,
                raise_auth_failed=self.api_provider != TESLA_PROVIDER_FLEET_API,
            )

            site_info = data.get("response", {})

            # Log timezone info for debugging
            installation_tz = site_info.get("installation_time_zone")
            if installation_tz:
                _LOGGER.info(f"Found Powerwall timezone: {installation_tz}")
            else:
                _LOGGER.warning("No installation_time_zone in site_info response")

            # Log battery capacity info for debugging
            _LOGGER.debug(f"Site info keys: {list(site_info.keys())}")
            components = site_info.get("components", {})
            if components:
                _LOGGER.debug(f"Site info components keys: {list(components.keys())}")
                # Log battery-related fields
                battery_fields = {
                    k: v
                    for k, v in site_info.items()
                    if "battery" in k.lower()
                    or "pack" in k.lower()
                    or "energy" in k.lower()
                    or "power" in k.lower()
                }
                if battery_fields:
                    _LOGGER.debug(f"Site info battery fields: {battery_fields}")
                component_battery = {
                    k: v
                    for k, v in components.items()
                    if "battery" in k.lower() or "nameplate" in k.lower()
                }
                if component_battery:
                    _LOGGER.debug(f"Components battery fields: {component_battery}")

            # Extract firmware version
            gateways = components.get("gateways", []) or site_info.get("gateways", [])
            if gateways:
                gateway = gateways[0]
                _LOGGER.info("Gateway keys: %s", list(gateway.keys()))
                fw_version = (
                    gateway.get("firmware_version")
                    or gateway.get("version")
                    or gateway.get("gateway_firmware_version")
                    or gateway.get("fw_version")
                    or ""
                )
                if fw_version:
                    self._firmware = fw_version
                    _LOGGER.info("Firmware version: %s", fw_version)
                else:
                    _LOGGER.info("No firmware key found in gateway: %s", gateway)

            # Extract country (used for region-gating; Tesla reports ISO country code
            # in site_info for Energy Sites, though the key has varied historically).
            self._site_country = (
                site_info.get("country")
                or site_info.get("installation_country")
                or components.get("country")
            )

            # Opportunistically capture current state for new energy-site controls.
            # Tesla returns these in site_info when available; otherwise we fall back
            # to explicit GET calls during the capability probe.
            if "off_grid_vehicle_charging_reserve_percent" in site_info:
                self._off_grid_reserve_percent = site_info.get(
                    "off_grid_vehicle_charging_reserve_percent"
                )
            elif "off_grid_vehicle_charging_reserve_percent" in components:
                self._off_grid_reserve_percent = components.get(
                    "off_grid_vehicle_charging_reserve_percent"
                )

            storm_mode_active = (
                site_info.get("storm_mode_active")
                if "storm_mode_active" in site_info
                else components.get("storm_mode_active")
            )
            storm_mode_enabled = (
                site_info.get("user_settings", {}).get("storm_mode_enabled")
                if isinstance(site_info.get("user_settings"), dict)
                else None
            )
            if storm_mode_enabled is not None:
                self._storm_mode_enabled = bool(storm_mode_enabled)
            elif storm_mode_active is not None:
                self._storm_mode_enabled = bool(storm_mode_active)

            # Cache the result with timestamp
            self._site_info_cache = site_info
            self._site_info_last_fetch = time.monotonic()

            # Schedule one-shot capability probe on first successful fetch.
            # Runs in background to avoid blocking the main fetch path.
            if not self._capabilities_probed:
                self._capabilities_probed = True
                self.hass.async_create_task(
                    self._async_probe_tesla_capabilities(),
                    name=f"{DOMAIN}_tesla_capability_probe",
                )

            return site_info

        except UpdateFailed as err:
            _LOGGER.warning(
                "Failed to fetch site_info: %s (will not retry until next restart)", err
            )
            self._site_info_fetch_failed = True
            return None
        except Exception as err:
            _LOGGER.warning(
                "Unexpected error fetching site_info: %s (will not retry until next restart)",
                err,
            )
            self._site_info_fetch_failed = True
            return None

    def invalidate_site_info_cache(self) -> None:
        """Force the next async_get_site_info() call to re-fetch from Tesla.

        Call this after any write that modifies site_info-level fields
        (backup reserve, operation mode, grid export rule, grid charging,
        storm mode, off-grid EV reserve, VPP enrollment) so that HA
        entities reading from the cache don't display stale values for
        up to six hours until the next natural refresh.
        """
        # Clear the cached payload itself, not just the timestamp.
        # async_get_site_info() returns cached data while it is inside the
        # caller's max_age window. Resetting only _site_info_last_fetch can
        # still leave a shorter-uptime HA instance inside that window, so clear
        # the cached payload itself to force the next call to refetch.
        self._site_info_cache = None
        self._site_info_last_fetch = 0
        self._site_info_fetch_failed = False
        _LOGGER.debug("Tesla site_info cache invalidated — next read will refetch")

    async def set_grid_charging_enabled(self, enabled: bool) -> bool:
        """Set grid charging and return only after direct readback confirms it."""
        _LOGGER.info(
            "Setting grid charging %s for site %s",
            "enabled" if enabled else "disabled",
            self.site_id,
        )
        try:
            outcome = await async_set_tesla_grid_charging_confirmed(
                self.session,
                self.api_base_url,
                str(self.site_id),
                self._tesla_headers(self._get_current_token()),
                enabled,
            )
        except Exception as err:
            _LOGGER.error("Error setting grid charging: %s", err)
            return False
        if outcome.applied:
            self.invalidate_site_info_cache()
            return True

        _LOGGER.error(
            "Grid charging %s did not verify for site %s (%s%s)",
            "enable" if enabled else "disable",
            self.site_id,
            outcome.status.value,
            f": {outcome.detail}" if outcome.detail else "",
        )
        return False

    # ------------------------------------------------------------------
    # Unified Tesla Energy Site API helper
    # ------------------------------------------------------------------

    def _tesla_headers(self, token: str | None = None) -> dict[str, str]:
        """Build authorization headers using the freshest token."""
        headers = {
            "Authorization": f"Bearer {token or self._get_current_token()}",
            "Content-Type": "application/json",
            "User-Agent": TESLA_V1R_USER_AGENT,
        }

        return headers

    async def _tesla_api_call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        max_retries: int = 3,
        timeout_seconds: int = 30,
    ) -> tuple[int, dict | None]:
        """Make a Tesla Energy Site API call with retry/backoff.

        Returns (status_code, response_json_or_none). Retries on 429/5xx using
        Retry-After if provided, otherwise exponential backoff. Does NOT raise
        on 4xx — callers interpret status codes (e.g. probe uses 4xx to detect
        unsupported features).
        """
        url = f"{self.api_base_url}{path}"
        last_status = 0
        retry_after_delay: float | None = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    wait_time = retry_after_delay or (2**attempt)
                    retry_after_delay = None
                    await asyncio.sleep(wait_time)

                headers = self._tesla_headers()
                request = self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body if method.upper() != "GET" else None,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                )
                async with request as response:
                    last_status = response.status
                    if response.status == 200:
                        try:
                            return response.status, await response.json()
                        except Exception:
                            return response.status, None

                    if response.status in (429, 500, 502, 503, 504):
                        retry_after_delay = _parse_retry_after(response)
                        _LOGGER.warning(
                            "Tesla %s %s attempt %d/%d: %s",
                            method,
                            path,
                            attempt + 1,
                            max_retries,
                            response.status,
                        )
                        continue

                    # Non-retryable status — return as-is for caller inspection
                    try:
                        return response.status, await response.json()
                    except Exception:
                        return response.status, None

            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Tesla %s %s attempt %d/%d timed out",
                    method,
                    path,
                    attempt + 1,
                    max_retries,
                )
                continue
            except aiohttp.ClientError as err:
                _LOGGER.warning(
                    "Tesla %s %s attempt %d/%d network error: %s",
                    method,
                    path,
                    attempt + 1,
                    max_retries,
                    err,
                )
                continue

        return last_status or 0, None

    # ------------------------------------------------------------------
    # Capability probe (run once after first site_info fetch)
    # ------------------------------------------------------------------

    async def _async_probe_tesla_capabilities(self) -> None:
        """Probe Tesla Energy Site endpoints to determine which features are supported.

        Tesla does not expose clean feature flags; instead we attempt a harmless
        GET on each new endpoint and interpret the response:
          - 200: feature supported → True
          - 404 / 501 / 400 "not_supported": unsupported → False
          - other 4xx: unknown (assume supported so user can retry)
          - 5xx / network error: unknown (assume supported; probe again later)
        Results are cached in self.tesla_capabilities and persist until restart.
        """
        _LOGGER.info("Probing Tesla Energy Site capabilities for site %s", self.site_id)

        async def _probe(name: str, path: str) -> bool:
            status, _body = await self._tesla_api_call(
                "GET", path, max_retries=1, timeout_seconds=15
            )
            if status == 200:
                _LOGGER.info("Tesla capability '%s' supported (200)", name)
                return True
            if status in (400, 404, 405, 501):
                _LOGGER.info("Tesla capability '%s' unsupported (%d)", name, status)
                return False
            _LOGGER.info(
                "Tesla capability '%s' probe inconclusive (%d) — assuming supported",
                name,
                status,
            )
            return True

        # Run probes sequentially to be gentle on Tesla rate limits.
        base = f"/api/1/energy_sites/{self.site_id}"
        self.tesla_capabilities["storm_mode"] = await _probe(
            "storm_mode",
            f"{base}/storm_mode",
        )
        self.tesla_capabilities["off_grid_vehicle_charging_reserve"] = await _probe(
            "off_grid_vehicle_charging_reserve",
            f"{base}/off_grid_vehicle_charging_reserve",
        )
        # VPP programs endpoint returns the list of programs the site is eligible for.
        # An empty list still means the endpoint is supported (just no programs).
        status, body = await self._tesla_api_call(
            "GET",
            f"{base}/programs",
            max_retries=1,
            timeout_seconds=15,
        )
        if status == 200:
            programs = []
            if isinstance(body, dict):
                resp = body.get("response", body)
                if isinstance(resp, dict):
                    programs = (
                        resp.get("programs") or resp.get("enrolled_programs") or []
                    )
                elif isinstance(resp, list):
                    programs = resp
            self._vpp_programs_cache = programs if isinstance(programs, list) else []
            self.tesla_capabilities["vpp_programs"] = True
            _LOGGER.info(
                "Tesla capability 'vpp_programs' supported — %d programs available",
                len(self._vpp_programs_cache),
            )
        elif status in (400, 404, 405, 501):
            self.tesla_capabilities["vpp_programs"] = False
            _LOGGER.info("Tesla capability 'vpp_programs' unsupported (%d)", status)
        else:
            self.tesla_capabilities["vpp_programs"] = True
            _LOGGER.info(
                "Tesla capability 'vpp_programs' probe inconclusive (%d) — assuming supported",
                status,
            )

        # Notify platforms so entities can be (re)created now that capabilities are known.
        # The probe can complete before async_setup_entry publishes its full
        # hass.data entry, so create the per-entry dict instead of writing to a
        # throwaway default.
        entry_data = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self._entry_id, {}
        )
        entry_data["tesla_capabilities"] = dict(self.tesla_capabilities)
        entry_data["tesla_site_country"] = self._site_country

        # Prune orphaned entities from prior sessions where a capability was
        # supported at the time but is no longer. Without this, the entity
        # registry keeps stale unique_ids which HA displays as "unavailable"
        # and the dashboard strategy will surface them as broken controls.
        self._cleanup_unsupported_tesla_entities()

    def _cleanup_unsupported_tesla_entities(self) -> None:
        """Remove registry entries for Tesla capabilities that the current
        site does not support. Called after every capability probe so that
        upgrading from a version where a capability was incorrectly detected
        (or switching sites) cleans up the orphans automatically."""
        try:
            from homeassistant.helpers import entity_registry as er
        except Exception:
            return
        try:
            ent_reg = er.async_get(self.hass)
        except Exception:
            return

        removed = 0

        def _remove_by_unique_id(domain: str, unique_id: str) -> None:
            nonlocal removed
            eid = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
            if eid:
                try:
                    ent_reg.async_remove(eid)
                    removed += 1
                    _LOGGER.debug("Removed orphaned Tesla entity %s", eid)
                except Exception as err:
                    _LOGGER.debug("Failed to remove %s: %s", eid, err)

        if self.tesla_capabilities.get("storm_mode") is False:
            _remove_by_unique_id("switch", f"{self._entry_id}_tesla_storm_watch")
            _remove_by_unique_id(
                "binary_sensor", f"{self._entry_id}_tesla_storm_watch_active"
            )

        if self.tesla_capabilities.get("off_grid_vehicle_charging_reserve") is False:
            _remove_by_unique_id(
                "number", f"{self._entry_id}_tesla_off_grid_ev_reserve"
            )

        if self.tesla_capabilities.get("vpp_programs") is False:
            # Remove every vpp_* switch created under this entry
            try:
                for reg_entry in list(ent_reg.entities.values()):
                    if (
                        reg_entry.config_entry_id == self._entry_id
                        and reg_entry.domain == "switch"
                        and reg_entry.platform == DOMAIN
                        and "_tesla_vpp_" in (reg_entry.unique_id or "")
                    ):
                        ent_reg.async_remove(reg_entry.entity_id)
                        removed += 1
                        _LOGGER.debug(
                            "Removed orphaned VPP switch %s", reg_entry.entity_id
                        )
            except Exception as err:
                _LOGGER.debug("Failed to scan VPP switches: %s", err)

        if removed > 0:
            _LOGGER.info(
                "Cleaned up %d orphaned Tesla capability entities (site no longer supports them)",
                removed,
            )

    # ------------------------------------------------------------------
    # New Energy Site controls (storm mode, off-grid EV reserve, VPP programs)
    # ------------------------------------------------------------------

    async def async_set_storm_watch(self, enabled: bool) -> bool:
        """Enable or disable Tesla Storm Watch (predictive pre-charging)."""
        path = f"/api/1/energy_sites/{self.site_id}/storm_mode"
        status, _body = await self._tesla_api_call(
            "POST",
            path,
            json_body={"enabled": bool(enabled)},
        )
        if status == 200:
            self._storm_mode_enabled = bool(enabled)
            self.invalidate_site_info_cache()
            _LOGGER.info(
                "Storm Watch %s for site %s",
                "enabled" if enabled else "disabled",
                self.site_id,
            )
            return True
        _LOGGER.error(
            "Failed to set storm mode for site %s: HTTP %s", self.site_id, status
        )
        return False

    async def async_get_storm_watch_status(self) -> dict | None:
        """Fetch current storm watch enabled + active state."""
        path = f"/api/1/energy_sites/{self.site_id}/storm_mode"
        status, body = await self._tesla_api_call("GET", path)
        if status != 200 or not isinstance(body, dict):
            return None
        resp = body.get("response", body)
        if not isinstance(resp, dict):
            return None
        if "enabled" in resp:
            self._storm_mode_enabled = bool(resp.get("enabled"))
        return resp

    async def async_set_off_grid_ev_reserve(self, percent: int) -> bool:
        """Set off-grid vehicle charging reserve percent (0-100)."""
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            _LOGGER.error("Invalid off-grid EV reserve value: %r", percent)
            return False
        percent = max(0, min(100, percent))
        path = f"/api/1/energy_sites/{self.site_id}/off_grid_vehicle_charging_reserve"
        status, _body = await self._tesla_api_call(
            "POST",
            path,
            json_body={"off_grid_vehicle_charging_reserve_percent": percent},
        )
        if status == 200:
            self._off_grid_reserve_percent = percent
            self.invalidate_site_info_cache()
            _LOGGER.info(
                "Off-grid EV reserve set to %d%% for site %s", percent, self.site_id
            )
            return True
        _LOGGER.error(
            "Failed to set off-grid EV reserve for site %s: HTTP %s",
            self.site_id,
            status,
        )
        return False

    async def async_get_vpp_programs(self, force_refresh: bool = False) -> list[dict]:
        """Fetch VPP / grid-services programs the site is eligible for.

        Each program is a dict; Tesla's schema has varied but typically includes
        ``id`` / ``program_id``, ``name``, and an ``enrolled`` / ``is_enrolled``
        flag.
        """
        if self._vpp_programs_cache is not None and not force_refresh:
            return self._vpp_programs_cache
        path = f"/api/1/energy_sites/{self.site_id}/programs"
        status, body = await self._tesla_api_call("GET", path)
        if status != 200 or not isinstance(body, dict):
            return self._vpp_programs_cache or []
        resp = body.get("response", body)
        programs: list[dict] = []
        if isinstance(resp, dict):
            raw = resp.get("programs") or resp.get("enrolled_programs") or []
            if isinstance(raw, list):
                programs = [p for p in raw if isinstance(p, dict)]
        elif isinstance(resp, list):
            programs = [p for p in resp if isinstance(p, dict)]
        self._vpp_programs_cache = programs
        return programs

    async def async_set_vpp_enrollment(self, program_id: str, enrolled: bool) -> bool:
        """Opt in or out of a Tesla VPP / grid-services program."""
        if not program_id:
            _LOGGER.error("Missing program_id for VPP enrollment")
            return False
        path = f"/api/1/energy_sites/{self.site_id}/programs"
        payload = {
            "program_id": program_id,
            "enrolled": bool(enrolled),
        }
        status, _body = await self._tesla_api_call("POST", path, json_body=payload)
        if status == 200:
            # Invalidate caches so next reads pick up new state.
            self._vpp_programs_cache = None
            self.invalidate_site_info_cache()
            _LOGGER.info(
                "VPP program %s %s for site %s",
                program_id,
                "enrolled" if enrolled else "unenrolled",
                self.site_id,
            )
            return True
        _LOGGER.error(
            "Failed to set VPP enrollment for site %s program %s: HTTP %s",
            self.site_id,
            program_id,
            status,
        )
        return False

    async def async_get_calendar_history(
        self,
        period: str = "day",
        kind: str = "energy",
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch calendar history from Tesla API.

        Args:
            period: 'day', 'week', 'month', 'year', or 'lifetime'
            kind: 'energy' or 'power'
            end_date: Optional end date in YYYY-MM-DD format (defaults to today)

        Returns:
            Calendar history data with time_series array, or None if fetch fails
        """
        current_token = self._get_current_token()
        headers = self._tesla_headers(current_token)

        try:
            # Get site timezone from site_info
            site_info = await self.async_get_site_info()
            timezone = "Australia/Brisbane"  # Default fallback
            if site_info:
                timezone = site_info.get("installation_time_zone", timezone)

            # Calculate end_date in site's timezone
            from datetime import timedelta
            from zoneinfo import ZoneInfo

            user_tz = ZoneInfo(timezone)

            # Use provided end_date or default to now
            if end_date:
                try:
                    reference_date = datetime.strptime(end_date, "%Y-%m-%d").replace(
                        tzinfo=user_tz
                    )
                except ValueError:
                    reference_date = datetime.now(user_tz)
            else:
                reference_date = datetime.now(user_tz)

            end_dt = reference_date.replace(hour=23, minute=59, second=59)
            end_date_iso = end_dt.isoformat()

            _LOGGER.info(
                f"Fetching calendar history for site {self.site_id}: period={period}, kind={kind}, end_date={end_date}"
            )

            params = {
                "kind": kind,
                "period": period,
                "end_date": end_date_iso,
                "time_zone": timezone,
            }

            url = f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/calendar_history"

            async with self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(
                        f"Failed to fetch calendar history: {response.status} - {text}"
                    )
                    return None

                data = await response.json()
                result = data.get("response", {})
                time_series = result.get("time_series", [])

                _LOGGER.info(
                    f"Fetched {len(time_series)} raw records from Tesla for period='{period}'"
                )

                # Tesla API often returns all historical data regardless of period
                # Filter client-side based on requested period and end_date
                if time_series and period in ["day", "week", "month", "year"]:
                    # Calculate cutoff date based on period, relative to reference_date
                    if period == "day":
                        cutoff = reference_date.replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                    elif period == "week":
                        cutoff = (reference_date - timedelta(days=7)).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                    elif period == "month":
                        cutoff = (reference_date - timedelta(days=30)).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                    elif period == "year":
                        cutoff = (reference_date - timedelta(days=365)).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )

                    # End of reference day as upper bound
                    end_of_day = reference_date.replace(
                        hour=23, minute=59, second=59, microsecond=999999
                    )

                    filtered_series = []
                    for entry in time_series:
                        try:
                            ts_str = entry.get("timestamp", "")
                            if ts_str:
                                entry_dt = datetime.fromisoformat(ts_str)
                                if cutoff <= entry_dt <= end_of_day:
                                    filtered_series.append(entry)
                        except (ValueError, TypeError) as e:
                            _LOGGER.warning(
                                f"Failed to parse timestamp: {entry.get('timestamp')}: {e}"
                            )
                            continue

                    _LOGGER.info(
                        f"Filtered calendar history from {len(time_series)} to {len(filtered_series)} records for period='{period}' (cutoff={cutoff.date()}, end={end_of_day.date()})"
                    )
                    time_series = filtered_series

                _LOGGER.info(
                    f"Successfully fetched calendar history: {len(time_series)} records for period='{period}'"
                )

                return {
                    "period": period,
                    "time_series": time_series,
                    "serial_number": result.get("serial_number"),
                    "installation_date": result.get("installation_date"),
                }

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout fetching calendar history")
            return None
        except Exception as err:
            _LOGGER.error(f"Error fetching calendar history: {err}")
            return None

    async def async_refresh_lifetime_totals(self) -> dict[str, float] | None:
        """Sum calendar_history period=lifetime into a small dict of kWh totals.

        Tesla returns Wh per bucket (yearly bins from install date). Result is
        cached in ``self._lifetime_totals`` so sensors return the last good value
        between refreshes; on permanent failure (e.g. unsupported endpoint),
        ``_lifetime_fetch_failed`` short-circuits subsequent calls.
        """
        history = await self.async_get_calendar_history(period="lifetime")
        if not history:
            return self._lifetime_totals

        totals = {key: 0.0 for key in LIFETIME_TOTAL_KEYS}
        for ts in history.get("time_series", []) or []:
            totals["lifetime_solar_kwh"] += ts.get("solar_energy_exported") or 0
            totals["lifetime_grid_import_kwh"] += ts.get("grid_energy_imported") or 0
            totals["lifetime_grid_export_kwh"] += (
                ts.get("grid_energy_exported_from_solar") or 0
            ) + (ts.get("grid_energy_exported_from_battery") or 0)
            totals["lifetime_battery_charged_kwh"] += (
                ts.get("battery_energy_imported_from_grid") or 0
            ) + (ts.get("battery_energy_imported_from_solar") or 0)
            totals["lifetime_battery_discharged_kwh"] += (
                ts.get("battery_energy_exported") or 0
            )
            totals["lifetime_home_kwh"] += (
                (ts.get("consumer_energy_imported_from_grid") or 0)
                + (ts.get("consumer_energy_imported_from_solar") or 0)
                + (ts.get("consumer_energy_imported_from_battery") or 0)
            )

        # Tesla returns Wh; convert to kWh
        for key, value in totals.items():
            totals[key] = round(value / 1000.0, 3)

        totals = self._clamp_lifetime_totals(totals)
        self._lifetime_totals = totals
        self._lifetime_last_fetch = time.monotonic()
        await self.async_flush_lifetime_totals()
        return totals

class NativeBatteryIntegrationReadinessMixin:
    """Shared startup guard for battery paths owned by another HA integration."""

    uses_native_battery_integration = True

    def _native_integration_enabled(self) -> bool:
        """Return whether this coordinator is using an upstream HA integration."""
        return True

    def _native_live_telemetry_ready(self) -> bool:
        """Read the upstream entity surface again immediately before a write."""
        controller = getattr(self, "_controller", None)
        checker = getattr(controller, "telemetry_ready", None)
        return bool(checker()) if callable(checker) else True

    def _native_control_surface_ready(self) -> bool:
        """Return whether command entities are available, when separately known."""
        return True

    def startup_control_ready(self) -> bool:
        """Return True only after live native telemetry has completed startup."""
        if not self._native_integration_enabled():
            return True
        data = getattr(self, "data", None)
        if not isinstance(data, dict) or data.get("telemetry_ready") is not True:
            return False
        try:
            return (
                self._native_live_telemetry_ready()
                and self._native_control_surface_ready()
            )
        except Exception as exc:
            _LOGGER.debug(
                "Native battery readiness check is still unavailable: %s",
                exc,
            )
            return False

    def _native_control_allowed(self, operation: str) -> bool:
        """Refuse a write while the upstream integration is still restoring."""
        if self.startup_control_ready():
            return True
        _LOGGER.warning(
            "%s deferred — native Home Assistant battery integration "
            "telemetry/control is not ready",
            operation,
        )
        return False

    def _native_stale_data(self) -> dict[str, Any] | None:
        """Return stale display data marked unsafe for optimization/control."""
        data = getattr(self, "data", None)
        if not isinstance(data, dict):
            return None
        stale = dict(data)
        stale["telemetry_ready"] = False
        if "grid_power_valid" in stale:
            stale["grid_power_valid"] = False
        return stale

class DiscoveredEntityEnergyCoordinator(
    NativeBatteryIntegrationReadinessMixin,
):
    """Monitoring-only normalized telemetry from a selected HA integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        source_entities: dict[str, str],
        entry_id: str,
        *,
        profile_id: str,
        power_multipliers: dict[str, float] | None = None,
    ) -> None:
        self.profile_id = profile_id
        super().__init__(
            hass,
            source_entities,
            entry_id,
            power_multipliers=power_multipliers,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        data = await super()._async_update_data()
        data["telemetry_ready"] = True
        data["connection_profile_id"] = self.profile_id
        data["monitoring_only"] = True
        return data

    def _unsupported_control(self, operation: str) -> bool:
        _LOGGER.warning(
            "%s is unavailable for monitoring-only battery profile %s",
            operation,
            self.profile_id,
        )
        return False

    async def force_charge(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("force_charge")

    async def force_discharge(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("force_discharge")

    async def restore_normal(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("restore_normal")

    async def set_backup_reserve(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("set_backup_reserve")

    async def set_backup_mode(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("set_backup_mode")

    async def restore_work_mode_from_idle(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("restore_work_mode_from_idle")

    async def set_work_mode(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("set_work_mode")

    async def set_charge_rate_limit(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("set_charge_rate_limit")

    async def set_discharge_rate_limit(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("set_discharge_rate_limit")

    async def curtail(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("curtail")

    async def restore_curtailment(self, *args: Any, **kwargs: Any) -> bool:
        return self._unsupported_control("restore_curtailment")

    async def async_shutdown(self) -> None:
        await self._energy_acc.async_flush()

    def async_start_teslemetry_stream(self) -> None:
        """Compatibility no-op for Tesla monitoring profiles."""
