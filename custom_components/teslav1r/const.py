"""Constants for the Teslav1r integration."""
import json
from datetime import timedelta
from pathlib import Path

# Integration domain
DOMAIN = "tesla_v1r"

# Version from manifest.json (single source of truth)
_MANIFEST_PATH = Path(__file__).parent / "manifest.json"
try:
    with open(_MANIFEST_PATH) as f:
        _manifest = json.load(f)
    TESLA_V1R_VERSION = _manifest.get("version", "0.0.0")
except (FileNotFoundError, json.JSONDecodeError):
    TESLA_V1R_VERSION = "0.0.0"

# User-Agent for API identification
TESLA_V1R_USER_AGENT = f"Teslav1r/{TESLA_V1R_VERSION} HomeAssistant"

# Startup waits for external services should be bounded so HA startup is not
# held at wrap-up for minutes when an API cannot publish initial state.
TESLA_CAPABILITY_WAIT_SECONDS = 30.0

# Configuration keys
CONF_DISPLAY_CURRENCY = "display_currency"
DISPLAY_CURRENCY_AUTOMATIC = "automatic"
DISPLAY_CURRENCIES = (
    DISPLAY_CURRENCY_AUTOMATIC,
    "AUD",
    "EUR",
    "GBP",
    "NZD",
    "SEK",
)
CONF_TESLA_ENERGY_SITE_ID = "tesla_energy_site_id"
CONF_AUTO_SYNC_ENABLED = "auto_sync_enabled"
CONF_AUTO_UPDATE_ENABLED = "auto_update_enabled"
CONF_AUTO_UPDATE_TIME = "auto_update_time"
DEFAULT_AUTO_UPDATE_TIME = "03:00"
CONF_TIMEZONE = "timezone"

# Automations - OpenWeatherMap API for weather triggers

# Tesla EV API Provider selection (v2.10.1+).
# Selects which Tesla cloud API is used for vehicle commands when the energy
# site provider is PowerSync.cc (which has no vehicle endpoints). Independent
# from CONF_TESLA_API_PROVIDER, which controls energy site calls only.
CONF_TESLA_EV_API_PROVIDER = "tesla_ev_api_provider"
TESLA_EV_API_PROVIDER_NONE = "none"
TESLA_EV_API_PROVIDER_FLEET_API = "tesla_fleet"

TESLA_EV_API_PROVIDERS = {
    TESLA_EV_API_PROVIDER_NONE: "None (no Tesla cloud vehicle commands)",
    TESLA_EV_API_PROVIDER_FLEET_API: "Tesla Fleet API",
}

# Battery System Selection
CONF_BATTERY_SYSTEM = "battery_system"
CONF_BATTERY_CONNECTION_PROFILE = "battery_connection_profile"
CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID = "battery_integration_config_entry_id"
CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY = "battery_integration_anchor_entity"
CONF_BATTERY_SENSOR_DISPLAY_MODE = "battery_sensor_display_mode"
BATTERY_SENSOR_DISPLAY_RECOMMENDED = "recommended"
BATTERY_SENSOR_DISPLAY_ALL = "all"
BATTERY_SENSOR_DISPLAY_OFF = "off"
BATTERY_SENSOR_DISPLAY_MODES = {
    BATTERY_SENSOR_DISPLAY_RECOMMENDED: "Recommended sensors",
    BATTERY_SENSOR_DISPLAY_ALL: "All supported sensors",
    BATTERY_SENSOR_DISPLAY_OFF: "Off",
}
BATTERY_SYSTEM_TESLA = "tesla"
BATTERY_SYSTEM_CUSTOM = "custom"

BATTERY_SYSTEMS = {
    BATTERY_SYSTEM_TESLA: "Tesla Powerwall — Fleet API or Teslemetry",
    BATTERY_SYSTEM_CUSTOM: "Custom / external controller — planner only via Home Assistant entities",
}

CONF_CUSTOM_BATTERY_LEVEL_ENTITY = "custom_battery_level_entity"
CONF_CUSTOM_BATTERY_POWER_ENTITY = "custom_battery_power_entity"
CONF_CUSTOM_GRID_POWER_ENTITY = "custom_grid_power_entity"
CONF_CUSTOM_SOLAR_POWER_ENTITY = "custom_solar_power_entity"
CONF_CUSTOM_LOAD_POWER_ENTITY = "custom_load_power_entity"


# Tesla API Provider selection
CONF_TESLA_API_PROVIDER = "tesla_api_provider"
TESLA_PROVIDER_FLEET_API = "fleet_api"

# All supported Tesla/EV integrations (for device/entity discovery)
# These are the HA integration domain names used in device identifiers
TESLA_INTEGRATIONS = [
    "tesla_fleet",    # Official Tesla Fleet API integration
    "tesla_custom",   # Tesla Custom Integration
    "tesla",          # Older Tesla integration
]


# Fleet API configuration (direct Tesla API)
CONF_FLEET_API_ACCESS_TOKEN = "fleet_api_access_token"
CONF_FLEET_API_REFRESH_TOKEN = "fleet_api_refresh_token"
CONF_FLEET_API_TOKEN_EXPIRES_AT = "fleet_api_token_expires_at"
CONF_FLEET_API_BASE_URL = "fleet_api_base_url"
CONF_FLEET_API_CLIENT_ID = "fleet_api_client_id"
CONF_FLEET_API_CLIENT_SECRET = "fleet_api_client_secret"

# Powerwall local control (LAN / TEDAPI v1r)
# Set only after the pairing flow completes. Stored in entry.data so HA
# encrypts the private key at rest. The IP and customer password are
# mirrored from the mobile app so local monitoring works device-independently.
CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
CONF_POWERWALL_LOCAL_PRIVATE_KEY = "powerwall_local_private_key_pem"
CONF_POWERWALL_LOCAL_PUBLIC_KEY = "powerwall_local_public_key_der"
CONF_POWERWALL_LOCAL_DIN = "powerwall_local_din"
CONF_POWERWALL_LOCAL_IP = "powerwall_local_ip"
CONF_POWERWALL_LOCAL_VERSION = "powerwall_local_version"  # "pw2" | "pw3"
# DEPRECATED — kept only so HA doesn't choke on legacy entry.data values
# carried forward from versions <= 2.12.247. The integration uses RSA-signed
# /tedapi/v1r exclusively now; never written, never read at runtime.
CONF_POWERWALL_LOCAL_CUSTOMER_PASSWORD = "powerwall_local_customer_password"
CONF_POWERWALL_LOCAL_WIFI_SSID = "powerwall_local_wifi_ssid"
CONF_POWERWALL_LOCAL_WIFI_PASSWORD = "powerwall_local_wifi_password"
CONF_POWERWALL_LOCAL_ENERGY_SITE_ID = "powerwall_local_energy_site_id"
CONF_POWERWALL_LOCAL_PAIRED_AT = "powerwall_local_paired_at"
# Minimum battery SOC (%) below which off-grid commands are refused.
CONF_POWERWALL_OFF_GRID_MIN_SOC = "powerwall_off_grid_min_soc"
DEFAULT_POWERWALL_OFF_GRID_MIN_SOC = 20
# Local poll interval for meters/SOC/grid_status when paired. Gateway samples
# at ~1 Hz natively; 2s gives near-real-time updates with a small margin.
POWERWALL_LOCAL_POLL_INTERVAL = 2  # seconds
# Pairing window the user has to toggle the Powerwall switch.
POWERWALL_PAIRING_WINDOW_SECONDS = 120

# AEMO region options (NEM regions)
AEMO_REGIONS = {
    "NSW1": "NSW - New South Wales",
    "QLD1": "QLD - Queensland",
    "VIC1": "VIC - Victoria",
    "SA1": "SA - South Australia",
    "TAS1": "TAS - Tasmania",
}

FLOW_POWER_KWATCH_REGIONS = {
    "NSW1": "nsw",
    "VIC1": "vic",
    "QLD1": "qld",
    "SA1": "sa",
    "TAS1": "tas",
}

# Data coordinator update intervals
UPDATE_INTERVAL_PRICES = timedelta(minutes=5)  # Amber updates every 5 minutes
UPDATE_INTERVAL_ENERGY = timedelta(seconds=15)  # Tesla energy data every 15 seconds
TESLA_SITE_INFO_CACHE_TTL_SECONDS = 6 * 60 * 60
TESLA_SITE_INFO_CONTROL_MAX_AGE_SECONDS = 60
# How recently the local Powerwall coordinator must have ticked for its data
# to be trusted by number.py/select.py/sensor.py's local-prefer overrides and
# optimization/battery_controller.py's local snapshot lookup.
TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS = 30


# Tesla Fleet API (direct)
FLEET_API_BASE_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"
FLEET_API_AUTH_URL = "https://auth.tesla.com/oauth2/v3"
FLEET_API_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"

def get_tesla_api_base_url(
    provider: str | None, fleet_base_url: str | None = None
) -> str:
    """Return the Tesla API base URL for a given provider.

    Used by all Tesla service handlers to construct API request URLs.
    All three providers expose the same /api/1/... path structure, only
    the base differs.

    fleet_base_url overrides FLEET_API_BASE_URL for Fleet API provider — pass
    entry.data.get(CONF_FLEET_API_BASE_URL) to support EU/AP regional endpoints.
    """
    if provider == TESLA_PROVIDER_FLEET_API:
        return fleet_base_url or FLEET_API_BASE_URL


# Sensor types
SENSOR_TYPE_CURRENT_PRICE = "current_price"  # Legacy - kept for compatibility
SENSOR_TYPE_CURRENT_IMPORT_PRICE = "current_import_price"
SENSOR_TYPE_CURRENT_EXPORT_PRICE = "current_export_price"
SENSOR_TYPE_FORECAST_PRICE = "forecast_price"
SENSOR_TYPE_SOLAR_POWER = "solar_power"
SENSOR_TYPE_GRID_POWER = "grid_power"
SENSOR_TYPE_GRID_STATUS = "grid_status"
SENSOR_TYPE_BATTERY_POWER = "battery_power"
SENSOR_TYPE_HOME_LOAD = "home_load"
SENSOR_TYPE_BATTERY_LEVEL = "battery_level"
# Battery BMS-reported power limits (kW) — used by force-mode defaults and mobile sliders
SENSOR_TYPE_BATTERY_MAX_CHARGE_POWER = "battery_max_charge_power"
SENSOR_TYPE_BATTERY_MAX_DISCHARGE_POWER = "battery_max_discharge_power"

# Switch types
SWITCH_TYPE_AUTO_SYNC = "auto_sync"
SWITCH_TYPE_FORCE_DISCHARGE = "force_discharge"
SWITCH_TYPE_FORCE_CHARGE = "force_charge"
SWITCH_TYPE_MONITORING_MODE = "monitoring_mode"
SWITCH_TYPE_AWAY_MODE = "away_mode"
SWITCH_TYPE_PROFIT_MAX_MODE = "profit_max_mode"
SWITCH_TYPE_COST_NEUTRAL = "cost_neutral"
SWITCH_TYPE_CHARGE_BY_TIME = "charge_by_time"
SWITCH_TYPE_OPTIMIZATION_DISABLE_IDLE = "optimization_disable_idle"
SWITCH_TYPE_OPTIMIZATION_SPREAD_EXPORT = "optimization_spread_export"
SWITCH_TYPE_OPTIMIZATION_SPREAD_IMPORT = "optimization_spread_import"
SWITCH_TYPE_OPTIMIZATION_ENABLED = "optimization_enabled"
SWITCH_TYPE_OPTIMIZATION_AUTO_APPLY_RESERVE = "optimization_auto_apply_reserve"
SWITCH_TYPE_AUTO_UPDATE = "auto_update"

# Battery mode sensor (for automation triggers)
SENSOR_TYPE_BATTERY_MODE = "battery_mode"

# Battery mode states
BATTERY_MODE_STATE_NORMAL = "normal"
BATTERY_MODE_STATE_FORCE_CHARGE = "force_charge"
BATTERY_MODE_STATE_FORCE_DISCHARGE = "force_discharge"
BATTERY_MODE_STATE_HOLD_SOC = "hold_soc"
BATTERY_MODE_STATE_SELF_CONSUMPTION = "self_consumption"

# Services for manual battery control
SERVICE_FORCE_DISCHARGE = "force_discharge"
SERVICE_FORCE_CHARGE = "force_charge"
SERVICE_HOLD_BATTERY_SOC = "hold_battery_soc"
SERVICE_RESTORE_NORMAL = "restore_normal"
SERVICE_GET_CALENDAR_HISTORY = "get_calendar_history"
SERVICE_SYNC_BATTERY_HEALTH = "sync_battery_health"
SERVICE_PREVIEW_HISTORY_RELINK = "preview_history_relink"
SERVICE_APPLY_HISTORY_RELINK = "apply_history_relink"
SERVICE_SET_BACKUP_RESERVE = "set_backup_reserve"
SERVICE_SET_OPERATION_MODE = "set_operation_mode"
SERVICE_SET_GRID_EXPORT = "set_grid_export"
SERVICE_SET_GRID_CHARGING = "set_grid_charging"
SERVICE_CURTAIL_INVERTER = "curtail_inverter"
SERVICE_RESTORE_INVERTER = "restore_inverter"


# Manual discharge/charge duration options (minutes)
DISCHARGE_DURATIONS = [
    5,
    10,
    15,
    30,
    45,
    60,
    75,
    90,
    105,
    120,
    135,
    150,
    165,
    180,
    195,
    210,
    225,
    240,
]
DEFAULT_DISCHARGE_DURATION = 30

# Duration dropdown entity option keys (stored in ConfigEntry.options)
CONF_FORCE_CHARGE_DURATION = "force_charge_duration"
CONF_FORCE_DISCHARGE_DURATION = "force_discharge_duration"

# Battery health sensor (from mobile app TEDAPI scans)
SENSOR_TYPE_BATTERY_HEALTH = "battery_health"
SENSOR_TYPE_FIRMWARE = "firmware"

# Tesla Powerwall extended sensors (cloud)
SENSOR_TYPE_LIFETIME_SOLAR = "lifetime_solar_energy"
SENSOR_TYPE_LIFETIME_GRID_IMPORT = "lifetime_grid_import"
SENSOR_TYPE_LIFETIME_GRID_EXPORT = "lifetime_grid_export"
SENSOR_TYPE_LIFETIME_BATTERY_CHARGED = "lifetime_battery_charged"
SENSOR_TYPE_LIFETIME_BATTERY_DISCHARGED = "lifetime_battery_discharged"
SENSOR_TYPE_LIFETIME_HOME_CONSUMPTION = "lifetime_home_consumption"
SENSOR_TYPE_BACKUP_TIME_REMAINING = "backup_time_remaining"
SENSOR_TYPE_TOTAL_PACK_ENERGY = "total_pack_energy"
SENSOR_TYPE_ENERGY_LEFT = "energy_left"
SENSOR_TYPE_GRID_SERVICES_POWER = "grid_services_power"

# Tesla Powerwall local TEDAPI sensors (gated on CONF_POWERWALL_LOCAL_PAIRED)
SENSOR_TYPE_PW_SYSTEM_ISLAND_STATE = "pw_system_island_state"
SENSOR_TYPE_PW_COUNT = "pw_count"
SENSOR_TYPE_PW_ACTIVE_ALERTS = "pw_active_alerts"
SENSOR_TYPE_PW_BLOCK_SOC = "pw_block_soc"  # per-block (key gets index suffix)
SENSOR_TYPE_PW_BLOCK_CAPACITY = "pw_block_capacity"
SENSOR_TYPE_PW_BLOCK_VOLTAGE = "pw_block_voltage"
SENSOR_TYPE_PW_BLOCK_TEMPERATURE = "pw_block_temperature"
SENSOR_TYPE_PW_BLOCK_SOH = "pw_block_soh"


# Map battery system to native optimization name
OPTIMIZATION_PROVIDER_NATIVE_NAMES = {
    BATTERY_SYSTEM_TESLA: "Tesla Powerwall",
    BATTERY_SYSTEM_CUSTOM: "Custom / external controller",
}




def normalize_grid_charge_blackout_windows(value) -> list[dict[str, str]]:
    """Return canonical local-time grid-charge blackout windows.

    The persisted form is deliberately small and timezone-agnostic: schedule
    timestamps are converted to the site's local time when the policy is
    evaluated.  A JSON list is accepted by the config form/API, while a
    comma-separated ``HH:MM-HH:MM`` form remains convenient for text clients.
    """
    if value in (None, "", []):
        return []
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        raise ValueError("blackout windows must be a list")

    canonical: set[tuple[str, str]] = set()
    for item in raw:
        if isinstance(item, str) and "-" in item:
            start, end = (part.strip() for part in item.split("-", 1))
        elif isinstance(item, dict):
            start, end = item.get("start"), item.get("end")
        else:
            raise ValueError("blackout windows must contain start/end ranges")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("blackout times must be strings")
        for clock in (start, end):
            if len(clock) != 5 or clock[2] != ":" or not (
                clock[:2].isdigit() and clock[3:].isdigit()
            ):
                raise ValueError("blackout times must use HH:MM")
            hour, minute = int(clock[:2]), int(clock[3:])
            if hour > 23 or minute > 59:
                raise ValueError("blackout times must use a 24-hour clock")
        if start == end:
            raise ValueError("blackout windows cannot be zero length")
        canonical.add((start, end))
    return [{"start": start, "end": end} for start, end in sorted(canonical)]


# Optimization cost function (only cost minimization — self-consumption is the battery's native mode)
COST_FUNCTION_COST = "cost"

# Default optimization settings
DEFAULT_OPTIMIZATION_INTERVAL = 5      # Re-optimize every 5 minutes
DEFAULT_OPTIMIZATION_HORIZON = 48      # 48-hour forecast horizon
DEFAULT_OPTIMIZATION_BACKUP_RESERVE = 0.20  # 20% minimum SOC
DEFAULT_OPTIMIZATION_MIN_EXPORT_PRICE = 0.0  # $/kWh; 0 preserves current behavior
DEFAULT_OPTIMIZATION_BACKUP_ENERGY_WH = 0  # Disabled until explicitly configured
DEFAULT_OPTIMIZATION_BACKUP_ENERGY_MAX_POWER_W = 3600
DEFAULT_OPTIMIZATION_BACKUP_ENERGY_START = "18:00"
DEFAULT_OPTIMIZATION_BACKUP_ENERGY_END = "06:00"
DEFAULT_CHARGE_BY_TIME_TARGET_TIME = "17:15"
DEFAULT_CHARGE_BY_TIME_TARGET_SOC = 1.0
DEFAULT_PROFIT_MAX_TARGET_TIME = DEFAULT_CHARGE_BY_TIME_TARGET_TIME
DEFAULT_PROFIT_MAX_TARGET_SOC = DEFAULT_CHARGE_BY_TIME_TARGET_SOC

# Battery capacity defaults by system (Wh)
BATTERY_CAPACITY_DEFAULTS = {
    BATTERY_SYSTEM_TESLA: 13500,     # Powerwall 2: 13.5 kWh
    BATTERY_SYSTEM_CUSTOM: 10000,     # User-provided external system
}

# Max charge/discharge power defaults by system (W)
BATTERY_POWER_DEFAULTS = {
    BATTERY_SYSTEM_TESLA: 5000,       # Powerwall 2: 5 kW continuous
    BATTERY_SYSTEM_CUSTOM: 5000,       # User-provided external system
}

# Optimization sensor types
SENSOR_TYPE_OPTIMIZATION_STATUS = "optimization_status"
SENSOR_TYPE_OPTIMIZATION_SAVINGS = "optimization_savings"
SENSOR_TYPE_OPTIMIZATION_NEXT_ACTION = "optimization_next_action"
SENSOR_TYPE_OPTIMIZATION_FORCE_CHARGE_WINDOWS = "optimization_force_charge_windows"
SENSOR_TYPE_OPTIMIZATION_FORCE_DISCHARGE_WINDOWS = "optimization_force_discharge_windows"


# ============================================================
# Device Family Grouping
# Each family maps to a HA sub-device linked via via_device to the parent
# entry device, so sensors appear in logical groups rather than one flat list.
# ============================================================
SENSOR_FAMILY_LP_OPTIMIZER = "lp_optimizer"
SENSOR_FAMILY_BATTERY = "battery"
SENSOR_FAMILY_SOLAR_INVERTER = "solar_inverter"
SENSOR_FAMILY_GRID_HOME = "grid_home"
SENSOR_FAMILY_PRICING = "pricing"
SENSOR_FAMILY_FLOW_POWER = "flow_power"
SENSOR_FAMILY_GLOBIRD = "globird"
SENSOR_FAMILY_AEMO = "aemo"
SENSOR_FAMILY_EV_CHARGING = "ev_charging"
SENSOR_FAMILY_OCTOPUS = "octopus"
SENSOR_FAMILY_CONTROLS = "controls"

FAMILY_DISPLAY_NAMES: dict[str, str] = {
    SENSOR_FAMILY_LP_OPTIMIZER: "LP Optimizer",
    SENSOR_FAMILY_BATTERY: "Battery",
    SENSOR_FAMILY_SOLAR_INVERTER: "Solar & Inverter",
    SENSOR_FAMILY_GRID_HOME: "Grid & Home",
    SENSOR_FAMILY_PRICING: "Pricing & Cost",
    SENSOR_FAMILY_FLOW_POWER: "Flow Power",
    SENSOR_FAMILY_GLOBIRD: "GloBird",
    SENSOR_FAMILY_AEMO: "AEMO",
    SENSOR_FAMILY_EV_CHARGING: "EV Charging",
    SENSOR_FAMILY_OCTOPUS: "Octopus",
    SENSOR_FAMILY_CONTROLS: "Controls",
}

SENSOR_KEY_TO_FAMILY: dict[str, str] = {
    # LP Optimizer
    "optimization_status": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_next_action": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_force_charge_windows": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_force_discharge_windows": SENSOR_FAMILY_LP_OPTIMIZER,
    "optimization_savings": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_solar_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_load_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_battery_power_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_import_price_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "lp_export_price_forecast": SENSOR_FAMILY_LP_OPTIMIZER,
    "load_forecast_today_remaining": SENSOR_FAMILY_LP_OPTIMIZER,
    "load_forecast_tomorrow": SENSOR_FAMILY_LP_OPTIMIZER,
    "tariff_schedule": SENSOR_FAMILY_LP_OPTIMIZER,
    # Battery
    "battery_power": SENSOR_FAMILY_BATTERY,
    "battery_level": SENSOR_FAMILY_BATTERY,
    "battery_level_1": SENSOR_FAMILY_BATTERY,
    "battery_level_2": SENSOR_FAMILY_BATTERY,
    "battery_max_charge_power": SENSOR_FAMILY_BATTERY,
    "battery_max_discharge_power": SENSOR_FAMILY_BATTERY,
    "battery_health": SENSOR_FAMILY_BATTERY,
    "battery_mode": SENSOR_FAMILY_BATTERY,
    "min_soc": SENSOR_FAMILY_BATTERY,
    "daily_battery_charge": SENSOR_FAMILY_BATTERY,
    "daily_battery_discharge": SENSOR_FAMILY_BATTERY,
    "daily_battery_charge_foxess": SENSOR_FAMILY_BATTERY,
    "daily_battery_discharge_foxess": SENSOR_FAMILY_BATTERY,
    # Solar & Inverter
    "solar_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "daily_solar_energy": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv1_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv2_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv3_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv4_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv5_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv6_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv1_voltage": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv2_voltage": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv3_voltage": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv1_current": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv2_current": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv3_current": SENSOR_FAMILY_SOLAR_INVERTER,
    "ct2_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv_dc_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "pv_ac_power": SENSOR_FAMILY_SOLAR_INVERTER,
    "work_mode": SENSOR_FAMILY_SOLAR_INVERTER,
    "firmware": SENSOR_FAMILY_SOLAR_INVERTER,
    "solar_curtailment": SENSOR_FAMILY_SOLAR_INVERTER,
    "inverter_status": SENSOR_FAMILY_SOLAR_INVERTER,
    "solcast_today_forecast": SENSOR_FAMILY_SOLAR_INVERTER,
    "solcast_tomorrow_forecast": SENSOR_FAMILY_SOLAR_INVERTER,
    "solcast_current_estimate": SENSOR_FAMILY_SOLAR_INVERTER,
    # Grid & Home
    "grid_power": SENSOR_FAMILY_GRID_HOME,
    "grid_status": SENSOR_FAMILY_GRID_HOME,
    "home_load": SENSOR_FAMILY_GRID_HOME,
    "daily_grid_import": SENSOR_FAMILY_GRID_HOME,
    "daily_grid_export": SENSOR_FAMILY_GRID_HOME,
    "daily_load": SENSOR_FAMILY_GRID_HOME,
    "grid_import_power": SENSOR_FAMILY_GRID_HOME,
    # Pricing & Cost
    "current_price": SENSOR_FAMILY_PRICING,
    "current_import_price": SENSOR_FAMILY_PRICING,
    "current_export_price": SENSOR_FAMILY_PRICING,
    "forecast_price": SENSOR_FAMILY_PRICING,
    "daily_import_cost": SENSOR_FAMILY_PRICING,
    "daily_export_earnings": SENSOR_FAMILY_PRICING,
    "daily_avg_cost_per_kwh": SENSOR_FAMILY_PRICING,
    "mtd_avg_cost_per_kwh": SENSOR_FAMILY_PRICING,
    "in_demand_charge_period": SENSOR_FAMILY_PRICING,
    "peak_demand_this_cycle": SENSOR_FAMILY_PRICING,
    "demand_charge_cost": SENSOR_FAMILY_PRICING,
    "days_until_demand_reset": SENSOR_FAMILY_PRICING,
    "daily_supply_charge_cost": SENSOR_FAMILY_PRICING,
    "monthly_supply_charge": SENSOR_FAMILY_PRICING,
    "total_monthly_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_yesterday_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_today_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_yesterday_savings": SENSOR_FAMILY_PRICING,
    "amber_usage_month_cost": SENSOR_FAMILY_PRICING,
    "amber_usage_month_savings": SENSOR_FAMILY_PRICING,
    # AEMO
    "aemo_price": SENSOR_FAMILY_AEMO,
    "aemo_spike_status": SENSOR_FAMILY_AEMO,
    # Tesla Powerwall extended (cloud)
    "lifetime_solar_energy": SENSOR_FAMILY_SOLAR_INVERTER,
    "lifetime_grid_import": SENSOR_FAMILY_GRID_HOME,
    "lifetime_grid_export": SENSOR_FAMILY_GRID_HOME,
    "lifetime_battery_charged": SENSOR_FAMILY_BATTERY,
    "lifetime_battery_discharged": SENSOR_FAMILY_BATTERY,
    "lifetime_home_consumption": SENSOR_FAMILY_GRID_HOME,
    "backup_time_remaining": SENSOR_FAMILY_BATTERY,
    "total_pack_energy": SENSOR_FAMILY_BATTERY,
    "energy_left": SENSOR_FAMILY_BATTERY,
    "grid_services_power": SENSOR_FAMILY_GRID_HOME,
    # Powerwall local
    "pw_system_island_state": SENSOR_FAMILY_GRID_HOME,
    "pw_count": SENSOR_FAMILY_BATTERY,
    "pw_active_alerts": SENSOR_FAMILY_BATTERY,
}


def family_device_info(entry_id: str, family: str) -> dict:
    """Return device_info dict pointing all entities at the single parent device."""
    return {
        "identifiers": {(DOMAIN, entry_id)},
    }


def powerwall_device_info(entry_id: str) -> dict:
    """Tesla Powerwall device — sub-device of the main PowerSync entry.

    Holds Powerwall-specific telemetry (lifetime totals, backup time remaining,
    grid services state, alerts) so the HA device tree separates raw Powerwall
    diagnostics from the optimiser's user-facing controls.
    """
    return {
        "identifiers": {(DOMAIN, f"{entry_id}_powerwall")},
        "name": "Tesla Powerwall",
        "manufacturer": "Tesla",
        "model": "Powerwall",
        "via_device": (DOMAIN, entry_id),
    }


def provider_pricing_device_info(entry_id: str, provider: str) -> dict:
    """Provider pricing/account device linked to the main PowerSync hub."""
    provider_key = provider.lower()
    if provider_key == SENSOR_FAMILY_GLOBIRD:
        name = "GloBird Pricing"
        manufacturer = "GloBird Energy"
    elif provider_key == SENSOR_FAMILY_FLOW_POWER:
        name = "Flow Power Pricing"
        manufacturer = "Flow Power"
    else:
        name = f"{provider.title()} Pricing"
        manufacturer = provider.title()

    return {
        "identifiers": {(DOMAIN, f"{entry_id}_{provider_key}_pricing")},
        "name": name,
        "manufacturer": manufacturer,
        "model": "Electricity Pricing",
        "via_device": (DOMAIN, entry_id),
    }


def powerwall_block_device_info(entry_id: str, index: int) -> dict:
    """Per-Powerwall sub-device, used for individual battery-block sensors.

    Each in-service Powerwall gets its own device (Powerwall 1, Powerwall 2, …)
    via the Tesla Powerwall parent so SOC / voltage / temperature / SoH for
    each pack live on a distinct device card in HA.
    """
    return {
        "identifiers": {(DOMAIN, f"{entry_id}_pw_{index + 1}")},
        "name": f"Powerwall {index + 1}",
        "manufacturer": "Tesla",
        "model": "Powerwall Battery",
        "via_device": (DOMAIN, f"{entry_id}_powerwall"),
    }
