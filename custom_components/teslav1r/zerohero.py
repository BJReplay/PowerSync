"""GloBird ZeroHero tariff settlement helpers.

ZeroHero is not a simple TOU export rate.  The Super Export rate applies only
to a capped daily export bucket, and the daily credit depends on keeping import
under a small allowance during the evening window.

ZeroCharge's configured cap is a daily-average allowance.  It forms a single
pool for each local calendar month; the helper functions below deliberately use
the timestamp's local calendar rather than a fixed 24-hour window.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

GLOBIRD_PLAN_NOT_ZEROHERO = "not_zerohero"
GLOBIRD_PLAN_FOUR4FREE = "four4free"
GLOBIRD_PLAN_FOUR4FREE_CUSTOM = "four4free_custom"
GLOBIRD_PLAN_ZEROHERO_JUL_2026 = "zerohero_jul_2026"
GLOBIRD_PLAN_ZEROHERO_PRE_JUL_2026 = "zerohero_pre_jul_2026"
GLOBIRD_PLAN_ZEROHERO_CURRENT = "zerohero_current"
GLOBIRD_PLAN_ZEROHERO_LEGACY = "zerohero_legacy"
GLOBIRD_PLAN_ZEROHERO_CUSTOM = "zerohero_custom"

_CONF_GLOBIRD_PLAN = "globird_plan"
_CONF_START = "globird_zerohero_start"
_CONF_END = "globird_zerohero_end"
_CONF_EXPORT_CAP = "globird_zerohero_export_cap_kwh"
_CONF_EXPORT_RATE = "globird_zerohero_super_export_rate"
_CONF_CREDIT = "globird_zerohero_credit_amount"
_CONF_IMPORT_LIMIT = "globird_zerohero_import_limit_kw"
_CONF_ZEROCHARGE_START = "globird_zerocharge_start"
_CONF_ZEROCHARGE_END = "globird_zerocharge_end"
_CONF_ZEROCHARGE_IMPORT_CAP = "globird_zerocharge_import_cap_kwh"


@dataclass(frozen=True)
class ZeroHeroConfig:
    """Resolved ZeroHero plan settings."""

    plan: str
    start: str
    end: str
    export_cap_kwh: float
    super_export_rate: float
    credit_amount: float
    import_limit_kw: float
    zerocharge_start: str | None = None
    zerocharge_end: str | None = None
    zerocharge_import_cap_kwh: float = 0.0

    @property
    def window_hours(self) -> float:
        minutes = _window_duration_minutes(self.start, self.end)
        return max(0.0, minutes / 60.0)

    @property
    def import_allowance_kwh(self) -> float:
        return max(0.0, self.import_limit_kw * self.window_hours)

    @property
    def zerocharge_enabled(self) -> bool:
        return (
            bool(self.zerocharge_start)
            and bool(self.zerocharge_end)
            and self.zerocharge_import_cap_kwh > 0
        )


@dataclass
class ZeroHeroSettlement:
    """Settlement result for a series of intervals."""

    export_earnings: float = 0.0
    base_export_earnings: float = 0.0
    bonus_export_earnings: float = 0.0
    bonus_export_kwh: float = 0.0
    total_export_kwh: float = 0.0
    import_window_kwh: float = 0.0
    credit_value: float = 0.0
    credit_status: str = "disabled"
    zerocharge_import_kwh: float = 0.0
    zerocharge_credit_value: float = 0.0


ZEROHERO_PRESETS: dict[str, dict[str, Any]] = {
    GLOBIRD_PLAN_ZEROHERO_JUL_2026: {
        "start": "18:00",
        "end": "21:00",
        "export_cap_kwh": 15.0,
        "super_export_rate": 0.10,
        "credit_amount": 1.0,
        "import_limit_kw": 0.03,
        "zerocharge_start": "12:00",
        "zerocharge_end": "15:00",
        "zerocharge_import_cap_kwh": 50.0,
    },
    GLOBIRD_PLAN_ZEROHERO_PRE_JUL_2026: {
        "start": "18:00",
        "end": "21:00",
        "export_cap_kwh": 15.0,
        "super_export_rate": 0.10,
        "credit_amount": 1.0,
        "import_limit_kw": 0.03,
        "zerocharge_start": "11:00",
        "zerocharge_end": "14:00",
        "zerocharge_import_cap_kwh": 50.0,
    },
    GLOBIRD_PLAN_ZEROHERO_CURRENT: {
        "start": "18:00",
        "end": "21:00",
        "export_cap_kwh": 15.0,
        "super_export_rate": 0.15,
        "credit_amount": 1.0,
        "import_limit_kw": 0.03,
        "zerocharge_start": None,
        "zerocharge_end": None,
        "zerocharge_import_cap_kwh": 0.0,
    },
    GLOBIRD_PLAN_ZEROHERO_LEGACY: {
        "start": "18:00",
        "end": "20:00",
        "export_cap_kwh": 10.0,
        "super_export_rate": 0.15,
        "credit_amount": 1.0,
        "import_limit_kw": 0.03,
        "zerocharge_start": None,
        "zerocharge_end": None,
        "zerocharge_import_cap_kwh": 0.0,
    },
}


def _canonical_tariff_rate(value: Any) -> float | None:
    """Return a tariff rate in dollars/kWh from common API representations."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0:
        return None
    return rate / 100.0 if rate > 2.0 else rate


def _tariff_period_entries(period_data: Any) -> list[dict[str, Any]]:
    """Return Tesla-style period entries from flat or nested TOU data."""
    if isinstance(period_data, list):
        return [entry for entry in period_data if isinstance(entry, dict)]
    if not isinstance(period_data, dict):
        return []
    periods = period_data.get("periods")
    if isinstance(periods, list):
        return [entry for entry in periods if isinstance(entry, dict)]
    if any(key in period_data for key in ("fromHour", "startHour", "start_hour")):
        return [period_data]
    return []


def _tariff_period_bounds(entry: dict[str, Any]) -> tuple[int, int] | None:
    """Return (start_minute, end_minute) for a Tesla-style period entry."""
    start_hour = entry.get("fromHour", entry.get("startHour", entry.get("start_hour")))
    end_hour = entry.get("toHour", entry.get("endHour", entry.get("end_hour")))
    start_minute = entry.get(
        "fromMinute", entry.get("startMinute", entry.get("start_minute", 0))
    )
    end_minute = entry.get(
        "toMinute", entry.get("endMinute", entry.get("end_minute", 0))
    )
    try:
        start = int(start_hour) * 60 + int(start_minute or 0)
        end = int(end_hour) * 60 + int(end_minute or 0)
    except (TypeError, ValueError):
        return None
    if not (0 <= start <= 24 * 60 and 0 <= end <= 24 * 60):
        return None
    return start, end


def infer_globird_plan_from_tariff(
    tariff_schedule: dict[str, Any] | None,
) -> str | None:
    """Infer a known GloBird plan identity from an authoritative tariff source.

    FOUR4FREE terms vary by account and date, so its name identifies the plan
    while the account tariff remains authoritative for prices and windows.
    ZeroHero additionally requires matching rates/windows before PowerSync
    activates its capped export and no-import-credit settlement rules.
    """
    if not isinstance(tariff_schedule, dict):
        return None
    plan_name = "".join(
        character
        for character in str(tariff_schedule.get("plan_name", "")).casefold()
        if character.isalnum()
    )
    if "four4free" in plan_name or "fourforfree" in plan_name:
        return GLOBIRD_PLAN_FOUR4FREE
    if "zerohero" not in plan_name:
        return None

    buy_rates = tariff_schedule.get("buy_rates")
    sell_rates = tariff_schedule.get("sell_rates")
    tou_periods = tariff_schedule.get("tou_periods")
    if not isinstance(buy_rates, dict) or not isinstance(sell_rates, dict):
        return None
    if not isinstance(tou_periods, dict):
        return None

    normalized_sell = {
        str(period): _canonical_tariff_rate(rate)
        for period, rate in sell_rates.items()
    }
    positive_sell = [
        rate for rate in normalized_sell.values() if rate is not None and rate > 0.001
    ]
    if not positive_sell:
        return None
    super_export_rate = max(positive_sell)

    export_windows: set[tuple[int, int]] = set()
    for period_name, rate in normalized_sell.items():
        if rate is None or abs(rate - super_export_rate) > 0.001:
            continue
        for entry in _tariff_period_entries(tou_periods.get(period_name)):
            bounds = _tariff_period_bounds(entry)
            if bounds is not None:
                export_windows.add(bounds)

    if abs(super_export_rate - 0.10) <= 0.001:
        if (18 * 60, 21 * 60) not in export_windows:
            return None
        free_windows: set[tuple[int, int]] = set()
        for period_name, rate_value in buy_rates.items():
            rate = _canonical_tariff_rate(rate_value)
            if rate is None or rate > 0.001:
                continue
            for entry in _tariff_period_entries(tou_periods.get(period_name)):
                bounds = _tariff_period_bounds(entry)
                if bounds is not None:
                    free_windows.add(bounds)
        if (11 * 60, 14 * 60) in free_windows:
            return GLOBIRD_PLAN_ZEROHERO_PRE_JUL_2026
        if (12 * 60, 15 * 60) in free_windows:
            return GLOBIRD_PLAN_ZEROHERO_JUL_2026
        return None

    if abs(super_export_rate - 0.15) <= 0.001:
        if (18 * 60, 20 * 60) in export_windows:
            return GLOBIRD_PLAN_ZEROHERO_LEGACY
        if (18 * 60, 21 * 60) in export_windows:
            return GLOBIRD_PLAN_ZEROHERO_CURRENT
    return None


def infer_zerohero_plan_from_tariff(
    tariff_schedule: dict[str, Any] | None,
) -> str | None:
    """Infer only ZeroHero settlement presets from a Tesla tariff."""
    plan = infer_globird_plan_from_tariff(tariff_schedule)
    if plan == GLOBIRD_PLAN_FOUR4FREE:
        return None
    return plan


def zerohero_config_from_settings(settings: dict[str, Any] | None) -> ZeroHeroConfig | None:
    """Return a resolved ZeroHero config from config entry data/options."""
    settings = settings or {}
    plan = settings.get(_CONF_GLOBIRD_PLAN, GLOBIRD_PLAN_NOT_ZEROHERO)
    if plan in (
        None,
        "",
        GLOBIRD_PLAN_NOT_ZEROHERO,
        GLOBIRD_PLAN_FOUR4FREE,
        GLOBIRD_PLAN_FOUR4FREE_CUSTOM,
    ):
        return None

    preset = ZEROHERO_PRESETS.get(plan, ZEROHERO_PRESETS[GLOBIRD_PLAN_ZEROHERO_CURRENT])
    if plan == GLOBIRD_PLAN_ZEROHERO_CUSTOM:
        start = _string_setting(settings.get(_CONF_START), preset["start"])
        end = _string_setting(settings.get(_CONF_END), preset["end"])
        export_cap = _float_setting(settings.get(_CONF_EXPORT_CAP), preset["export_cap_kwh"])
        super_rate = _cents_or_dollars(settings.get(_CONF_EXPORT_RATE), preset["super_export_rate"])
        credit = _float_setting(settings.get(_CONF_CREDIT), preset["credit_amount"])
        import_limit = _float_setting(settings.get(_CONF_IMPORT_LIMIT), preset["import_limit_kw"])
        zerocharge_supplied = any(
            key in settings
            for key in (
                _CONF_ZEROCHARGE_START,
                _CONF_ZEROCHARGE_END,
                _CONF_ZEROCHARGE_IMPORT_CAP,
            )
        )
        if zerocharge_supplied:
            zerocharge_start = _string_setting(
                settings.get(_CONF_ZEROCHARGE_START),
                ZEROHERO_PRESETS[GLOBIRD_PLAN_ZEROHERO_JUL_2026]["zerocharge_start"],
            )
            zerocharge_end = _string_setting(
                settings.get(_CONF_ZEROCHARGE_END),
                ZEROHERO_PRESETS[GLOBIRD_PLAN_ZEROHERO_JUL_2026]["zerocharge_end"],
            )
            zerocharge_cap = _float_setting(
                settings.get(_CONF_ZEROCHARGE_IMPORT_CAP),
                ZEROHERO_PRESETS[GLOBIRD_PLAN_ZEROHERO_JUL_2026]["zerocharge_import_cap_kwh"],
            )
        else:
            zerocharge_start = None
            zerocharge_end = None
            zerocharge_cap = 0.0
    else:
        start = preset["start"]
        end = preset["end"]
        export_cap = preset["export_cap_kwh"]
        super_rate = preset["super_export_rate"]
        credit = preset["credit_amount"]
        import_limit = preset["import_limit_kw"]
        zerocharge_start = preset.get("zerocharge_start")
        zerocharge_end = preset.get("zerocharge_end")
        zerocharge_cap = preset.get("zerocharge_import_cap_kwh", 0.0)

    if export_cap <= 0 or super_rate <= 0:
        return None

    return ZeroHeroConfig(
        plan=plan,
        start=start,
        end=end,
        export_cap_kwh=max(0.0, export_cap),
        super_export_rate=max(0.0, super_rate),
        credit_amount=max(0.0, credit),
        import_limit_kw=max(0.0, import_limit),
        zerocharge_start=zerocharge_start,
        zerocharge_end=zerocharge_end,
        zerocharge_import_cap_kwh=max(0.0, float(zerocharge_cap or 0.0)),
    )


def zerohero_plan_from_entry(
    entry: Any | None,
    tariff_schedule: dict[str, Any] | None = None,
) -> str:
    """Resolve the configured plan, using a definitive Tesla tariff as fallback."""
    if entry is None:
        return GLOBIRD_PLAN_NOT_ZEROHERO
    data = dict(getattr(entry, "data", {}) or {})
    data.update(getattr(entry, "options", {}) or {})
    configured = data.get(_CONF_GLOBIRD_PLAN, GLOBIRD_PLAN_NOT_ZEROHERO)
    if configured not in (None, "", GLOBIRD_PLAN_NOT_ZEROHERO):
        return str(configured)
    return infer_globird_plan_from_tariff(tariff_schedule) or GLOBIRD_PLAN_NOT_ZEROHERO


def zerohero_config_from_entry(
    entry: Any | None,
    tariff_schedule: dict[str, Any] | None = None,
) -> ZeroHeroConfig | None:
    """Resolve ZeroHero settings from a Home Assistant config entry and tariff."""
    if entry is None:
        return None
    data = dict(getattr(entry, "data", {}) or {})
    data.update(getattr(entry, "options", {}) or {})
    data[_CONF_GLOBIRD_PLAN] = zerohero_plan_from_entry(entry, tariff_schedule)
    return zerohero_config_from_settings(data)


def zerohero_is_in_window(ts: datetime, config: ZeroHeroConfig) -> bool:
    """Return True when a timestamp is in the configured local window."""
    return _is_in_window(ts, config.start, config.end)


def zerocharge_is_in_window(ts: datetime, config: ZeroHeroConfig) -> bool:
    """Return True when a timestamp is in the configured ZeroCharge window."""
    if not config.zerocharge_enabled:
        return False
    return _is_in_window(ts, config.zerocharge_start, config.zerocharge_end)


def zerocharge_period_key(ts: datetime) -> str:
    """Return the local calendar month owning a ZeroCharge allowance."""
    return ts.strftime("%Y-%m")


def zerocharge_monthly_cap_kwh(
    config: ZeroHeroConfig,
    ts: datetime | str,
) -> float:
    """Return the ZeroCharge pool for the local calendar month.

    ``zerocharge_import_cap_kwh`` remains the configured daily-average value
    for compatibility with existing settings and status keys.  The contract is
    now a calendar-month pool, so February and 31-day months naturally receive
    different total allowances.
    """
    if isinstance(ts, str):
        try:
            year, month = (int(value) for value in ts.split("-", 1))
        except (TypeError, ValueError):
            return 0.0
    else:
        year, month = ts.year, ts.month
    try:
        days = calendar.monthrange(year, month)[1]
    except ValueError:
        return 0.0
    return max(0.0, float(config.zerocharge_import_cap_kwh)) * days


# Short alias useful to callers that describe the allowance as a month cap.
zerocharge_month_cap_kwh = zerocharge_monthly_cap_kwh


def _is_in_window(ts: datetime, start_value: str, end_value: str) -> bool:
    """Return True when a timestamp is inside a local HH:MM window."""
    minute = ts.hour * 60 + ts.minute
    start = _hhmm_to_minutes(start_value)
    end = _hhmm_to_minutes(end_value)
    if end <= start:
        return minute >= start or minute < end
    return start <= minute < end


def settle_zerocharge_imports(
    config: ZeroHeroConfig | None,
    timestamps: list[datetime],
    import_kwh: list[float],
    import_prices: list[float],
    *,
    initial_import_kwh: float = 0.0,
    initial_period_key: str | None = None,
) -> tuple[float, float]:
    """Return (month import kWh, import credit value) for ZeroCharge imports.

    Imports are counted in every eligible window, including house and battery
    imports.  A forecast can span a month boundary, so settlement keeps a
    separate running pool for each local ``YYYY-MM`` period.  The scalar import
    result remains backwards compatible for the runtime's current-period
    accumulator and is the aggregate of all periods when a forecast spans a
    boundary.
    """
    if config is None or not config.zerocharge_enabled:
        return max(0.0, initial_import_kwh), 0.0

    initial_used = max(0.0, initial_import_kwh)
    if not timestamps:
        return initial_used, 0.0
    used_by_period: dict[str, float] = {}
    if initial_used > 0:
        seed_period = initial_period_key
        if seed_period is None and timestamps:
            seed_period = zerocharge_period_key(timestamps[0])
        if seed_period is not None:
            used_by_period[seed_period] = initial_used

    credit = 0.0
    for idx, ts in enumerate(timestamps):
        if not zerocharge_is_in_window(ts, config):
            continue
        period = zerocharge_period_key(ts)
        imported = max(0.0, import_kwh[idx] if idx < len(import_kwh) else 0.0)
        price = max(0.0, import_prices[idx] if idx < len(import_prices) else 0.0)
        used = used_by_period.get(period, 0.0)
        remaining = max(0.0, zerocharge_monthly_cap_kwh(config, ts) - used)
        eligible = min(imported, remaining)
        used += imported
        used_by_period[period] = used
        credit += eligible * price
    return sum(used_by_period.values()), credit


def zerohero_window_end_for(ts: datetime, config: ZeroHeroConfig) -> datetime:
    """Return the local datetime at which today's ZeroHero window ends."""
    end_minutes = _hhmm_to_minutes(config.end)
    start_minutes = _hhmm_to_minutes(config.start)
    end_dt = ts.replace(
        hour=end_minutes // 60,
        minute=end_minutes % 60,
        second=0,
        microsecond=0,
    )
    if end_minutes <= start_minutes and (ts.hour * 60 + ts.minute) >= start_minutes:
        end_dt += timedelta(days=1)
    return end_dt


def zerohero_credit_status(
    config: ZeroHeroConfig | None,
    now: datetime,
    import_window_kwh: float,
    credit_applied: bool = False,
) -> str:
    """Return a user-facing ZeroHero credit state."""
    if config is None:
        return "disabled"
    allowance = config.import_allowance_kwh
    if import_window_kwh > allowance + 1e-6:
        return "lost"
    if credit_applied:
        return "earned"
    if now >= zerohero_window_end_for(now, config):
        return "earned"
    if allowance > 0 and import_window_kwh >= allowance * 0.8:
        return "at_risk"
    return "eligible"


def settle_zerohero_series(
    config: ZeroHeroConfig | None,
    timestamps: list[datetime],
    import_kwh: list[float],
    export_kwh: list[float],
    base_export_prices: list[float],
    *,
    initial_bonus_kwh: float = 0.0,
    initial_import_window_kwh: float = 0.0,
    credit_already_applied: bool = False,
    include_credit: bool = False,
) -> ZeroHeroSettlement:
    """Settle export and daily credit value for a series of intervals."""
    result = ZeroHeroSettlement()
    if config is None:
        for idx, export in enumerate(export_kwh):
            price = base_export_prices[idx] if idx < len(base_export_prices) else 0.0
            result.export_earnings += max(0.0, export) * max(0.0, price)
            result.base_export_earnings = result.export_earnings
            result.total_export_kwh += max(0.0, export)
        return result

    bonus_used = max(0.0, initial_bonus_kwh)
    window_import = max(0.0, initial_import_window_kwh)
    for idx, ts in enumerate(timestamps):
        export = max(0.0, export_kwh[idx] if idx < len(export_kwh) else 0.0)
        imported = max(0.0, import_kwh[idx] if idx < len(import_kwh) else 0.0)
        base_price = max(
            0.0,
            base_export_prices[idx] if idx < len(base_export_prices) else 0.0,
        )
        base_earnings = export * base_price
        result.base_export_earnings += base_earnings
        result.total_export_kwh += export

        if zerohero_is_in_window(ts, config):
            window_import += imported
            remaining = max(0.0, config.export_cap_kwh - bonus_used)
            bonus_kwh = min(export, remaining)
            topup = max(0.0, config.super_export_rate - base_price)
            result.bonus_export_kwh += bonus_kwh
            result.bonus_export_earnings += bonus_kwh * topup
            bonus_used += bonus_kwh

    result.import_window_kwh = window_import
    result.export_earnings = (
        result.base_export_earnings + result.bonus_export_earnings
    )
    result.credit_status = zerohero_credit_status(
        config,
        timestamps[-1] if timestamps else datetime.now(),
        window_import,
        credit_already_applied,
    )
    if include_credit and not credit_already_applied and window_import <= config.import_allowance_kwh + 1e-6:
        result.credit_value = config.credit_amount
    return result


def _string_setting(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _float_setting(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _cents_or_dollars(value: Any, default: float) -> float:
    parsed = _float_setting(value, default)
    if parsed > 1.0:
        return parsed / 100.0
    return parsed


def _hhmm_to_minutes(value: str) -> int:
    try:
        hour, minute = value.split(":", 1)
        h = int(hour)
        m = int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (AttributeError, ValueError):
        pass
    return 0


def _window_duration_minutes(start: str, end: str) -> int:
    start_min = _hhmm_to_minutes(start)
    end_min = _hhmm_to_minutes(end)
    if end_min <= start_min:
        end_min += 24 * 60
    return end_min - start_min
