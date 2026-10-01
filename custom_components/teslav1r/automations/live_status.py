"""Helpers for normalizing coordinator power data for EV automation."""

from __future__ import annotations

from typing import Any


def _kw_to_w(value: Any) -> float:
    """Convert coordinator kW values to watts, treating missing values as 0."""
    try:
        return float(value or 0) * 1000
    except (TypeError, ValueError):
        return 0.0


def _optional_kw_to_w(value: Any) -> float | None:
    """Convert coordinator kW values to watts without inventing missing data."""
    if value is None:
        return None
    try:
        return float(value) * 1000
    except (TypeError, ValueError):
        return None

