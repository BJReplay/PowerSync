"""Normalisation helpers for persisted demand-charge configuration."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any


DEMAND_CHARGE_DAYS_ALL = "All Days"
DEMAND_CHARGE_DAYS_WEEKDAYS = "Weekdays Only"
DEMAND_CHARGE_DAYS_WEEKENDS = "Weekends Only"


def normalize_demand_charge_days(value: Any) -> str:
    """Return a current demand-charge day selector value.

    Older releases stored lower-case values such as ``all`` and some API
    surfaces exposed weekday-number lists.  Treat unknown legacy values as
    all-days, which matches the historical runtime behaviour.
    """
    if isinstance(value, str):
        normalized = " ".join(value.strip().lower().replace("_", " ").split())
        aliases = {
            "all": DEMAND_CHARGE_DAYS_ALL,
            "all day": DEMAND_CHARGE_DAYS_ALL,
            "all days": DEMAND_CHARGE_DAYS_ALL,
            "weekday": DEMAND_CHARGE_DAYS_WEEKDAYS,
            "weekdays": DEMAND_CHARGE_DAYS_WEEKDAYS,
            "weekdays only": DEMAND_CHARGE_DAYS_WEEKDAYS,
            "weekend": DEMAND_CHARGE_DAYS_WEEKENDS,
            "weekends": DEMAND_CHARGE_DAYS_WEEKENDS,
            "weekends only": DEMAND_CHARGE_DAYS_WEEKENDS,
        }
        return aliases.get(normalized, DEMAND_CHARGE_DAYS_ALL)

    if isinstance(value, Collection) and not isinstance(value, (bytes, bytearray)):
        try:
            weekdays = {int(day) for day in value}
        except (TypeError, ValueError):
            return DEMAND_CHARGE_DAYS_ALL
        if weekdays == set(range(5)):
            return DEMAND_CHARGE_DAYS_WEEKDAYS
        if weekdays == {5, 6}:
            return DEMAND_CHARGE_DAYS_WEEKENDS

    return DEMAND_CHARGE_DAYS_ALL


def normalize_demand_charge_billing_day(value: Any) -> int:
    """Return a safe integer billing-cycle reset day."""
    try:
        billing_day = int(float(value))
    except (TypeError, ValueError, OverflowError):
        billing_day = 1
    return max(1, min(31, billing_day))
