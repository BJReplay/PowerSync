"""Validate the Tesla force-discharge tariff's temporary buy and sell rates."""

from __future__ import annotations

import math

from .const import (
    CONF_TESLA_FORCE_DISCHARGE_BUY_PRICE,
    CONF_TESLA_FORCE_DISCHARGE_SELL_PRICE,
    DEFAULT_TESLA_FORCE_DISCHARGE_BUY_PRICE,
    DEFAULT_TESLA_FORCE_DISCHARGE_SELL_PRICE,
)


def validate_force_discharge_prices(buy: object, sell: object) -> tuple[float, float]:
    """Return finite $/kWh rates with an export incentive and bounded values."""
    try:
        buy_price = float(buy)
        sell_price = float(sell)
    except (TypeError, ValueError) as err:
        raise ValueError("Tesla force-discharge prices must be numbers") from err
    if not (math.isfinite(buy_price) and math.isfinite(sell_price)):
        raise ValueError("Tesla force-discharge prices must be finite")
    if not (0 <= buy_price <= 99 and 0 < sell_price <= 99):
        raise ValueError("Tesla force-discharge prices must be between $0 and $99/kWh")
    if sell_price <= buy_price:
        raise ValueError("Tesla force-discharge sell price must exceed buy price")
    return buy_price, sell_price


def configured_force_discharge_prices(data: dict) -> tuple[float, float]:
    """Use safe defaults for older or invalid stored configurations."""
    try:
        return validate_force_discharge_prices(
            data.get(
                CONF_TESLA_FORCE_DISCHARGE_BUY_PRICE,
                DEFAULT_TESLA_FORCE_DISCHARGE_BUY_PRICE,
            ),
            data.get(
                CONF_TESLA_FORCE_DISCHARGE_SELL_PRICE,
                DEFAULT_TESLA_FORCE_DISCHARGE_SELL_PRICE,
            ),
        )
    except ValueError:
        return (
            DEFAULT_TESLA_FORCE_DISCHARGE_BUY_PRICE,
            DEFAULT_TESLA_FORCE_DISCHARGE_SELL_PRICE,
        )
