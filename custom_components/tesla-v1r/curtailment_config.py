"""Shared configuration helpers for export-price curtailment."""

from __future__ import annotations

import math
from typing import Any

from .const import (
    BATTERY_SYSTEM_ALPHAESS,
    BATTERY_SYSTEM_SIGENERGY,
    BATTERY_SYSTEM_SOLAREDGE,
    CONF_ALPHAESS_DC_CURTAILMENT_ENABLED,
    CONF_BATTERY_CURTAILMENT_ENABLED,
    CONF_BATTERY_SYSTEM,
    CONF_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
    CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
    CONF_SOLAREDGE_DC_CURTAILMENT_ENABLED,
    DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
)
from .tariff_utils import with_hysteresis


CURTAILMENT_HYSTERESIS_CENTS = 0.2
MIN_CURTAILMENT_EXPORT_THRESHOLD_CENTS = -100.0
MAX_CURTAILMENT_EXPORT_THRESHOLD_CENTS = 200.0


def get_effective_solar_curtailment_configuration(entry: Any) -> tuple[bool, bool]:
    """Return ``(battery_export, direct_dc)`` curtailment permissions.

    Direct DC curtailment is deliberately limited to the selected brand's
    existing explicit opt-in.  In particular, a stale option for another
    battery brand, or a legacy entry without an explicit battery-system
    selection, must not turn a control path on after an upgrade.
    """
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}

    def value(key: str, default: Any = False) -> Any:
        return options.get(key, data.get(key, default))

    battery_export_enabled = bool(value(CONF_BATTERY_CURTAILMENT_ENABLED))
    direct_dc_setting = {
        BATTERY_SYSTEM_SIGENERGY: CONF_SIGENERGY_DC_CURTAILMENT_ENABLED,
        BATTERY_SYSTEM_ALPHAESS: CONF_ALPHAESS_DC_CURTAILMENT_ENABLED,
        BATTERY_SYSTEM_SOLAREDGE: CONF_SOLAREDGE_DC_CURTAILMENT_ENABLED,
    }.get(value(CONF_BATTERY_SYSTEM))
    direct_dc_enabled = bool(value(direct_dc_setting)) if direct_dc_setting else False
    return battery_export_enabled, direct_dc_enabled


def normalize_curtailment_export_threshold_cents(value: Any) -> float:
    """Return a finite, bounded curtailment entry threshold in c/kWh."""
    if isinstance(value, bool):
        return DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS
    if not math.isfinite(parsed):
        return DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS
    return max(
        MIN_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
        min(MAX_CURTAILMENT_EXPORT_THRESHOLD_CENTS, parsed),
    )


def get_curtailment_price_thresholds(entry: Any) -> tuple[float, float]:
    """Return configured enter/exit thresholds in c/kWh.

    The user selects the economic entry boundary. The existing 0.2 c/kWh
    deadband follows a non-default boundary so changing a custom entry
    threshold cannot silently retain an old release point.
    """
    options = getattr(entry, "options", {}) or {}
    data = getattr(entry, "data", {}) or {}
    raw = options.get(
        CONF_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
        data.get(
            CONF_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
            DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS,
        ),
    )
    enter = normalize_curtailment_export_threshold_cents(raw)
    return enter, enter + CURTAILMENT_HYSTERESIS_CENTS


def export_earnings_are_uneconomic(
    export_earnings_cents: float,
    was_active: bool,
    entry: Any,
) -> bool:
    """Return whether export earnings require automatic curtailment.

    The standard zero threshold is intentionally strict: only genuinely
    negative export earnings curtail, and an active curtailment releases at
    exactly 0 c/kWh. Existing non-zero user-selected thresholds retain their
    established hysteresis behavior.
    """
    enter, exit_ = get_curtailment_price_thresholds(entry)
    if enter == DEFAULT_CURTAILMENT_EXPORT_THRESHOLD_CENTS:
        return export_earnings_cents < 0.0
    return with_hysteresis(
        export_earnings_cents,
        was_active,
        enter_threshold=enter,
        exit_threshold=exit_,
    )
