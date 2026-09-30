"""Pure accounting model for the Cost Neutral optimiser mode."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class CostNeutralBudget:
    """Immutable local-day inputs used to cap discretionary export earnings."""

    supply_charge: float
    measured_import_cost: float
    measured_export_earnings: float
    forecast_import_cost: float
    forecast_natural_export_earnings: float

    @property
    def fixed_cost_allowance(self) -> float:
        """Return the local-day balance before replaceable forecast imports."""
        return (
            self.supply_charge
            + self.measured_import_cost
            - self.measured_export_earnings
            - self.forecast_natural_export_earnings
        )

    @property
    def base_projected_cost(self) -> float:
        return self.fixed_cost_allowance + self.forecast_import_cost

    @property
    def battery_export_earnings_cap(self) -> float:
        return max(0.0, self.base_projected_cost)


@dataclass(frozen=True)
class CostNeutralPlan:
    """Date-partitioned Cost Neutral inputs shared by every solve path."""

    day_ids: list[str | None]
    earnings_caps_by_day: dict[str, float]
    forecast_import_costs_by_day: dict[str, float]
    fixed_cost_allowances_by_day: dict[str, float]
    current_day: str | None = None
    timezone: str | None = None
    settlement_import_prices: list[float] = field(default_factory=list)
    settlement_export_prices: list[float] = field(default_factory=list)

    def normalized(self, length: int) -> "CostNeutralPlan":
        """Return finite, aligned values suitable for optimizer constraints."""

        day_ids = [
            str(value) if value is not None else None
            for value in self.day_ids[:length]
        ]
        if len(day_ids) < length:
            day_ids.extend([None] * (length - len(day_ids)))

        def _finite_map(
            values: dict[str, Any],
            *,
            nonnegative: bool,
        ) -> dict[str, float]:
            normalized: dict[str, float] = {}
            for key, raw in values.items():
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if value != value or value in (float("inf"), float("-inf")):
                    continue
                normalized[str(key)] = max(0.0, value) if nonnegative else value
            return normalized

        caps = _finite_map(self.earnings_caps_by_day, nonnegative=True)
        imports = _finite_map(
            self.forecast_import_costs_by_day,
            nonnegative=True,
        )
        allowances = _finite_map(
            self.fixed_cost_allowances_by_day,
            nonnegative=False,
        )

        def _price_list(values: list[float]) -> list[float]:
            if not values:
                return []
            normalized: list[float] = []
            for raw in values[:length]:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = 0.0
                if value != value or value in (float("inf"), float("-inf")):
                    value = 0.0
                normalized.append(value)
            if len(normalized) < length:
                normalized.extend(
                    [normalized[-1] if normalized else 0.0]
                    * (length - len(normalized))
                )
            return normalized

        valid_days = list(caps)
        valid_day_set = set(valid_days)
        day_ids = [value if value in valid_day_set else None for value in day_ids]
        return CostNeutralPlan(
            day_ids=day_ids,
            earnings_caps_by_day=caps,
            forecast_import_costs_by_day={
                key: imports.get(key, 0.0) for key in valid_days
            },
            fixed_cost_allowances_by_day={
                key: allowances.get(
                    key,
                    caps[key] - imports.get(key, 0.0),
                )
                for key in valid_days
            },
            current_day=(
                str(self.current_day)
                if self.current_day is not None
                else None
            ),
            timezone=self.timezone,
            settlement_import_prices=_price_list(
                self.settlement_import_prices
            ),
            settlement_export_prices=_price_list(
                self.settlement_export_prices
            ),
        )

    @classmethod
    def from_legacy(
        cls,
        *,
        length: int,
        earnings_cap: float | None,
        slots: list[bool],
        forecast_import_cost: float,
        fixed_cost_allowance: float | None,
    ) -> "CostNeutralPlan | None":
        """Adapt the historical scalar contract into one synthetic day."""

        if earnings_cap is None:
            return None
        day = "__legacy__"
        try:
            cap = max(0.0, float(earnings_cap))
        except (TypeError, ValueError):
            return None
        try:
            forecast_cost = max(0.0, float(forecast_import_cost or 0.0))
        except (TypeError, ValueError):
            forecast_cost = 0.0
        try:
            fixed_allowance = float(
                fixed_cost_allowance
                if fixed_cost_allowance is not None
                else cap - forecast_cost
            )
        except (TypeError, ValueError):
            fixed_allowance = cap - forecast_cost
        return cls(
            day_ids=[
                day if idx < len(slots) and slots[idx] else None
                for idx in range(length)
            ],
            earnings_caps_by_day={day: cap},
            forecast_import_costs_by_day={day: forecast_cost},
            fixed_cost_allowances_by_day={day: fixed_allowance},
            current_day=day,
        )


def elapsed_settlement_seconds(start: datetime, end: datetime) -> float:
    """Return elapsed real time across local clock and DST transitions."""

    return (
        end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    ).total_seconds()
