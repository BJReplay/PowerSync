"""Static battery connection-profile registry.

Profiles are intentionally pure data. Importing this module must never import or
construct a hardware client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..const import (
    BATTERY_SYSTEM_CUSTOM,
    BATTERY_SYSTEM_TESLA,
    CONF_BATTERY_CONNECTION_PROFILE,
    CONF_BATTERY_SYSTEM,
)


@dataclass(frozen=True, slots=True)
class BatteryConnectionProfile:
    """One validated user-selectable connection bundle."""

    profile_id: str
    battery_system: str
    label: str
    route_kind: str
    upstream_domains: tuple[str, ...] = ()
    monitoring_only: bool = False
    requires_upstream: bool = False
    route_value: str | None = None
    # Translate an upstream integration's power convention into PowerSync's
    # canonical convention before telemetry is used for balances or display.
    power_multipliers: tuple[tuple[str, float], ...] = ()
    controls_summary: str = "Existing PowerSync control route"


def _p(
    profile_id: str,
    battery_system: str,
    label: str,
    route_kind: str,
    **kwargs: Any,
) -> BatteryConnectionProfile:
    return BatteryConnectionProfile(
        profile_id=profile_id,
        battery_system=battery_system,
        label=label,
        route_kind=route_kind,
        **kwargs,
    )


_PROFILES: tuple[BatteryConnectionProfile, ...] = (
    _p("tesla_provider", BATTERY_SYSTEM_TESLA, "Configured Tesla provider", "provider"),
    _p(
        "tesla_powerwall_monitoring",
        BATTERY_SYSTEM_TESLA,
        "Home Assistant Powerwall integration — monitoring only",
        "ha_monitoring",
        upstream_domains=("powerwall",),
        monitoring_only=True,
        requires_upstream=True,
        controls_summary="Monitoring only; Tesla controls disabled",
    ),
    _p(
        "custom_entities",
        BATTERY_SYSTEM_CUSTOM,
        "Selected Home Assistant entities — monitoring only",
        "manual_monitoring",
        monitoring_only=True,
    ),
)

PROFILE_REGISTRY = {profile.profile_id: profile for profile in _PROFILES}


def profiles_for_system(battery_system: str) -> tuple[BatteryConnectionProfile, ...]:
    """Return stable profiles registered for one battery system."""
    return tuple(p for p in _PROFILES if p.battery_system == battery_system)


def _value(
    data: Mapping[str, Any], options: Mapping[str, Any], key: str, default: Any = None
) -> Any:
    return options.get(key, data.get(key, default))


def legacy_profile_id(
    battery_system: str,
    data: Mapping[str, Any],
    options: Mapping[str, Any],
) -> str:
    """Map existing settings to an equivalent profile without changing route."""
    return {
        BATTERY_SYSTEM_TESLA: "tesla_provider",
        BATTERY_SYSTEM_CUSTOM: "custom_entities",
    }.get(battery_system, "tesla_provider")


def resolve_connection_profile(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    battery_system: str | None = None,
) -> BatteryConnectionProfile:
    """Resolve an explicit profile or the exact legacy-equivalent default."""
    system = battery_system or str(
        _value(data, options, CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)
    )
    requested = _value(data, options, CONF_BATTERY_CONNECTION_PROFILE)
    profile = PROFILE_REGISTRY.get(str(requested)) if requested else None
    if profile is None or profile.battery_system != system:
        profile = PROFILE_REGISTRY[legacy_profile_id(system, data, options)]
    return profile
