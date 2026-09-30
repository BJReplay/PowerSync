"""Sigenergy power-flow normalization helpers."""

from __future__ import annotations

from typing import Any


EVDC_STATE_CHARGING = 0x03
EVDC_STATE_DISCHARGING = 0x08


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_evdc_power_kw(
    power_kw: Any,
    *,
    raw_state: int | None = None,
    status: str | None = None,
) -> float | None:
    """Return signed EVDC power: positive charging, negative V2X discharge."""
    if power_kw is None:
        return None

    value = _as_float(power_kw)
    status_key = str(status or "").strip().lower()
    if raw_state == EVDC_STATE_DISCHARGING or status_key == "discharging":
        return -abs(value)
    if raw_state == EVDC_STATE_CHARGING or status_key == "charging":
        return abs(value)
    return value


def sigenergy_home_load_kw(
    *,
    solar_kw: Any,
    grid_kw: Any,
    battery_kw: Any,
    evdc_power_kw: Any = 0.0,
    external_ev_power_kw: Any = 0.0,
) -> float:
    """Return home/site load excluding separately measured EV power.

    The Sigenergy balance-derived load is home load plus EVDC net power. EVDC
    charging is positive demand and V2X discharge is negative supply, so
    subtracting the signed EVDC branch leaves home/site load. Positive external
    Tesla power is also behind the site meter and is rendered as its own branch.
    """
    balance_kw = _as_float(solar_kw) + _as_float(grid_kw) + _as_float(battery_kw)
    home_kw = (
        balance_kw
        - _as_float(evdc_power_kw)
        - max(0.0, _as_float(external_ev_power_kw))
    )
    return max(0.0, home_kw)
