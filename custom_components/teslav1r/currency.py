"""Currency helpers for PowerSync markets and presentation metadata."""
from __future__ import annotations

from typing import Any

from .const import (
    CONF_DISPLAY_CURRENCY,
    DISPLAY_CURRENCIES,
    DISPLAY_CURRENCY_AUTOMATIC,
)

DEFAULT_CURRENCY = "AUD"
CONF_ELECTRICITY_PROVIDER = "electricity_provider"

AU_PROVIDER_CURRENCY = "AUD"

PROVIDER_CURRENCIES: dict[str, str] = {
    "amber": AU_PROVIDER_CURRENCY,
    "localvolts": AU_PROVIDER_CURRENCY,
    "flow_power": AU_PROVIDER_CURRENCY,
    "agl": AU_PROVIDER_CURRENCY,
    "globird": AU_PROVIDER_CURRENCY,
    "aemo_vpp": AU_PROVIDER_CURRENCY,
    "octopus": "GBP",
    "epex": "EUR",
    "nz": "NZD",
    "nz_retailer": "NZD",
    "nz_custom": "NZD",
}

def normalize_currency(currency: Any, fallback: str = DEFAULT_CURRENCY) -> str:
    """Return a normalized ISO 4217 currency code."""
    if isinstance(currency, str):
        code = currency.strip().upper()
        if len(code) == 3 and code.isalpha():
            return code
    return fallback


def currency_from_hass(hass: Any | None, fallback: str = DEFAULT_CURRENCY) -> str:
    """Return Home Assistant's configured currency, with a PowerSync fallback."""
    config = getattr(hass, "config", None)
    return normalize_currency(getattr(config, "currency", None), fallback)


def currency_for_provider(
    provider: str | None,
    hass: Any | None = None,
    fallback: str = DEFAULT_CURRENCY,
) -> str:
    """Return the active currency for a provider.

    Dynamic market providers have fixed native currencies. Generic/custom
    tariff providers follow Home Assistant's configured currency.
    """
    provider_key = (provider or "").strip().lower()
    if provider_key in PROVIDER_CURRENCIES:
        return PROVIDER_CURRENCIES[provider_key]
    return currency_from_hass(hass, fallback)


def currency_for_entry(entry: Any, hass: Any | None = None) -> str:
    """Return the active currency for a config entry."""
    provider = None
    options = getattr(entry, "options", None) or {}
    data = getattr(entry, "data", None) or {}
    if isinstance(options, dict):
        provider = options.get(CONF_ELECTRICITY_PROVIDER)
    if provider is None and isinstance(data, dict):
        provider = data.get(CONF_ELECTRICITY_PROVIDER)
    return currency_for_provider(provider, hass)


def display_currency_for_entry(entry: Any) -> str | None:
    """Return a valid explicit display preference, or None for Automatic.

    This value is deliberately only a presentation preference.  It never
    changes source/provider currency or causes an exchange-rate conversion.
    """
    options = getattr(entry, "options", None) or {}
    data = getattr(entry, "data", None) or {}
    raw = (
        options.get(CONF_DISPLAY_CURRENCY)
        if isinstance(options, dict) and CONF_DISPLAY_CURRENCY in options
        else data.get(CONF_DISPLAY_CURRENCY)
        if isinstance(data, dict)
        else None
    )
    if not isinstance(raw, str):
        return None
    preference = raw.strip().upper()
    if raw.strip().lower() == DISPLAY_CURRENCY_AUTOMATIC:
        return None
    return preference if preference in DISPLAY_CURRENCIES else None


def money_unit(currency: str | None) -> str:
    """Return the Home Assistant unit for pure monetary sensors."""
    return normalize_currency(currency)


def major_price_unit(currency: str | None, denominator: str = "kWh") -> str:
    """Return a major-unit price rate unit, e.g. GBP/kWh."""
    return f"{normalize_currency(currency)}/{denominator}"


def minor_currency_unit(currency: str | None) -> str:
    """Return the familiar minor-unit label for price presentation."""
    code = normalize_currency(currency)
    if code == "GBP":
        return "p"
    if code == "EUR":
        return "ct"
    if code == "SEK":
        return "öre"
    return "c"


def minor_price_unit(currency: str | None, denominator: str = "kWh") -> str:
    """Return a minor-unit rate label, e.g. p/kWh, ct/kWh, or c/kWh."""
    return f"{minor_currency_unit(currency)}/{denominator}"


def currency_metadata(currency: str | None) -> dict[str, str]:
    """Return common currency attributes for price dashboards and clients."""
    code = normalize_currency(currency)
    return {
        "currency": code,
        "price_unit": major_price_unit(code),
        "minor_price_unit": minor_price_unit(code),
    }


def presentation_currency_metadata(
    source_currency: str | None,
    requested_display_currency: str | None = None,
) -> dict[str, str | bool]:
    """Return source metadata plus a fail-closed display preference result.

    Currency conversion is intentionally unsupported.  A selected display
    currency only becomes effective when it already equals the authoritative
    source currency; otherwise clients must keep the source number and unit.
    """
    source = normalize_currency(source_currency)
    requested = normalize_currency(requested_display_currency, "")
    if requested not in DISPLAY_CURRENCIES:
        requested = ""
    fallback = bool(requested and requested != source)
    return {
        **currency_metadata(source),
        "source_currency": source,
        "display_currency": source,
        "requested_display_currency": requested or DISPLAY_CURRENCY_AUTOMATIC,
        "display_currency_fallback": fallback,
    }


def presentation_currency_metadata_for_entry(
    entry: Any,
    source_currency: str | None,
) -> dict[str, str | bool]:
    """Return fail-closed currency presentation metadata for a config entry."""
    return presentation_currency_metadata(
        source_currency,
        display_currency_for_entry(entry),
    )


def selector_unit_for_provider(
    provider: str | None,
    hass: Any | None,
    unit_kind: str = "minor_rate",
) -> str:
    """Return a currency-aware unit for config selectors."""
    currency = currency_for_provider(provider, hass)
    if unit_kind == "money":
        return money_unit(currency)
    if unit_kind == "market_rate":
        return major_price_unit(currency, "MWh")
    if unit_kind == "demand_rate":
        return major_price_unit(currency, "kW")
    if unit_kind == "daily":
        return major_price_unit(currency, "day")
    if unit_kind == "monthly":
        return major_price_unit(currency, "month")
    if unit_kind == "minor_daily":
        return f"{minor_currency_unit(currency)}/day"
    if unit_kind == "major_rate":
        return major_price_unit(currency)
    return minor_price_unit(currency)
