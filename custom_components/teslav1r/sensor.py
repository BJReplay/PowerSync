"""Sensor platform for Teslav1r integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .battery_backend.discovery import (
    discover_battery_sensor_catalog,
    discover_canonical_entities,
)
from .const import (
    ATTR_NETWORK_PRICE,
    ATTR_PRICE_SPIKE,
    ATTR_WHOLESALE_PRICE,
    BATTERY_MODE_STATE_FORCE_CHARGE,
    BATTERY_MODE_STATE_FORCE_DISCHARGE,
    BATTERY_MODE_STATE_HOLD_SOC,
    BATTERY_MODE_STATE_NORMAL,
    BATTERY_MODE_STATE_SELF_CONSUMPTION,
    BATTERY_SENSOR_DISPLAY_ALL,
    BATTERY_SENSOR_DISPLAY_RECOMMENDED,
    CONF_BATTERY_SENSOR_DISPLAY_MODE,
    CONF_BATTERY_SYSTEM,
    CONF_POWERWALL_LOCAL_PAIRED,
    DOMAIN,
    SENSOR_FAMILY_BATTERY,
    SENSOR_FAMILY_GRID_HOME,
    SENSOR_KEY_TO_FAMILY,
    SENSOR_TYPE_BACKUP_TIME_REMAINING,
    SENSOR_TYPE_BATTERY_HEALTH,
    SENSOR_TYPE_BATTERY_LEVEL,
    SENSOR_TYPE_BATTERY_MAX_CHARGE_POWER,
    SENSOR_TYPE_BATTERY_MAX_DISCHARGE_POWER,
    SENSOR_TYPE_BATTERY_MODE,
    SENSOR_TYPE_BATTERY_POWER,
    SENSOR_TYPE_CURRENT_EXPORT_PRICE,
    SENSOR_TYPE_CURRENT_IMPORT_PRICE,
    SENSOR_TYPE_DAILY_AVG_COST_PER_KWH,
    SENSOR_TYPE_DAILY_BATTERY_CHARGE,
    SENSOR_TYPE_DAILY_BATTERY_DISCHARGE,
    SENSOR_TYPE_DAILY_EXPORT_EARNINGS,
    SENSOR_TYPE_DAILY_GRID_EXPORT,
    SENSOR_TYPE_DAILY_GRID_IMPORT,
    SENSOR_TYPE_DAILY_IMPORT_COST,
    SENSOR_TYPE_DAILY_LOAD,
    SENSOR_TYPE_DAILY_SOLAR_ENERGY,
    SENSOR_TYPE_ENERGY_LEFT,
    SENSOR_TYPE_FIRMWARE,
    SENSOR_TYPE_GRID_POWER,
    SENSOR_TYPE_GRID_SERVICES_POWER,
    SENSOR_TYPE_GRID_STATUS,
    SENSOR_TYPE_HOME_LOAD,
    SENSOR_TYPE_LIFETIME_BATTERY_CHARGED,
    SENSOR_TYPE_LIFETIME_BATTERY_DISCHARGED,
    SENSOR_TYPE_LIFETIME_GRID_EXPORT,
    SENSOR_TYPE_LIFETIME_GRID_IMPORT,
    SENSOR_TYPE_LIFETIME_HOME_CONSUMPTION,
    SENSOR_TYPE_LIFETIME_SOLAR,
    SENSOR_TYPE_MTD_AVG_COST_PER_KWH,
    SENSOR_TYPE_SOLAR_POWER,
    SENSOR_TYPE_TOTAL_PACK_ENERGY,
    TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS,
    family_device_info,
    powerwall_device_info,
)
from .coordinator import TeslaEnergyCoordinator
from .currency import (
    currency_for_entry,
    major_price_unit,
    minor_price_unit,
    money_unit,
    normalize_currency,
    presentation_currency_metadata_for_entry,
)
from .registry_compat import iter_device_entries
from .tesla_alerts import powerwall_alert_attributes, split_powerwall_alerts

_LOGGER = logging.getLogger(__name__)


def _merge_inverter_status_attributes(
    previous: dict[str, Any],
    current: dict[str, Any],
    current_date: str,
    current_source_id: str | None = None,
) -> dict[str, Any]:
    """Keep a same-day daily yield when a sleeping inverter omits it."""
    merged = dict(current)
    if current_source_id:
        merged["inverter_source_id"] = current_source_id
    if "daily_pv_generation" in merged:
        merged["daily_pv_generation_date"] = current_date
    elif (
        previous.get("daily_pv_generation_date") == current_date
        and previous.get("daily_pv_generation") is not None
        and (
            current_source_id is None
            or previous.get("inverter_source_id") == current_source_id
        )
    ):
        merged["daily_pv_generation"] = previous["daily_pv_generation"]
        merged["daily_pv_generation_date"] = current_date
    return merged


def _restored_inverter_daily_attributes(
    restored: dict[str, Any],
    current_date: str,
    current_source_id: str | None,
) -> dict[str, Any]:
    """Restore only a same-day daily yield, never stale instantaneous power."""
    restored_daily = _merge_inverter_status_attributes(
        restored,
        {},
        current_date,
        current_source_id,
    )
    return restored_daily if "daily_pv_generation" in restored_daily else {}


def _home_load_power_kw(data: Any) -> float | None:
    """Return Home Load in kW, clamped to its physical lower bound."""
    if not data:
        return None
    value = data.get("load_power")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


# Large rolling prediction arrays exposed as sensor attributes (≈48h @ 5min /
# price-period series). They exceed Home Assistant's 16 KB per-state recorder
# attribute cap and are regenerated each cycle (not history), so the recorder
# is told to skip them via Entity._unrecorded_attributes while the scalar state
# is still recorded. Keys cover the LP optimizer, Solcast and Amber forecast
# sensors (different sensors use different key names for their array).
_FORECAST_ARRAY_ATTRS = frozenset(
    {
        "forecast",
        "forecast_values_kw",
        "charge_values_kw",
        "discharge_values_kw",
        "home_consumption_values_kw",
        "export_values_kw",
        "power_values_kw",
        "price_values",
        "hourly_forecast",
        "forecast_periods",
    }
)


@dataclass
class Teslav1rSensorEntityDescription(SensorEntityDescription):
    """Describes Teslav1r sensor entity."""

    value_fn: Callable[[Any], Any] | None = None
    attr_fn: Callable[[Any], dict[str, Any]] | None = None
    # Optional override that pulls the sensor onto a separate HA device.
    # Currently only "powerwall" is recognised — anything else falls back to
    # the default family_device_info routing so existing sensors are unaffected.
    device_section: str | None = None
    # Currency unit kind. "money" is a pure monetary total, "major_rate" is
    # ISO/kWh, "market_rate" is ISO/MWh, and "minor_rate" is p/ct/c per kWh.
    currency_unit: str | None = None
    currency_attrs: bool = False


RATE_CURRENCY_UNITS = {"major_rate", "market_rate", "minor_rate"}
_RESTORED_NUMERIC_SENSOR_KEYS = {
    SENSOR_TYPE_CURRENT_IMPORT_PRICE,
    SENSOR_TYPE_CURRENT_EXPORT_PRICE,
    SENSOR_TYPE_DAILY_IMPORT_COST,
    SENSOR_TYPE_DAILY_EXPORT_EARNINGS,
    SENSOR_TYPE_DAILY_AVG_COST_PER_KWH,
    SENSOR_TYPE_MTD_AVG_COST_PER_KWH,
}

_ENERGY_SUMMARY_VALUE_KEYS = {
    SENSOR_TYPE_DAILY_IMPORT_COST: "import_cost_today",
    SENSOR_TYPE_DAILY_EXPORT_EARNINGS: "export_earnings_today",
    SENSOR_TYPE_DAILY_AVG_COST_PER_KWH: "avg_cost_per_kwh_today",
    SENSOR_TYPE_MTD_AVG_COST_PER_KWH: "avg_cost_per_kwh_mtd",
}


def _restored_numeric_state_value(state: Any) -> float | None:
    """Return a numeric value from a restored HA state, if it is usable."""
    raw = getattr(state, "state", None)
    if raw in (None, "", "unknown", "unavailable"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class RestoredNumericStateMixin(RestoreEntity):
    """Restore the last valid numeric state while startup data is unavailable."""

    _restored_native_value: float | None = None

    async def _async_restore_numeric_state(self) -> None:
        last_state = await self.async_get_last_state()
        self._restored_native_value = _restored_numeric_state_value(last_state)

    def _restored_numeric_value(self, sensor_key: str) -> float | None:
        if sensor_key not in _RESTORED_NUMERIC_SENSOR_KEYS:
            return None
        return self._restored_native_value


def _currency_unit_for_kind(kind: str | None, currency: str) -> str | None:
    """Return a unit string for a Teslav1r currency unit kind."""
    if kind == "money":
        return money_unit(currency)
    if kind == "major_rate":
        return major_price_unit(currency)
    if kind == "market_rate":
        return major_price_unit(currency, "MWh")
    if kind == "minor_rate":
        return minor_price_unit(currency)
    return None


def _entity_currency(entity: Any, tariff_data: dict[str, Any] | None = None) -> str:
    """Return the currency for an entity, optionally preferring tariff metadata."""
    if tariff_data:
        tariff_currency = normalize_currency(tariff_data.get("currency"), "")
        if tariff_currency:
            return tariff_currency
    return currency_for_entry(
        getattr(entity, "_entry", None), getattr(entity, "hass", None)
    )


def _entity_currency_attrs(
    entity: Any,
    attrs: dict[str, Any] | None,
    tariff_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge currency metadata into attributes for currency-aware sensors."""
    base = dict(attrs or {})
    description = getattr(entity, "entity_description", None)
    kind = getattr(description, "currency_unit", None) or getattr(
        entity, "_attr_currency_unit", None
    )
    include = getattr(description, "currency_attrs", False) or getattr(
        entity, "_attr_currency_attrs", False
    )
    if kind and include:
        base.update(
            presentation_currency_metadata_for_entry(
                getattr(entity, "_entry", None),
                _entity_currency(entity, tariff_data),
            )
        )
    return base


class Teslav1rCurrencyMixin:
    """Mixin for dynamic currency units on Teslav1r sensors."""

    def _currency_source_data(self) -> dict[str, Any] | None:
        """Return optional tariff data that should override entry currency."""
        return None

    @property
    def _currency_unit_kind(self) -> str | None:
        description = getattr(self, "entity_description", None)
        return getattr(description, "currency_unit", None) or getattr(
            self, "_attr_currency_unit", None
        )

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return a provider/HA currency-aware unit when requested."""
        unit = _currency_unit_for_kind(
            self._currency_unit_kind,
            _entity_currency(self, self._currency_source_data()),
        )
        if unit:
            return unit
        description = getattr(self, "entity_description", None)
        return getattr(self, "_attr_native_unit_of_measurement", None) or getattr(
            description, "native_unit_of_measurement", None
        )

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Avoid monetary device class for price-rate sensors."""
        if self._currency_unit_kind in RATE_CURRENCY_UNITS:
            return None
        description = getattr(self, "entity_description", None)
        return getattr(self, "_attr_device_class", None) or getattr(
            description, "device_class", None
        )


def _get_import_price(data):
    """Extract import (general) price from Amber data."""
    if not data:
        _LOGGER.debug("_get_import_price: No data available")
        return None
    if not data.get("current"):
        _LOGGER.debug(
            "_get_import_price: No 'current' key in data. Keys: %s",
            list(data.keys()) if isinstance(data, dict) else "not a dict",
        )
        return None
    current_prices = data.get("current", [])
    _LOGGER.debug(
        "_get_import_price: Found %d current price entries", len(current_prices)
    )
    for price in current_prices:
        if price.get("channelType") == "general":
            raw_price = price.get("perKwh", 0)
            converted_price = raw_price / 100
            _LOGGER.debug(
                "_get_import_price: Found general price: %s c/kWh -> %s $/kWh",
                raw_price,
                converted_price,
            )
            return converted_price
    _LOGGER.debug("_get_import_price: No 'general' channel found in current prices")
    return None


def _get_export_price(data):
    """Extract export earnings from Amber feedIn data.

    Amber convention: feedIn.perKwh is NEGATIVE when you earn money (good)
                      feedIn.perKwh is POSITIVE when you pay to export (bad)

    We negate to show user-friendly "export earnings":
        Positive = earning money per kWh exported
        Negative = paying money per kWh exported
    """
    if not data:
        _LOGGER.debug("_get_export_price: No data available")
        return None
    if not data.get("current"):
        _LOGGER.debug("_get_export_price: No 'current' key in data")
        return None
    current_prices = data.get("current", [])
    channel_types = [p.get("channelType") for p in current_prices]
    _LOGGER.debug(
        "_get_export_price: Found %d entries with channels: %s",
        len(current_prices),
        channel_types,
    )
    for price in current_prices:
        if price.get("channelType") == "feedIn":
            raw_price = price.get("perKwh", 0)
            # Negate to convert from Amber feedIn to export earnings
            # Amber feedIn +10 (paying) → sensor -0.10 (negative earnings)
            # Amber feedIn -10 (earning) → sensor +0.10 (positive earnings)
            converted_price = -raw_price / 100
            _LOGGER.debug(
                "_get_export_price: Found feedIn price: %s c/kWh -> %s $/kWh",
                raw_price,
                converted_price,
            )
            return converted_price
    _LOGGER.debug("_get_export_price: No 'feedIn' channel found in current prices")
    return None


PRICE_SENSORS: tuple[Teslav1rSensorEntityDescription, ...] = (
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_CURRENT_IMPORT_PRICE,
        name="Current Import Price",
        currency_unit="major_rate",
        currency_attrs=True,
        suggested_display_precision=4,
        value_fn=_get_import_price,
        attr_fn=lambda data: {
            ATTR_PRICE_SPIKE: data.get("current", [{}])[0].get("spikeStatus")
            if data and data.get("current")
            else None,
            ATTR_WHOLESALE_PRICE: data.get("current", [{}])[0].get(
                "wholesaleKWHPrice", 0
            )
            / 100
            if data and data.get("current")
            else 0,
            ATTR_NETWORK_PRICE: data.get("current", [{}])[0].get("networkKWHPrice", 0)
            / 100
            if data and data.get("current")
            else 0,
        },
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_CURRENT_EXPORT_PRICE,
        name="Current Export Price",
        currency_unit="major_rate",
        currency_attrs=True,
        suggested_display_precision=4,
        icon="mdi:transmission-tower-export",
        value_fn=_get_export_price,
        attr_fn=lambda data: {
            "channel_type": "feedIn",
        },
    ),
)


_SUPPORTED_GRID_STATUS_VALUES = frozenset(
    {
        "active",
        "systemgridconnected",
        "inactive",
        "islanded",
        "off-grid",
        "systemislandedactive",
    }
)


def _grid_status_value(data: dict[str, Any] | None) -> str | None:
    """Return only a recognized provider-reported grid status."""
    if not data:
        return None
    raw_status = data.get("grid_status")
    if not isinstance(raw_status, str):
        return None
    if raw_status.strip().lower() not in _SUPPORTED_GRID_STATUS_VALUES:
        return None
    return raw_status


ENERGY_SENSORS: tuple[Teslav1rSensorEntityDescription, ...] = (
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_SOLAR_POWER,
        name="Solar Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("solar_power") if data else None,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_GRID_POWER,
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("grid_power") if data else None,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_GRID_STATUS,
        name="Grid Status",
        icon="mdi:transmission-tower",
        value_fn=_grid_status_value,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_POWER,
        name="Battery Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda data: data.get("battery_power") if data else None,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_HOME_LOAD,
        name="Home Load",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=_home_load_power_kw,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_LEVEL,
        name="Battery Level",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("battery_level") if data else None,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_SOLAR_ENERGY,
        name="Daily Solar Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:solar-power",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("pv_today_kwh") if data else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_GRID_IMPORT,
        name="Daily Grid Import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("grid_import_today_kwh")
            if data
            else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_GRID_EXPORT,
        name="Daily Grid Export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:transmission-tower-export",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("grid_export_today_kwh")
            if data
            else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_CHARGE,
        name="Daily Battery Charge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-charging",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("charge_today_kwh") if data else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_BATTERY_DISCHARGE,
        name="Daily Battery Discharge",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("discharge_today_kwh") if data else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_LOAD,
        name="Daily Home Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("load_today_kwh") if data else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_IMPORT_COST,
        name="Daily Import Cost",
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-minus",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("import_cost_today") if data else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_EXPORT_EARNINGS,
        name="Daily Export Earnings",
        currency_unit="money",
        currency_attrs=True,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        icon="mdi:cash-plus",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("export_earnings_today")
            if data
            else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_DAILY_AVG_COST_PER_KWH,
        name="Average Cost per kWh Today",
        currency_unit="major_rate",
        currency_attrs=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:cash-clock",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("avg_cost_per_kwh_today")
            if data
            else None
        ),
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_MTD_AVG_COST_PER_KWH,
        name="Average Cost per kWh Month to Date",
        currency_unit="major_rate",
        currency_attrs=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:calendar-month",
        value_fn=lambda data: (
            data.get("energy_summary", {}).get("avg_cost_per_kwh_mtd") if data else None
        ),
    ),
)

TESLA_SENSORS: tuple[Teslav1rSensorEntityDescription, ...] = (
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_FIRMWARE,
        name="Firmware",
        icon="mdi:chip",
        value_fn=lambda data: data.get("firmware") if data else None,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_TOTAL_PACK_ENERGY,
        name="Battery Pack Capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-high",
        value_fn=lambda data: data.get("total_pack_energy_kwh") if data else None,
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_ENERGY_LEFT,
        name="Battery Energy Left",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-50",
        value_fn=lambda data: data.get("energy_left_kwh") if data else None,
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_BACKUP_TIME_REMAINING,
        name="Backup Time Remaining",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:timer-sand",
        value_fn=lambda data: data.get("backup_time_remaining_hours") if data else None,
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_GRID_SERVICES_POWER,
        name="Grid Services Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        icon="mdi:transmission-tower",
        value_fn=lambda data: data.get("grid_services_power_kw") if data else None,
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_SOLAR,
        name="Lifetime Solar Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:solar-power-variant",
        value_fn=lambda data: (
            (data.get("lifetime_totals") or {}).get("lifetime_solar_kwh")
            if data
            else None
        ),
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_GRID_IMPORT,
        name="Lifetime Grid Import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:transmission-tower-import",
        value_fn=lambda data: (
            (data.get("lifetime_totals") or {}).get("lifetime_grid_import_kwh")
            if data
            else None
        ),
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_GRID_EXPORT,
        name="Lifetime Grid Export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:transmission-tower-export",
        value_fn=lambda data: (
            (data.get("lifetime_totals") or {}).get("lifetime_grid_export_kwh")
            if data
            else None
        ),
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_BATTERY_CHARGED,
        name="Lifetime Battery Charged",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:battery-charging-100",
        value_fn=lambda data: (
            (data.get("lifetime_totals") or {}).get("lifetime_battery_charged_kwh")
            if data
            else None
        ),
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_BATTERY_DISCHARGED,
        name="Lifetime Battery Discharged",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:battery-arrow-down",
        value_fn=lambda data: (
            (data.get("lifetime_totals") or {}).get("lifetime_battery_discharged_kwh")
            if data
            else None
        ),
        device_section="powerwall",
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_LIFETIME_HOME_CONSUMPTION,
        name="Lifetime Home Consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        icon="mdi:home-lightning-bolt",
        value_fn=lambda data: (
            (data.get("lifetime_totals") or {}).get("lifetime_home_kwh")
            if data
            else None
        ),
        device_section="powerwall",
    ),
)

# Shared sensors exposing BMS/inverter-reported power ceilings. Coordinators
# populate the same battery_max_* fields even when the brand-specific source
# differs (for example AlphaESS BMS registers vs FoxESS nominal inverter power).
BMS_POWER_LIMIT_SENSORS: tuple[Teslav1rSensorEntityDescription, ...] = (
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_MAX_CHARGE_POWER,
        name="Battery Max Charge Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-plus",
        value_fn=lambda data: data.get("battery_max_charge_power") if data else None,
    ),
    Teslav1rSensorEntityDescription(
        key=SENSOR_TYPE_BATTERY_MAX_DISCHARGE_POWER,
        name="Battery Max Discharge Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:battery-minus",
        value_fn=lambda data: data.get("battery_max_discharge_power") if data else None,
    ),
)


def _parse_optimizer_time(value: Any) -> datetime | None:
    """Parse an optimizer ISO timestamp."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_optimizer_window(start: datetime | None, end: datetime | None) -> str:
    """Format a compact time range for HA state display."""
    if not start or not end:
        return "unknown"

    start_local = dt_util.as_local(start)
    end_local = dt_util.as_local(end)
    prefix = "" if start_local.date() == dt_util.now().date() else f"{start_local:%a} "
    return f"{prefix}{start_local:%H:%M}-{end_local:%H:%M}"


def _optimizer_action_names(action: str | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize one or more optimizer action names."""
    return (action,) if isinstance(action, str) else action


def _future_optimizer_action_windows(
    data: dict[str, Any] | None,
    action: str | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return future consolidated optimizer windows for an action."""
    if not data:
        return []

    action_names = _optimizer_action_names(action)
    windows: list[dict[str, Any]] = []
    now = dt_util.now()
    for item in data.get("next_actions") or []:
        item_action = item.get("action")
        if item_action not in action_names:
            continue

        start = _parse_optimizer_time(item.get("timestamp"))
        end = _parse_optimizer_time(item.get("end_time"))
        if end and end <= now:
            continue

        planned_power_w = item.get("power_w")
        display_power_w = planned_power_w
        if start and end and start <= now < end:
            effective_action = data.get("effective_current_action") or data.get(
                "current_action"
            )
            if item_action == effective_action:
                try:
                    current_power_w = float(data.get("current_power_w") or 0)
                except (TypeError, ValueError):
                    current_power_w = 0
                if current_power_w > 0:
                    display_power_w = current_power_w

        window: dict[str, Any] = {
            "action": item_action,
            "start_time": item.get("timestamp"),
            "end_time": item.get("end_time"),
            "label": _format_optimizer_window(start, end),
            "power_w": display_power_w,
            "planned_power_w": planned_power_w,
            "soc": item.get("soc"),
        }

        if start and end:
            window["duration_minutes"] = round((end - start).total_seconds() / 60)

        windows.append(window)

    return windows


def _optimizer_window_state(
    data: dict[str, Any] | None,
    action: str | tuple[str, ...],
) -> str:
    """Return a short sensor state for upcoming optimizer windows."""
    windows = _future_optimizer_action_windows(data, action)
    if not windows:
        return "none"

    labels = [w["label"] for w in windows if w.get("label")]
    state = ", ".join(labels)
    if len(state) <= 255:
        return state

    return f"{labels[0]} (+{len(labels) - 1} more)"


def _optimizer_window_attributes(
    data: dict[str, Any] | None,
    action: str | tuple[str, ...],
) -> dict[str, Any]:
    """Return attributes for upcoming optimizer windows."""
    action_names = _optimizer_action_names(action)
    windows = _future_optimizer_action_windows(data, action)
    total_minutes = sum(w.get("duration_minutes", 0) or 0 for w in windows)
    attrs: dict[str, Any] = {
        "actions": list(action_names),
        "count": len(windows),
        "total_minutes": total_minutes,
        "windows": windows,
    }
    if windows:
        attrs["next_start"] = windows[0].get("start_time")
        attrs["next_end"] = windows[0].get("end_time")
        attrs["next_label"] = windows[0].get("label")
        attrs["next_power_w"] = windows[0].get("power_w")
    return attrs


class BatteryIntegrationDetailsSensor(SensorEntity):
    """Expose a live, non-duplicating catalog of upstream battery sensors."""

    _attr_has_entity_name = True
    _attr_name = "Battery Integration Details"
    _attr_icon = "mdi:home-battery-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._catalog: dict[str, Any] = {}
        self._attr_unique_id = f"{entry.entry_id}_battery_integration_details"
        self._attr_suggested_object_id = "power_sync_battery_integration_details"

    @property
    def device_info(self) -> dict[str, Any]:
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_GRID_HOME)

    @property
    def native_value(self) -> int:
        return len(self._catalog.get("entity_ids", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        metrics = self._catalog.get("metrics", [])
        self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        control = {}
        return {
            **control,
            "catalog_version": self._catalog.get("version", 1),
            "connection_profile": self._catalog.get("profile_id", ""),
            "battery_system": self._catalog.get("battery_system", ""),
            "display_mode": self._catalog.get("display_mode", "recommended"),
            "monitoring_only": bool(self._catalog.get("monitoring_only", False)),
            "controls_summary": self._catalog.get("controls_summary", ""),
            "entity_ids": list(self._catalog.get("entity_ids", [])),
            "groups": dict(self._catalog.get("groups", {})),
            "canonical_entities": dict(self._catalog.get("canonical_entities", {})),
            "disabled_count": int(self._catalog.get("disabled_count", 0)),
            "unavailable_count": sum(
                1
                for metric in metrics
                if metric.get("enabled") and not metric.get("available")
            ),
        }

    async def async_update(self) -> None:
        domain_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        profile = domain_data.get("battery_connection_profile")
        prior = domain_data.get("battery_sensor_catalog", {})
        if profile is None:
            self._catalog = dict(prior)
            return
        kwargs = {
            "battery_system": prior.get("battery_system", ""),
            "profile_id": profile.profile_id,
            "allowed_domains": profile.upstream_domains,
            "config_entry_id": prior.get("source_config_entry_id") or None,
            "anchor_entity_id": prior.get("anchor_entity_id") or None,
        }
        display_mode = self._entry.options.get(
            CONF_BATTERY_SENSOR_DISPLAY_MODE,
            self._entry.data.get(
                CONF_BATTERY_SENSOR_DISPLAY_MODE,
                BATTERY_SENSOR_DISPLAY_RECOMMENDED,
            ),
        )
        catalog = discover_battery_sensor_catalog(
            self._hass,
            **kwargs,
            display_mode=display_mode,
        )
        all_metrics = discover_battery_sensor_catalog(
            self._hass,
            **kwargs,
            display_mode=BATTERY_SENSOR_DISPLAY_ALL,
        )
        canonical, _missing = discover_canonical_entities(
            all_metrics,
            battery_system=kwargs["battery_system"],
        )
        catalog["canonical_entities"] = canonical
        catalog["monitoring_only"] = profile.monitoring_only
        catalog["controls_summary"] = profile.controls_summary
        self._catalog = catalog
        domain_data["battery_sensor_catalog"] = catalog


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Teslav1r sensor entities."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    tesla_coordinator: TeslaEnergyCoordinator | None = domain_data.get(
        "tesla_coordinator"
    )

    entities: list[SensorEntity] = []

    integration_details = BatteryIntegrationDetailsSensor(hass, entry)
    await integration_details.async_update()
    entities.append(integration_details)

    _LOGGER.debug("No price coordinator or known provider - skipping price sensors")

    # Add Tesla-specific sensors (gateway firmware, etc.)
    if tesla_coordinator:
        for description in TESLA_SENSORS:
            entities.append(
                TeslaEnergySensor(
                    coordinator=tesla_coordinator,
                    description=description,
                    entry=entry,
                )
            )

    # Always add battery health sensor
    # For non-Tesla systems, pass coordinator so it can read battery_soh
    battery_system = "tesla"

    # Always add battery mode sensor (for automation triggers)
    entities.append(BatteryModeSensor(hass=hass, entry=entry))
    _LOGGER.info("Battery mode sensor added")

    # Powerwall local TEDAPI sensors — gated on completed pairing.
    # System-level sensors come from the live local snapshot.
    if entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
        local_coord = domain_data.get("powerwall_local", {}).get("coordinator")
        if local_coord is not None:
            entities.extend(
                [
                    PowerwallSystemIslandStateSensor(local_coord, entry),
                    PowerwallCountSensor(local_coord, entry),
                    PowerwallActiveAlertsSensor(local_coord, entry),
                    PowerwallV1rDeviceSensor(local_coord, entry),
                    PowerwallV1rFirmwareSensor(local_coord, entry),
                    PowerwallV1rNetworkSensor(local_coord, entry),
                    PowerwallV1rInternetSensor(local_coord, entry),
                ]
            )
    # Pack-level sensors come from the richer BMS health scan because
    # batteryBlocks only contains shallow block identity/count data on PW3 sites.
    if battery_system == "tesla":
        _setup_powerwall_pack_sensor_additions(hass, entry, async_add_entities)
        _setup_powerwall_solar_string_sensor_additions(hass, entry, async_add_entities)

    async_add_entities(entities)


def _powerwall_pack_data(health_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return BMS-scanned packs, including expansion packs, in scan order."""
    if not isinstance(health_data, dict):
        return []
    packs = health_data.get("individual_batteries") or []
    if not isinstance(packs, list):
        return []
    return [pack for pack in packs if isinstance(pack, dict)]


def _pack_value(pack: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = pack.get(key)
        if value is not None:
            return value
    return None


def _pack_float(pack: dict[str, Any], *keys: str) -> float | None:
    value = _pack_value(pack, *keys)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pack_has_value(pack: dict[str, Any], *keys: str) -> bool:
    return _pack_value(pack, *keys) is not None


def _pack_label(packs: list[dict[str, Any]], index: int) -> str:
    """Human label for a BMS pack: base Powerwalls first, expansions separately."""
    pack = packs[index]
    role = pack.get("role")
    if role == "powerwall":
        powerwall_number = sum(
            1 for prior in packs[: index + 1] if prior.get("role") == "powerwall"
        )
        return f"Powerwall {powerwall_number}"
    if role == "leader":
        return "Leader Powerwall"
    if role == "follower" or pack.get("isFollower"):
        follower_number = sum(
            1
            for prior in packs[: index + 1]
            if prior.get("role") == "follower" or prior.get("isFollower")
        )
        return (
            "Follower Powerwall"
            if follower_number == 1
            else f"Follower Powerwall {follower_number}"
        )
    if pack.get("isExpansion"):
        expansion_number = sum(
            1 for prior in packs[: index + 1] if prior.get("isExpansion")
        )
        return f"Expansion Pack {expansion_number}"

    base_number = sum(1 for prior in packs[: index + 1] if not prior.get("isExpansion"))
    return (
        "Leader Powerwall"
        if base_number == 1
        else f"Follower Powerwall {base_number - 1}"
    )


def _pack_metric_available(packs: list[dict[str, Any]], metric: str) -> bool:
    if metric in ("soc", "capacity", "soh"):
        return any(
            _pack_float(pack, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh")
            for pack in packs
        )
    if metric == "current_energy":
        return any(
            _pack_float(
                pack,
                "nominalEnergyRemainingWh",
                "nominal_energy_remaining_wh",
            )
            is not None
            for pack in packs
        )
    if metric == "voltage":
        return any(
            _pack_has_value(
                pack,
                "voltage_v",
                "voltage",
                "battery_voltage",
                "BMS_packVoltage",
            )
            for pack in packs
        )
    if metric == "temperature":
        return any(
            _pack_has_value(
                pack,
                "temperature_c",
                "temperature",
                "battery_temp",
                "BMS_maxCellTemp",
                "BMS_minCellTemp",
            )
            for pack in packs
        )
    return False


def _pack_sensor_classes_for(
    packs: list[dict[str, Any]],
) -> tuple[type[SensorEntity], ...]:
    classes: list[type[SensorEntity]] = [
        PowerwallBlockSocSensor,
        PowerwallBlockCurrentEnergySensor,
        PowerwallBlockCapacitySensor,
        PowerwallBlockSohSensor,
    ]
    if _pack_metric_available(packs, "voltage"):
        classes.append(PowerwallBlockVoltageSensor)
    if _pack_metric_available(packs, "temperature"):
        classes.append(PowerwallBlockTemperatureSensor)
    return tuple(classes)


def _build_powerwall_pack_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    packs: list[dict[str, Any]],
    added_keys: set[tuple[int, str]],
) -> list[SensorEntity]:
    """Build pack-level entities for BMS metrics that are present."""
    entities: list[SensorEntity] = []
    for index, _pack in enumerate(packs):
        for sensor_cls in _pack_sensor_classes_for(packs):
            if not _pack_metric_available([_pack], sensor_cls.metric_key):
                continue
            key = (index, sensor_cls.metric_key)
            if key in added_keys:
                continue
            added_keys.add(key)
            entities.append(sensor_cls(hass, entry, index))
    return entities


def _setup_powerwall_pack_sensor_additions(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create pack sensors from BMS health data now and after future scans."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    added_keys: set[tuple[int, str]] = domain_data.setdefault(
        "powerwall_pack_sensor_keys", set()
    )

    def _add_from_health(health_data: dict[str, Any] | None) -> None:
        packs = _powerwall_pack_data(health_data)
        if not packs:
            return
        new_entities = _build_powerwall_pack_sensors(hass, entry, packs, added_keys)
        if new_entities:
            async_add_entities(new_entities)
            _LOGGER.info(
                "Added %d Powerwall pack sensors across %d BMS pack(s)",
                len(new_entities),
                len(packs),
            )

    _add_from_health(domain_data.get("battery_health"))

    if domain_data.get("powerwall_pack_sensor_unsub") is not None:
        return

    @callback
    def _handle_battery_health_update(data: dict[str, Any]) -> None:
        _add_from_health(data)

    domain_data["powerwall_pack_sensor_unsub"] = async_dispatcher_connect(
        hass,
        f"{DOMAIN}_battery_health_update_{entry.entry_id}",
        _handle_battery_health_update,
    )

    try:
        _cleanup_legacy_powerwall_pack_registry(hass, entry)
    except Exception:
        _LOGGER.warning(
            "Could not clean up legacy Powerwall pack registry entries", exc_info=True
        )


def _solar_string_data(diagnostics: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(diagnostics, dict):
        return []
    strings = diagnostics.get("strings")
    if not isinstance(strings, list):
        return []
    return [string for string in strings if isinstance(string, dict)]


def _solar_string_label(reading: dict[str, Any], index: int) -> str:
    label = reading.get("label")
    if isinstance(label, str) and label:
        return label
    mppt = reading.get("mppt")
    if isinstance(mppt, str) and mppt:
        return mppt
    return str(index + 1)


def _solar_string_key(reading: dict[str, Any], index: int) -> str:
    raw = reading.get("id") or reading.get("label") or f"string_{index + 1}"
    key = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(raw)).strip("_")
    return key or f"string_{index + 1}"


def _build_powerwall_solar_string_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    diagnostics: dict[str, Any] | None,
    added_keys: set[str],
) -> list[SensorEntity]:
    """Build DC string voltage entities for strings reported by TEDAPI scans."""
    entities: list[SensorEntity] = []
    for index, reading in enumerate(_solar_string_data(diagnostics)):
        key = _solar_string_key(reading, index)
        if key in added_keys:
            continue
        added_keys.add(key)
        entities.append(
            PowerwallSolarStringVoltageSensor(
                hass,
                entry,
                key,
                reading.get("id"),
                _solar_string_label(reading, index),
            )
        )
    return entities


def _setup_powerwall_solar_string_sensor_additions(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create Powerwall string voltage sensors now and after future scans."""
    domain_data = hass.data[DOMAIN][entry.entry_id]
    added_keys: set[str] = domain_data.setdefault(
        "powerwall_solar_string_sensor_keys", set()
    )

    def _add_from_diagnostics(diagnostics: dict[str, Any] | None) -> None:
        new_entities = _build_powerwall_solar_string_sensors(
            hass,
            entry,
            diagnostics,
            added_keys,
        )
        if new_entities:
            async_add_entities(new_entities)
            _LOGGER.info(
                "Added %d Powerwall solar string voltage sensor(s)",
                len(new_entities),
            )

    _add_from_diagnostics(domain_data.get("solar_string_diagnostics"))

    if domain_data.get("powerwall_solar_string_sensor_unsub") is not None:
        return

    @callback
    def _handle_solar_strings_update(data: dict[str, Any]) -> None:
        _add_from_diagnostics(data)

    domain_data["powerwall_solar_string_sensor_unsub"] = async_dispatcher_connect(
        hass,
        f"{DOMAIN}_solar_strings_update_{entry.entry_id}",
        _handle_solar_strings_update,
    )


def _cleanup_legacy_powerwall_pack_registry(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Remove stale standalone Powerwall N registry entries from older releases."""
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
    except Exception as err:
        _LOGGER.debug(
            "Unable to access HA registries for Powerwall pack cleanup: %s", err
        )
        return

    legacy_device_ids: set[str] = set()
    legacy_identifier_prefix = f"{entry.entry_id}_pw_"
    for device in list(iter_device_entries(device_registry)):
        identifiers = getattr(device, "identifiers", set()) or set()
        for identifier_entry in identifiers:
            if (
                not isinstance(identifier_entry, (tuple, list))
                or len(identifier_entry) < 2
            ):
                continue
            domain, identifier = identifier_entry[0], identifier_entry[1]
            if domain == DOMAIN and str(identifier).startswith(
                legacy_identifier_prefix
            ):
                legacy_device_ids.add(device.id)
                break

    for entity in list(entity_registry.entities.values()):
        if (
            entity.platform == DOMAIN
            and entity.device_id in legacy_device_ids
            and str(entity.unique_id).startswith(f"{entry.entry_id}_pw")
            and str(entity.unique_id).endswith(("_temperature", "_voltage"))
        ):
            entity_registry.async_remove(entity.entity_id)

    for device_id in legacy_device_ids:
        try:
            device_registry.async_update_device(
                device_id=device_id,
                remove_config_entry_id=entry.entry_id,
            )
        except Exception as err:
            _LOGGER.debug(
                "Unable to remove legacy Powerwall pack device %s: %s", device_id, err
            )


_LOCAL_GRID_STATUS_TO_CLOUD = {
    "Active": "Active",
    "SystemGridConnected": "Active",
    "Inactive": "Off-Grid",
    "Islanded": "Off-Grid",
    "Off-Grid": "Off-Grid",
    "SystemIslandedActive": "Off-Grid",
}


def _local_value_for(
    sensor_key: str,
    snap: Any,
    *,
    ev_power_kw: float = 0.0,
    ev_load_complete: bool = True,
) -> Any:
    """Map a sensor key to its equivalent on the local PowerwallSnapshot.

    Returns the locally-derived value in the same units as the cloud value.
    ``None`` normally means no local equivalent; fresh grid status treats it
    as authoritative uncertainty instead of falling through to cloud.
    """
    if snap is None:
        return None
    if sensor_key == SENSOR_TYPE_BATTERY_POWER:
        return None if snap.battery_w is None else snap.battery_w / 1000.0
    if sensor_key == SENSOR_TYPE_GRID_POWER:
        return None if snap.grid_w is None else snap.grid_w / 1000.0
    if sensor_key == SENSOR_TYPE_SOLAR_POWER:
        return None if snap.solar_w is None else snap.solar_w / 1000.0
    if sensor_key == SENSOR_TYPE_HOME_LOAD:
        if snap.load_w is None:
            return None
        if not ev_load_complete:
            return None
        # Powerwall local TEDAPI reports total behind-the-meter load, which
        # includes EV charging. Keep Home Load aligned with the cloud
        # coordinator and Tesla app by removing observed EV charging power.
        return max(0.0, (snap.load_w / 1000.0) - ev_power_kw)
    if sensor_key == SENSOR_TYPE_BATTERY_LEVEL:
        return snap.soc
    if sensor_key == SENSOR_TYPE_GRID_STATUS:
        raw_grid_status = getattr(snap, "grid_status", None)
        if raw_grid_status is None:
            return None
        return _LOCAL_GRID_STATUS_TO_CLOUD.get(raw_grid_status)
    return None


_LOCAL_OVERRIDABLE = {
    SENSOR_TYPE_BATTERY_POWER,
    SENSOR_TYPE_GRID_POWER,
    SENSOR_TYPE_SOLAR_POWER,
    SENSOR_TYPE_HOME_LOAD,
    SENSOR_TYPE_BATTERY_LEVEL,
    SENSOR_TYPE_GRID_STATUS,
}

# How recently the local coordinator must have ticked for its data to be
# trusted by the local-prefer override. The local coord polls every 2s, so
# 30s is ~15 missed ticks — comfortably past transient blips, well before
# the data turns into a "stuck at 41%" disaster.
_LOCAL_STALE_SECONDS = TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS
_ENERGY_COORDINATOR_STALE_FACTOR = 4
_ENERGY_COORDINATOR_MIN_STALE_SECONDS = 60


def _local_data_is_fresh(local_coord: Any) -> bool:
    """True iff the local coordinator's last successful update is recent."""
    if local_coord is None or local_coord.data is None:
        return False
    last_ts = getattr(local_coord, "last_success_monotonic", None)
    if last_ts is None:
        return False
    import time as _time

    return (_time.monotonic() - last_ts) <= _LOCAL_STALE_SECONDS


def _coordinator_data_is_fresh(coordinator: Any) -> bool:
    """Return False when a coordinator is serving stale energy data."""
    if not getattr(coordinator, "last_update_success", True):
        return False
    if getattr(coordinator, "data", None) is None:
        return False

    last_update = getattr(coordinator, "last_update_success_time", None)
    update_interval = getattr(coordinator, "update_interval", None)
    if last_update is None or update_interval is None:
        return True

    try:
        stale_after = max(
            update_interval * _ENERGY_COORDINATOR_STALE_FACTOR,
            timedelta(seconds=_ENERGY_COORDINATOR_MIN_STALE_SECONDS),
        )
        now = dt_util.utcnow()
        if (
            getattr(now, "tzinfo", None) is None
            and getattr(last_update, "tzinfo", None) is not None
        ):
            now = now.replace(tzinfo=last_update.tzinfo)
        elif (
            getattr(now, "tzinfo", None) is not None
            and getattr(last_update, "tzinfo", None) is None
        ):
            last_update = last_update.replace(tzinfo=now.tzinfo)
        age = now - last_update
    except Exception:
        return True

    return age <= stale_after


class TeslaEnergySensor(
    Teslav1rCurrencyMixin, CoordinatorEntity, RestoredNumericStateMixin, SensorEntity
):
    """Sensor for Tesla energy data.

    Reads cloud-coordinator data via the entity description's ``value_fn`` by
    default. When the entry is paired and the local coordinator has a fresh
    snapshot, the locally-derived value wins for keys in ``_LOCAL_OVERRIDABLE``
    — and the entity also subscribes to local coordinator updates so it
    refreshes at the local 2s cadence instead of the cloud 30-60s cadence.
    """

    entity_description: Teslav1rSensorEntityDescription

    def __init__(
        self,
        coordinator: TeslaEnergyCoordinator,
        description: Teslav1rSensorEntityDescription,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_has_entity_name = True
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{description.key}"
        self._entry = entry
        self._local_unsub = None

    @property
    def device_info(self):
        if self.entity_description.device_section == "powerwall":
            return powerwall_device_info(self._entry.entry_id)
        return family_device_info(
            self._entry.entry_id,
            SENSOR_KEY_TO_FAMILY.get(
                self.entity_description.key, SENSOR_FAMILY_BATTERY
            ),
        )

    def _local_coordinator(self):
        """Return the PowerwallLocalCoordinator if paired and built, else None."""
        if not self._entry.data.get(CONF_POWERWALL_LOCAL_PAIRED):
            return None
        bucket = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("powerwall_local", {})
        )
        return bucket.get("coordinator")

    def _battery_system(self) -> str:
        """Return the configured site battery backend."""
        return str(
            self._entry.options.get(
                CONF_BATTERY_SYSTEM,
                self._entry.data.get(CONF_BATTERY_SYSTEM, "tesla"),
            )
            or "tesla"
        ).lower()

    def _observed_ev_snapshot(self):
        """Return the fresh site-wide EV observation cached by EVStatusSensor."""
        from .ev_load import (
            EvLoadObservation,
            EvLoadQuality,
            EvMeasurementKind,
            ObservedEvLoadSnapshot,
            aggregate_ev_load,
            reconcile_ev_load_snapshot,
        )

        now = dt_util.utcnow()
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        snapshot = entry_data.get("observed_ev_load_snapshot")
        coordinator_data = self.coordinator.data or {}
        physical_fallbacks = coordinator_data.get("ev_power_fallback_by_physical_key")
        if physical_fallbacks:
            return reconcile_ev_load_snapshot(
                snapshot if isinstance(snapshot, ObservedEvLoadSnapshot) else None,
                at=now,
                fallback_power_kw=coordinator_data.get("ev_power", 0.0),
                fallback_by_physical_key=physical_fallbacks,
                fallback_observed_at=coordinator_data.get("last_update"),
            )
        if isinstance(snapshot, ObservedEvLoadSnapshot):
            age = now - snapshot.observed_at
            if timedelta(0) <= age <= timedelta(seconds=90):
                return snapshot
            if snapshot.components or snapshot.unavailable_active_keys:
                return ObservedEvLoadSnapshot(
                    power_kw=0.0,
                    components=(),
                    observed_at=now,
                    quality=EvLoadQuality.INCOMPLETE,
                    unavailable_active_keys=tuple(
                        item.physical_load_key for item in snapshot.components
                    )
                    or snapshot.unavailable_active_keys,
                )

        embedded_ev = coordinator_data.get("ev_power")
        if embedded_ev is not None:
            try:
                active = abs(float(embedded_ev or 0.0)) > 0.05
            except (TypeError, ValueError):
                active = True
            return aggregate_ev_load(
                [
                    EvLoadObservation(
                        physical_load_key="coordinator:embedded_ev",
                        source_key="energy_coordinator",
                        power_kw=embedded_ev,
                        observed_at=now,
                        active=active,
                        measurement_kind=EvMeasurementKind.INTEGRATED_CHARGER,
                        supports_bidirectional_power=self._battery_system()
                        == "sigenergy",
                    )
                ],
                at=now,
            )
        return ObservedEvLoadSnapshot(
            power_kw=0.0,
            components=(),
            observed_at=now,
            quality=EvLoadQuality.COMPLETE,
        )

    def _normalized_energy_data(self) -> dict[str, Any] | None:
        """Return the canonical site snapshot used by every energy sensor."""
        from .ev_load import normalize_energy_data

        return normalize_energy_data(
            self.coordinator.data,
            battery_system=self._battery_system(),
            ev_load=self._observed_ev_snapshot(),
            at=dt_util.utcnow(),
        )

    @property
    def available(self) -> bool:
        """Return False when the backing energy coordinator is stale."""
        return _coordinator_data_is_fresh(self.coordinator)

    @property
    def native_value(self) -> Any:
        """Prefer local snapshot value when paired AND fresh; else cloud value_fn.

        Freshness guard: if the local coordinator's last successful update is
        older than ``_LOCAL_STALE_SECONDS``, fall through to cloud. The local
        coordinator can die silently (eg gateway unreachable, key rejection,
        unhandled exception in update loop) and its ``data`` attribute keeps
        the last successful snapshot. Without this guard, sensors would
        cling to that stale value indefinitely.
        """
        if self.entity_description.key in _LOCAL_OVERRIDABLE:
            local_coord = self._local_coordinator()
            if local_coord is not None and _local_data_is_fresh(local_coord):
                ev_snapshot = self._observed_ev_snapshot()
                local_v = _local_value_for(
                    self.entity_description.key,
                    local_coord.data,
                    ev_power_kw=ev_snapshot.power_kw,
                    ev_load_complete=ev_snapshot.quality.value == "complete",
                )
                if self.entity_description.key == SENSOR_TYPE_GRID_STATUS:
                    return local_v
                if local_v is not None:
                    return local_v
        if self.entity_description.value_fn:
            energy_data = self._normalized_energy_data()
            value = self.entity_description.value_fn(energy_data)
            if value is not None:
                return value
            summary_key = _ENERGY_SUMMARY_VALUE_KEYS.get(self.entity_description.key)
            summary = (energy_data or {}).get("energy_summary")
            if (
                summary_key is not None
                and isinstance(summary, dict)
                and summary_key in summary
            ):
                # A current explicit None means accounting coverage is partial;
                # do not mask that state with a restored historical number.
                return None
            return self._restored_numeric_value(self.entity_description.key)
        return None

    async def async_added_to_hass(self) -> None:
        """Subscribe to both cloud and local coordinator updates."""
        await super().async_added_to_hass()
        await self._async_restore_numeric_state()
        if self.entity_description.key in _LOCAL_OVERRIDABLE:
            local_coord = self._local_coordinator()
            if local_coord is not None:
                self._local_unsub = local_coord.async_add_listener(
                    self.async_write_ha_state
                )

    async def async_will_remove_from_hass(self) -> None:
        """Drop the local coordinator listener cleanly."""
        if self._local_unsub is not None:
            self._local_unsub()
            self._local_unsub = None
        await super().async_will_remove_from_hass()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        energy_data = self._normalized_energy_data() or {}
        if self.entity_description.attr_fn:
            attrs = self.entity_description.attr_fn(energy_data)
        else:
            attrs = {}
        if self.entity_description.key == SENSOR_TYPE_HOME_LOAD:
            attrs.update(
                {
                    "home_load_basis": energy_data.get("home_load_basis"),
                    "raw_home_load_kw": energy_data.get("raw_home_load_power"),
                    "observed_ev_power_kw": energy_data.get("observed_ev_power"),
                    "normalization_quality": energy_data.get(
                        "home_load_normalization_quality"
                    ),
                }
            )
        energy_summary = energy_data.get("energy_summary") or {}
        if self.entity_description.key == SENSOR_TYPE_DAILY_IMPORT_COST:
            attrs.update(
                {
                    "coverage": energy_summary.get("import_cost_coverage"),
                    "priced_energy_kwh": energy_summary.get("import_cost_covered_kwh"),
                    "energy_source": energy_summary.get("grid_import_today_source"),
                }
            )
        elif self.entity_description.key == SENSOR_TYPE_DAILY_EXPORT_EARNINGS:
            attrs.update(
                {
                    "coverage": energy_summary.get("export_earnings_coverage"),
                    "priced_energy_kwh": energy_summary.get(
                        "export_earnings_covered_kwh"
                    ),
                    "energy_source": energy_summary.get("grid_export_today_source"),
                }
            )
        return _entity_currency_attrs(self, attrs)


class _PowerwallLocalSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for sensors that read directly from the local TEDAPI snapshot."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_suggested_object_id = f"power_sync_{key}"
        self._attr_name = name

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def _snap(self):
        return self.coordinator.data

    @property
    def _v1r_diagnostics(self) -> dict[str, Any]:
        diagnostics = getattr(self.coordinator, "_v1r_diagnostics", None)
        return diagnostics if isinstance(diagnostics, dict) else {}


class _PowerwallV1rSensorBase(_PowerwallLocalSensorBase):
    """Common API sensor with per-endpoint availability and diagnostics."""

    diagnostic_key = ""

    @property
    def available(self) -> bool:
        # Common API diagnostics are refreshed independently of the live DCQ
        # snapshot, so a DCQ polling failure must not hide a successful
        # identity/network/internet response.
        return isinstance(self._v1r_diagnostics.get(self.diagnostic_key), dict)

    def _diagnostic_attributes(self) -> dict[str, Any]:
        diagnostics = self._v1r_diagnostics
        attrs: dict[str, Any] = {}
        if diagnostics.get("last_attempt_ts") is not None:
            attrs["last_attempt_ts"] = diagnostics["last_attempt_ts"]
        if diagnostics.get("last_success_ts") is not None:
            attrs["last_success_ts"] = diagnostics["last_success_ts"]
        errors = diagnostics.get("errors") or {}
        if isinstance(errors, dict) and errors.get(self.diagnostic_key):
            attrs["error"] = errors[self.diagnostic_key]
        return attrs


class PowerwallV1rDeviceSensor(_PowerwallV1rSensorBase):
    """Gateway device identity from the read-only v1r Common API."""

    _attr_icon = "mdi:developer-board"
    diagnostic_key = "system_info"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_v1r_device", "v1r Device")

    @property
    def native_value(self) -> Any:
        info = self._v1r_diagnostics.get("system_info") or {}
        return info.get("device_type") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._v1r_diagnostics.get("system_info") or {}
        return {
            key: info.get(key)
            for key in ("part_number", "serial_number", "din")
            if info.get(key) is not None
        } | self._diagnostic_attributes()


class PowerwallV1rFirmwareSensor(_PowerwallV1rSensorBase):
    """Gateway firmware reported by the v1r Common API."""

    _attr_icon = "mdi:chip"
    diagnostic_key = "system_info"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_v1r_firmware", "v1r Firmware")

    @property
    def native_value(self) -> Any:
        info = self._v1r_diagnostics.get("system_info") or {}
        return info.get("firmware_version") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._v1r_diagnostics.get("system_info") or {}
        githash = info.get("firmware_githash")
        attrs = {"githash": githash} if githash else {}
        return attrs | self._diagnostic_attributes()


class PowerwallV1rNetworkSensor(_PowerwallV1rSensorBase):
    """Active gateway route with credential-free interface diagnostics."""

    _attr_icon = "mdi:router-network"
    diagnostic_key = "networking"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_v1r_network", "v1r Network")

    @property
    def native_value(self) -> Any:
        interfaces = self._v1r_diagnostics.get("networking")
        if not isinstance(interfaces, dict):
            return None
        for name, details in interfaces.items():
            if isinstance(details, dict) and details.get("active_route"):
                return name
        return "offline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        diagnostics = self._v1r_diagnostics
        interfaces = diagnostics.get("networking")
        attrs = self._diagnostic_attributes()
        if isinstance(interfaces, dict):
            attrs["interfaces"] = interfaces
        return attrs


class PowerwallV1rInternetSensor(_PowerwallV1rSensorBase):
    """Live gateway internet reachability from Common API field 30/31."""

    _attr_icon = "mdi:web-check"
    diagnostic_key = "internet"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_v1r_internet", "v1r Internet")

    @property
    def native_value(self) -> Any:
        interfaces = self._v1r_diagnostics.get("internet")
        if not isinstance(interfaces, dict):
            return None
        return (
            "connected"
            if any(
                isinstance(details, dict)
                and (details.get("connectivity") or {}).get("internet")
                for details in interfaces.values()
            )
            else "disconnected"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        interfaces = self._v1r_diagnostics.get("internet")
        attrs = self._diagnostic_attributes()
        if isinstance(interfaces, dict):
            attrs["interfaces"] = interfaces
        return attrs


class PowerwallSystemIslandStateSensor(_PowerwallLocalSensorBase):
    """Powerwall-reported island state (richer than the simple grid_status sensor)."""

    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, "pw_system_island_state", "System Island State"
        )

    @property
    def native_value(self) -> Any:
        snap = self._snap
        if snap is None:
            return None
        return snap.system_island_state or snap.grid_status


class PowerwallCountSensor(_PowerwallLocalSensorBase):
    """Number of in-service Powerwalls reported by the gateway."""

    _attr_icon = "mdi:battery-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pw_count", "Powerwall Count")

    @property
    def native_value(self) -> Any:
        snap = self._snap
        if snap is None:
            return None
        if snap.pw_count is not None:
            return snap.pw_count
        return len(snap.battery_blocks) if snap.battery_blocks else None


class PowerwallActiveAlertsSensor(_PowerwallLocalSensorBase):
    """Count actionable alerts while retaining informational alert details."""

    _attr_icon = "mdi:alert-circle"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, "pw_active_alerts", "Powerwall Active Alerts"
        )

    @property
    def native_value(self) -> Any:
        snap = self._snap
        if snap is None or snap.alerts is None:
            return None
        actionable, _informational = split_powerwall_alerts(snap.alerts)
        return len(actionable)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snap = self._snap
        if snap is None or not snap.alerts:
            return {}
        return powerwall_alert_attributes(snap.alerts)


class _PowerwallBlockSensorBase(SensorEntity):
    """Base class for BMS-scanned pack sensors.

    The class name is retained so existing entity unique IDs keep migrating
    cleanly, but the data now comes from battery_health.individual_batteries
    rather than local batteryBlocks.
    """

    _attr_has_entity_name = False
    metric_key = ""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, index: int, key: str, name: str
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._index = index
        self._metric_name = name
        self._attr_unique_id = f"{entry.entry_id}_pw{index + 1}_{key}"
        self._attr_suggested_object_id = f"powerwall_{index + 1}_{key}"
        self._attr_name = f"{self._label} {name}"

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def _health_data(self) -> dict[str, Any] | None:
        return (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("battery_health")
        )

    @property
    def _packs(self) -> list[dict[str, Any]]:
        return _powerwall_pack_data(self._health_data)

    @property
    def _label(self) -> str:
        packs = self._packs
        if self._index >= len(packs):
            return f"Powerwall {self._index + 1}"
        return _pack_label(packs, self._index)

    @property
    def _block(self) -> dict[str, Any] | None:
        packs = self._packs
        if self._index >= len(packs):
            return None
        return packs[self._index]

    @property
    def available(self) -> bool:
        return self._block is not None

    async def async_added_to_hass(self) -> None:
        """Refresh state when a new BMS health scan lands."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_battery_health_update_{self._entry.entry_id}",
                self._handle_battery_health_update,
            )
        )

    @callback
    def _handle_battery_health_update(self, data: dict[str, Any]) -> None:
        self._attr_name = f"{self._label} {self._metric_name}"
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pack = self._block
        if not pack:
            return {}

        is_expansion = bool(pack.get("isExpansion"))
        is_follower = bool(pack.get("isFollower"))
        role = pack.get("role") or (
            "expansion" if is_expansion else "follower" if is_follower else "leader"
        )
        attrs: dict[str, Any] = {
            "pack_index": self._index + 1,
            "pack_label": self._label,
            "pack_role": role,
            "is_expansion": is_expansion,
            "is_follower": is_follower,
        }
        serial = pack.get("serialNumber") or pack.get("serial_number")
        if serial:
            attrs["serial_number"] = serial
        physical_din = (
            pack.get("physicalDin") or pack.get("physical_din") or pack.get("din")
        )
        if physical_din:
            attrs["physical_din"] = physical_din
        bms_serial = pack.get("bmsSerialNumber") or pack.get("bms_serial_number")
        if bms_serial and bms_serial != serial:
            attrs["bms_serial_number"] = bms_serial

        full = _pack_float(
            pack, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh"
        )
        remaining = _pack_float(
            pack, "nominalEnergyRemainingWh", "nominal_energy_remaining_wh"
        )
        if full is not None:
            attrs["capacity_kwh"] = round(full / 1000.0, 2)
        if remaining is not None:
            attrs["energy_remaining_kwh"] = round(remaining / 1000.0, 2)
        if full and full > 0 and remaining is not None:
            attrs["soc_percent"] = round(remaining / full * 100.0, 1)

        health_data = self._health_data or {}
        if health_data.get("source"):
            attrs["source"] = health_data["source"]
        return attrs


class PowerwallBlockSocSensor(_PowerwallBlockSensorBase):
    metric_key = "soc"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "soc", "SOC")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        full = _pack_float(
            block, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh"
        )
        rem = _pack_float(
            block, "nominalEnergyRemainingWh", "nominal_energy_remaining_wh"
        )
        if full and rem is not None and full > 0:
            return round(rem / full * 100.0, 1)
        return None


class PowerwallBlockCurrentEnergySensor(_PowerwallBlockSensorBase):
    metric_key = "current_energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:battery-50"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "current_energy", "Current Energy")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        remaining = _pack_float(
            block,
            "nominalEnergyRemainingWh",
            "nominal_energy_remaining_wh",
        )
        return round(remaining / 1000.0, 2) if remaining is not None else None


class PowerwallBlockCapacitySensor(_PowerwallBlockSensorBase):
    metric_key = "capacity"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY_STORAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:battery-high"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "capacity", "Capacity")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        full = _pack_float(
            block, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh"
        )
        return round(full / 1000.0, 2) if full else None


class PowerwallBlockVoltageSensor(_PowerwallBlockSensorBase):
    metric_key = "voltage"
    _attr_native_unit_of_measurement = "V"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "voltage", "Voltage")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        v = _pack_float(
            block, "voltage_v", "voltage", "battery_voltage", "BMS_packVoltage"
        )
        return round(float(v), 1) if v is not None else None


class PowerwallBlockTemperatureSensor(_PowerwallBlockSensorBase):
    metric_key = "temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "temperature", "Temperature")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        value = _pack_float(
            block,
            "temperature_c",
            "temperature",
            "battery_temp",
            "BMS_maxCellTemp",
            "BMS_minCellTemp",
        )
        return round(value, 1) if value is not None else None


class PowerwallBlockSohSensor(_PowerwallBlockSensorBase):
    """State of Health: pack capacity vs nameplate. PW2 nameplate = 13.5 kWh."""

    metric_key = "soh"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:battery-heart"

    _NAMEPLATE_WH = 13500.0  # PW2 baseline; PW3 reports its own nominal

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, index: int) -> None:
        super().__init__(hass, entry, index, "soh", "State of Health")

    @property
    def native_value(self) -> Any:
        block = self._block
        if not block:
            return None
        full = _pack_float(
            block, "nominalFullPackEnergyWh", "nominal_full_pack_energy_wh"
        )
        if not full:
            return None
        return round(float(full) / self._NAMEPLATE_WH * 100.0, 1)


class PowerwallSolarStringVoltageSensor(SensorEntity):
    """Voltage for a single Powerwall DC-coupled solar string."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "V"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:solar-power-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        key: str,
        string_id: Any,
        label: str,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._key = key
        self._string_id = string_id
        self._label = label
        self._attr_unique_id = f"{entry.entry_id}_solar_string_{key}_voltage"
        self._attr_suggested_object_id = f"powerwall_solar_string_{key}_voltage"
        self._attr_name = f"Solar String {label} Voltage"

    @property
    def device_info(self):
        return powerwall_device_info(self._entry.entry_id)

    @property
    def _diagnostics(self) -> dict[str, Any] | None:
        return (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("solar_string_diagnostics")
        )

    @property
    def _reading(self) -> dict[str, Any] | None:
        strings = _solar_string_data(self._diagnostics)
        for index, reading in enumerate(strings):
            if (
                reading.get("id") == self._string_id
                or _solar_string_key(reading, index) == self._key
            ):
                return reading
        return None

    @property
    def available(self) -> bool:
        reading = self._reading
        return reading is not None and reading.get("voltage_v") is not None

    @property
    def native_value(self) -> Any:
        reading = self._reading
        if not reading:
            return None
        value = _pack_float(reading, "voltage_v")
        return round(value, 1) if value is not None else None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_solar_strings_update_{self._entry.entry_id}",
                self._handle_solar_strings_update,
            )
        )

    @callback
    def _handle_solar_strings_update(self, data: dict[str, Any]) -> None:
        reading = self._reading
        if reading:
            self._label = str(reading.get("label") or self._label)
            self._attr_name = f"Solar String {self._label} Voltage"
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reading = self._reading
        diagnostics = self._diagnostics or {}
        if not reading:
            return {
                "string_id": self._string_id,
                "source": diagnostics.get("source"),
                "last_scan": diagnostics.get("last_scan"),
            }

        attrs: dict[str, Any] = {
            "string_id": reading.get("id"),
            "string_label": reading.get("label"),
            "mppt": reading.get("mppt"),
            "connected": reading.get("connected"),
            "source": diagnostics.get("source"),
            "transport_source": diagnostics.get("transport_source"),
            "last_scan": diagnostics.get("last_scan"),
        }
        for attr_key in ("current_a", "power_w", "state", "device_id"):
            if reading.get(attr_key) is not None:
                attrs[attr_key] = reading.get(attr_key)

        string_id = reading.get("id")
        groups = diagnostics.get("groups") if isinstance(diagnostics, dict) else None
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                if string_id in (group.get("string_ids") or []):
                    attrs["group_id"] = group.get("id")
                    attrs["group_label"] = group.get("label")
                    if group.get("total_power_w") is not None:
                        attrs["group_total_power_w"] = group.get("total_power_w")
                    break
        return attrs


class BatteryHealthSensor(SensorEntity):
    """Sensor for battery health / state of health.

    Data sources:
    - Tesla: TEDAPI / Fleet API BMS scan (capacity-based, with per-battery breakdown)

    Shows battery health as a percentage. Tesla can be > 100% if batteries
    have more capacity than rated spec.
    """

    _attr_has_entity_name = True
    _attr_name = "Battery Health"
    _attr_icon = "mdi:battery-heart-variant"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator=None,
        battery_system: str = "tesla",
    ) -> None:
        """Initialize the sensor."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_BATTERY_HEALTH}"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_BATTERY_HEALTH}"

        # Energy coordinator for reading battery_soh (non-Tesla systems)
        self._coordinator = coordinator
        self._battery_system = battery_system
        self._soh_percent: float | None = None

        # Battery health data (from TEDAPI service call)
        self._original_capacity_wh: float | None = None
        self._current_capacity_wh: float | None = None
        self._degradation_percent: float | None = None
        self._battery_count: int | None = None
        self._scanned_at: str | None = None
        self._source: str | None = None
        self._individual_batteries: list | None = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    async def async_added_to_hass(self) -> None:
        """Subscribe to battery health updates when added to hass."""
        # Register for updates via dispatcher
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_battery_health_update_{self._entry.entry_id}",
                self._handle_battery_health_update,
            )
        )

        # Try to restore from storage
        domain_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        stored_health = domain_data.get("battery_health")
        if stored_health:
            self._original_capacity_wh = stored_health.get("original_capacity_wh")
            self._current_capacity_wh = stored_health.get("current_capacity_wh")
            self._degradation_percent = stored_health.get("degradation_percent")
            self._battery_count = stored_health.get("battery_count")
            self._scanned_at = stored_health.get("scanned_at")
            self._source = stored_health.get("source")
            self._individual_batteries = stored_health.get("individual_batteries")
            _LOGGER.info(
                f"Restored battery health from storage: {self._calculate_health_percent()}% health"
            )

        # For non-Tesla systems: listen to coordinator updates for battery_soh
        if self._coordinator is not None and self._battery_system != "tesla":
            self.async_on_remove(
                self._coordinator.async_add_listener(self._handle_coordinator_update)
            )
            # Read initial value if coordinator already has data
            if self._coordinator.data:
                self._handle_coordinator_update()

    @callback
    def _handle_battery_health_update(self, data: dict[str, Any]) -> None:
        """Handle battery health update from service call."""
        self._original_capacity_wh = data.get("original_capacity_wh")
        self._current_capacity_wh = data.get("current_capacity_wh")
        self._degradation_percent = data.get("degradation_percent")
        self._battery_count = data.get("battery_count")
        self._scanned_at = data.get("scanned_at")
        self._source = data.get("source")
        self._individual_batteries = data.get("individual_batteries")

        _LOGGER.info(
            f"Battery health updated: {self._calculate_health_percent()}% health, "
            f"{self._current_capacity_wh}Wh / {self._original_capacity_wh}Wh"
        )
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator data update — read battery_soh."""
        if not self._coordinator or not self._coordinator.data:
            return
        data = self._coordinator.data
        soh = data.get("battery_soh")
        if soh is not None and soh > 0:
            self._soh_percent = round(float(soh), 1)
            self.async_write_ha_state()

    def _calculate_health_percent(self) -> float | None:
        """Calculate health as percentage of original capacity."""
        if (
            self._current_capacity_wh is not None
            and self._original_capacity_wh is not None
            and self._original_capacity_wh > 0
        ):
            return round(
                (self._current_capacity_wh / self._original_capacity_wh) * 100, 1
            )
        return None

    @property
    def native_value(self) -> float | None:
        """Return the battery health as percentage of original capacity.

        Can be > 100% if batteries have more capacity than rated spec.
        Falls back to direct SOH% from coordinator for non-Tesla systems.
        """
        # TEDAPI capacity-based health (Tesla)
        health = self._calculate_health_percent()
        if health is not None:
            return health
        # Direct SOH from coordinator (Sungrow, Sigenergy, GoodWe)
        return self._soh_percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attributes = {}

        if self._original_capacity_wh is not None:
            attributes["original_capacity_wh"] = self._original_capacity_wh
            attributes["original_capacity_kwh"] = round(
                self._original_capacity_wh / 1000, 2
            )

        if self._current_capacity_wh is not None:
            attributes["current_capacity_wh"] = self._current_capacity_wh
            attributes["current_capacity_kwh"] = round(
                self._current_capacity_wh / 1000, 2
            )

        if self._degradation_percent is not None:
            attributes["degradation_percent"] = self._degradation_percent

        if self._battery_count is not None:
            attributes["battery_count"] = self._battery_count

        if self._scanned_at is not None:
            attributes["last_scan"] = self._scanned_at

        # Add individual battery data if available
        if self._individual_batteries:
            for i, battery in enumerate(self._individual_batteries):
                prefix = f"battery_{i + 1}"
                if isinstance(battery, dict):
                    attributes[f"{prefix}_label"] = _pack_label(
                        self._individual_batteries, i
                    )
                    din = (
                        battery.get("physicalDin")
                        or battery.get("physical_din")
                        or battery.get("din")
                    )
                    if din:
                        attributes[f"{prefix}_din"] = din
                    if battery.get("serialNumber"):
                        attributes[f"{prefix}_serial"] = battery.get("serialNumber")
                    bms_serial = battery.get("bmsSerialNumber") or battery.get(
                        "bms_serial_number"
                    )
                    if bms_serial:
                        attributes[f"{prefix}_bms_serial"] = bms_serial
                    if battery.get("nominalFullPackEnergyWh") is not None:
                        orig_wh = battery.get("nominalFullPackEnergyWh")
                        # Actual measured usable capacity of the battery
                        attributes[f"{prefix}_original_kwh"] = round(orig_wh / 1000, 2)
                    if battery.get("nominalEnergyRemainingWh") is not None:
                        curr_wh = battery.get("nominalEnergyRemainingWh")
                        # Current charge level (SOC)
                        attributes[f"{prefix}_current_kwh"] = round(curr_wh / 1000, 2)
                    # Calculate individual battery health as % of rated 13.5 kWh capacity
                    # nominalFullPackEnergyWh = actual measured capacity (can be > rated for new batteries)
                    # Health = actual_capacity / rated_capacity * 100
                    orig_wh = battery.get("nominalFullPackEnergyWh", 0)
                    if orig_wh > 0:
                        RATED_CAPACITY_WH = 13500  # 13.5 kWh per Powerwall
                        health = round((orig_wh / RATED_CAPACITY_WH) * 100, 1)
                        attributes[f"{prefix}_health_percent"] = health
                    if battery.get("isExpansion") is not None:
                        attributes[f"{prefix}_is_expansion"] = battery.get(
                            "isExpansion"
                        )
                    if battery.get("isFollower") is not None:
                        attributes[f"{prefix}_is_follower"] = battery.get("isFollower")
                    if battery.get("role") is not None:
                        attributes[f"{prefix}_role"] = battery.get("role")

        # Source attribution
        if self._original_capacity_wh is not None:
            attributes["source"] = self._source or "mobile_app_tedapi"
        elif self._soh_percent is not None:
            attributes["source"] = "inverter_modbus"
            attributes["state_of_health_percent"] = self._soh_percent

        return attributes


class BatteryModeSensor(SensorEntity):
    """Sensor for displaying battery mode (normal/force_charge/force_discharge).

    This sensor allows users to build automations that trigger when the battery
    mode changes, e.g., to exit force charge when electricity prices spike.

    States:
        - normal: Battery operating in normal self-consumption mode
        - force_charge: Battery is being force charged
        - force_discharge: Battery is being force discharged
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{SENSOR_TYPE_BATTERY_MODE}"
        self._attr_has_entity_name = True
        self._attr_name = "Battery Mode"
        # HA 2026.2.0+ requires lowercase suggested_object_id
        self._attr_suggested_object_id = f"power_sync_{SENSOR_TYPE_BATTERY_MODE}"
        self._attr_icon = "mdi:battery-sync"
        self._unsub_force_charge = None
        self._unsub_force_discharge = None
        self._unsub_hold_soc = None
        self._unsub_self_consumption = None

    @property
    def device_info(self):
        return family_device_info(self._entry.entry_id, SENSOR_FAMILY_BATTERY)

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        _LOGGER.info(
            "Battery mode sensor registered with entity_id: %s", self.entity_id
        )

        @callback
        def _handle_mode_update(data=None):
            """Handle battery mode update signal."""
            _LOGGER.debug("Battery mode sensor received update signal: %s", data)
            self.async_write_ha_state()

        # Subscribe to existing force charge/discharge signals
        self._unsub_force_charge = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_force_charge_state",
            _handle_mode_update,
        )
        self._unsub_force_discharge = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_force_discharge_state",
            _handle_mode_update,
        )
        self._unsub_hold_soc = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_hold_soc_state",
            _handle_mode_update,
        )
        self._unsub_self_consumption = async_dispatcher_connect(
            self.hass,
            f"{DOMAIN}_self_consumption_state",
            _handle_mode_update,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity is removed from hass."""
        if self._unsub_force_charge:
            self._unsub_force_charge()
        if self._unsub_force_discharge:
            self._unsub_force_discharge()
        if self._unsub_hold_soc:
            self._unsub_hold_soc()
        if self._unsub_self_consumption:
            self._unsub_self_consumption()

    def _get_current_mode(self) -> str:
        """Determine current battery mode from hass.data state."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

        # Check force charge state
        force_charge_state = entry_data.get("force_charge_state", {})
        if force_charge_state.get("active", False):
            return BATTERY_MODE_STATE_FORCE_CHARGE

        # Check force discharge state
        force_discharge_state = entry_data.get("force_discharge_state", {})
        if force_discharge_state.get("active", False):
            return BATTERY_MODE_STATE_FORCE_DISCHARGE

        # Check Hold SoC state (locks battery at current SoC for a duration)
        hold_soc_state = entry_data.get("hold_soc_state", {})
        if hold_soc_state.get("active", False):
            return BATTERY_MODE_STATE_HOLD_SOC

        # Check Self-Consumption override (duration-based manual override)
        self_consumption_state = entry_data.get("self_consumption_state", {})
        if self_consumption_state.get("active", False):
            return BATTERY_MODE_STATE_SELF_CONSUMPTION

        # Default to normal
        return BATTERY_MODE_STATE_NORMAL

    @property
    def native_value(self) -> str:
        """Return the current battery mode."""
        return self._get_current_mode()

    @property
    def icon(self) -> str:
        """Return the icon based on current mode."""
        mode = self._get_current_mode()
        if mode == BATTERY_MODE_STATE_FORCE_CHARGE:
            return "mdi:battery-charging"
        elif mode == BATTERY_MODE_STATE_FORCE_DISCHARGE:
            return "mdi:battery-arrow-down"
        elif mode == BATTERY_MODE_STATE_HOLD_SOC:
            return "mdi:battery-lock"
        elif mode == BATTERY_MODE_STATE_SELF_CONSUMPTION:
            return "mdi:home-lightning-bolt"
        return "mdi:battery-sync"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        force_charge_state = entry_data.get("force_charge_state", {})
        force_discharge_state = entry_data.get("force_discharge_state", {})
        hold_soc_state = entry_data.get("hold_soc_state", {})
        self_consumption_state = entry_data.get("self_consumption_state", {})

        mode = self._get_current_mode()

        attributes = {
            "mode": mode,
        }

        def _populate_timer_attrs(target: dict, src: dict) -> None:
            """Fill in expires_at + remaining_minutes from a state dict."""
            target["force_duration_minutes"] = src.get("duration", 0)
            if src.get("expires_at"):
                expires_at = src["expires_at"]
                target["expires_at"] = (
                    expires_at.isoformat()
                    if hasattr(expires_at, "isoformat")
                    else str(expires_at)
                )
                target["force_expires_at"] = target["expires_at"]
                from homeassistant.util import dt as dt_util

                remaining = (expires_at - dt_util.utcnow()).total_seconds() / 60
                target["remaining_minutes"] = max(0, int(remaining))
                target["force_remaining_minutes"] = target["remaining_minutes"]

        # Add mode-specific attributes
        if mode == BATTERY_MODE_STATE_FORCE_CHARGE:
            attributes["description"] = "Battery is being force charged"
            _populate_timer_attrs(attributes, force_charge_state)
        elif mode == BATTERY_MODE_STATE_FORCE_DISCHARGE:
            if force_discharge_state.get("command_status") == "entity_echo_unverified":
                attributes["description"] = (
                    "Force discharge command acknowledged; physical battery discharge is not verified"
                )
                attributes["command_status"] = "entity_echo"
                attributes["physical_effect"] = "unverified"
            else:
                attributes["description"] = "Battery is being force discharged"
            _populate_timer_attrs(attributes, force_discharge_state)
        elif mode == BATTERY_MODE_STATE_HOLD_SOC:
            attributes["description"] = "Battery locked at current state of charge"
            _populate_timer_attrs(attributes, hold_soc_state)
            if hold_soc_state.get("locked_soc") is not None:
                attributes["locked_soc"] = hold_soc_state["locked_soc"]
        elif mode == BATTERY_MODE_STATE_SELF_CONSUMPTION:
            attributes["description"] = "Pure self-consumption (TOU optimisation off)"
            _populate_timer_attrs(attributes, self_consumption_state)
            engaged_at = self_consumption_state.get("engaged_at")
            if engaged_at:
                attributes["engaged_at"] = (
                    engaged_at.isoformat()
                    if hasattr(engaged_at, "isoformat")
                    else str(engaged_at)
                )
        else:
            attributes["description"] = (
                "Battery operating in normal self-consumption mode"
            )

        return attributes
