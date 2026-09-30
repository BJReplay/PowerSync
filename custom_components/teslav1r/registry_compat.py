"""Compatibility helpers for Home Assistant registry API transitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def iter_device_entries(device_registry: Any) -> Iterable[Any]:
    """Iterate device entries on supported Home Assistant registry versions.

    Home Assistant 2024.8 exposes ``devices`` as a mapping, while newer
    releases expose an iterable view and deprecate mapping methods on it.
    """
    devices = getattr(device_registry, "devices", ())
    if isinstance(devices, Mapping):
        return devices.values()
    return devices
