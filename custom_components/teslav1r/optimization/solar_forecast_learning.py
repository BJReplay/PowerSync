"""Provider-neutral learning for solar forecast shortfall allowances.

The optimiser needs a conservative estimate of how much forecast solar may
fail to arrive before a Charge By Time deadline.  This module learns that
allowance from measured production without depending on provider-specific
payloads.  It deliberately records only aggregate energy and never mutates the
forecast used by the LP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from typing import Any


STORE_VERSION = 1
HISTORY_DAYS = 30
MIN_VALID_DAYS = 3
FULL_CONFIDENCE_DAYS = 7
MIN_VALID_HOURS = 3.0
MIN_FORECAST_KWH = 1.0
MIN_SIGNAL_KW = 0.2
MAX_SAMPLE_GAP_SECONDS = 15 * 60
MIN_SAMPLE_GAP_SECONDS = 30


def _finite_non_negative(value: Any) -> float | None:
    """Return a finite non-negative float, or None for invalid telemetry."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _quantile(values: list[float], fraction: float) -> float:
    """Return a linearly interpolated empirical quantile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


@dataclass
class _ActiveDay:
    """Energy accumulated from timestamp-aligned forecast/actual samples."""

    date: str
    forecast_kwh: float = 0.0
    actual_kwh: float = 0.0
    valid_hours: float = 0.0
    last_timestamp: str | None = None
    last_forecast_kw: float | None = None
    last_actual_kw: float | None = None
    last_valid: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "forecast_kwh": round(self.forecast_kwh, 6),
            "actual_kwh": round(self.actual_kwh, 6),
            "valid_hours": round(self.valid_hours, 6),
            "last_timestamp": self.last_timestamp,
            "last_forecast_kw": self.last_forecast_kw,
            "last_actual_kw": self.last_actual_kw,
            "last_valid": self.last_valid,
        }

    @classmethod
    def from_dict(cls, data: Any) -> _ActiveDay | None:
        if not isinstance(data, dict) or not isinstance(data.get("date"), str):
            return None
        active = cls(date=data["date"])
        for name in ("forecast_kwh", "actual_kwh", "valid_hours"):
            value = _finite_non_negative(data.get(name, 0.0))
            setattr(active, name, value if value is not None else 0.0)
        timestamp = data.get("last_timestamp")
        if isinstance(timestamp, str):
            try:
                datetime.fromisoformat(timestamp)
            except ValueError:
                timestamp = None
        else:
            timestamp = None
        active.last_timestamp = timestamp
        active.last_forecast_kw = _finite_non_negative(
            data.get("last_forecast_kw")
        )
        active.last_actual_kw = _finite_non_negative(data.get("last_actual_kw"))
        active.last_valid = bool(data.get("last_valid", False))
        return active


@dataclass
class SolarForecastLearner:
    """Learn one-sided daily forecast errors independently per provider."""

    histories: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    active_days: dict[str, _ActiveDay] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    last_observation: str | None = None

    @staticmethod
    def _canonical_source(source: Any) -> str | None:
        if not isinstance(source, str):
            return None
        source = source.strip().lower()
        return source or None

    def _finalize(self, source: str, active: _ActiveDay) -> None:
        if (
            active.valid_hours < MIN_VALID_HOURS
            or active.forecast_kwh < MIN_FORECAST_KWH
        ):
            self.skipped["insufficient_coverage"] = (
                self.skipped.get("insufficient_coverage", 0) + 1
            )
            return
        sample = {
            "date": active.date,
            "forecast_kwh": round(active.forecast_kwh, 4),
            "actual_kwh": round(active.actual_kwh, 4),
            "shortfall_kwh": round(
                max(0.0, active.forecast_kwh - active.actual_kwh), 4
            ),
            "valid_hours": round(active.valid_hours, 3),
        }
        history = self.histories.setdefault(source, [])
        history[:] = [item for item in history if item.get("date") != active.date]
        history.append(sample)
        history.sort(key=lambda item: str(item.get("date", "")))
        del history[:-HISTORY_DAYS]

    def observe(
        self,
        *,
        timestamp: datetime,
        source: str | None,
        forecast_kw: Any,
        actual_kw: Any,
        valid: bool,
        skip_reason: str | None = None,
    ) -> bool:
        """Record one timestamp-aligned power pair.

        Returns True when persisted state changed. Repeated calls with the same
        or an older timestamp are ignored, which prevents multiple forecast
        consumers from generating duplicate energy observations.
        """
        source_key = self._canonical_source(source)
        if source_key is None:
            return False
        forecast = _finite_non_negative(forecast_kw)
        actual = _finite_non_negative(actual_kw)
        valid = bool(valid and forecast is not None and actual is not None)
        day = timestamp.date().isoformat()
        active = self.active_days.get(source_key)
        if active is None or active.date != day:
            if active is not None:
                self._finalize(source_key, active)
            active = _ActiveDay(date=day)
            self.active_days[source_key] = active

        previous_time: datetime | None = None
        if active.last_timestamp:
            try:
                previous_time = datetime.fromisoformat(active.last_timestamp)
            except ValueError:
                previous_time = None
        if previous_time is not None and timestamp <= previous_time:
            return False

        if not valid:
            reason = skip_reason or "invalid_telemetry"
            self.skipped[reason] = self.skipped.get(reason, 0) + 1
        elif previous_time is not None and active.last_valid:
            elapsed_seconds = (timestamp - previous_time).total_seconds()
            if MIN_SAMPLE_GAP_SECONDS <= elapsed_seconds <= MAX_SAMPLE_GAP_SECONDS:
                previous_forecast = active.last_forecast_kw or 0.0
                previous_actual = active.last_actual_kw or 0.0
                signal_kw = max(
                    previous_forecast,
                    forecast or 0.0,
                    previous_actual,
                    actual or 0.0,
                )
                if signal_kw >= MIN_SIGNAL_KW:
                    hours = elapsed_seconds / 3600.0
                    active.forecast_kwh += (
                        (previous_forecast + (forecast or 0.0)) * 0.5 * hours
                    )
                    active.actual_kwh += (
                        (previous_actual + (actual or 0.0)) * 0.5 * hours
                    )
                    active.valid_hours += hours
            elif elapsed_seconds > MAX_SAMPLE_GAP_SECONDS:
                self.skipped["telemetry_gap"] = (
                    self.skipped.get("telemetry_gap", 0) + 1
                )

        active.last_timestamp = timestamp.isoformat()
        active.last_forecast_kw = forecast
        active.last_actual_kw = actual
        active.last_valid = valid
        self.last_observation = timestamp.isoformat()
        return True

    def valid_days(self, source: str | None) -> int:
        source_key = self._canonical_source(source)
        return len(self.histories.get(source_key or "", []))

    def confidence(self, source: str | None) -> float:
        """Return blend weight, retaining exact legacy behaviour initially."""
        days = self.valid_days(source)
        if days < MIN_VALID_DAYS:
            return 0.0
        span = max(1, FULL_CONFIDENCE_DAYS - MIN_VALID_DAYS + 1)
        return min(1.0, (days - MIN_VALID_DAYS + 1) / span)

    def allowance_kwh(self, source: str | None) -> float | None:
        """Return the provider's robust one-sided daily shortfall allowance."""
        source_key = self._canonical_source(source)
        history = self.histories.get(source_key or "", [])
        if len(history) < MIN_VALID_DAYS:
            return None
        shortfalls = [
            value
            for item in history[-HISTORY_DAYS:]
            if (value := _finite_non_negative(item.get("shortfall_kwh"))) is not None
        ]
        if len(shortfalls) < MIN_VALID_DAYS:
            return None
        return round(_quantile(shortfalls, 0.90), 3)

    def diagnostics(self, source: str | None) -> dict[str, Any]:
        source_key = self._canonical_source(source)
        days = self.valid_days(source_key)
        confidence = self.confidence(source_key)
        allowance = self.allowance_kwh(source_key)
        active = self.active_days.get(source_key or "")
        return {
            "store_version": STORE_VERSION,
            "active_source": source_key,
            "mode": (
                "legacy"
                if allowance is None
                else "learned" if confidence >= 1.0 else "blending"
            ),
            "allowance_kwh": allowance,
            "confidence": round(confidence, 3),
            "valid_days": days,
            "sample_count": days,
            "last_observation": self.last_observation,
            "active_day": active.date if active else None,
            "active_forecast_kwh": round(active.forecast_kwh, 3) if active else 0.0,
            "active_actual_kwh": round(active.actual_kwh, 3) if active else 0.0,
            "active_valid_hours": round(active.valid_hours, 3) if active else 0.0,
            "skipped": dict(sorted(self.skipped.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned, bounded Home Assistant Store payload."""
        return {
            "version": STORE_VERSION,
            "histories": {
                source: list(history[-HISTORY_DAYS:])
                for source, history in self.histories.items()
            },
            "active_days": {
                source: active.to_dict()
                for source, active in self.active_days.items()
            },
            "skipped": dict(self.skipped),
            "last_observation": self.last_observation,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SolarForecastLearner:
        """Restore valid state and safely ignore corrupt/unknown fields."""
        learner = cls()
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return learner
        histories = data.get("histories")
        if isinstance(histories, dict):
            for raw_source, raw_history in histories.items():
                source = learner._canonical_source(raw_source)
                if source is None or not isinstance(raw_history, list):
                    continue
                valid_samples: list[dict[str, Any]] = []
                for item in raw_history[-HISTORY_DAYS:]:
                    if not isinstance(item, dict) or not isinstance(
                        item.get("date"), str
                    ):
                        continue
                    forecast = _finite_non_negative(item.get("forecast_kwh"))
                    actual = _finite_non_negative(item.get("actual_kwh"))
                    shortfall = _finite_non_negative(item.get("shortfall_kwh"))
                    hours = _finite_non_negative(item.get("valid_hours"))
                    if None in (forecast, actual, shortfall, hours):
                        continue
                    valid_samples.append(
                        {
                            "date": item["date"],
                            "forecast_kwh": forecast,
                            "actual_kwh": actual,
                            "shortfall_kwh": shortfall,
                            "valid_hours": hours,
                        }
                    )
                learner.histories[source] = valid_samples[-HISTORY_DAYS:]
        active_days = data.get("active_days")
        if isinstance(active_days, dict):
            for raw_source, raw_active in active_days.items():
                source = learner._canonical_source(raw_source)
                active = _ActiveDay.from_dict(raw_active)
                if source and active:
                    learner.active_days[source] = active
        skipped = data.get("skipped")
        if isinstance(skipped, dict):
            learner.skipped = {
                str(key): int(value)
                for key, value in skipped.items()
                if isinstance(value, int) and value >= 0
            }
        if isinstance(data.get("last_observation"), str):
            learner.last_observation = data["last_observation"]
        return learner
