"""Config flow for Teslav1r integration."""

from __future__ import annotations

import logging
import sys
from typing import Any

import aiohttp
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .battery_backend.discovery import (
    discover_battery_sensor_catalog,
    discover_canonical_entities,
)
from .battery_backend.profiles import (
    PROFILE_REGISTRY,
    profiles_for_system,
    resolve_connection_profile,
)

if "probatio" in sys.modules:
    validator = sys.modules["probatio"]
else:
    try:
        import probatio

        validator = probatio
    except ImportError:
        import voluptuous

        validator = voluptuous

Schema = validator.Schema
Required = validator.Required
Optional = validator.Optional
All = validator.All
Coerce = validator.Coerce
Range = validator.Range
Marker = validator.Marker

from .const import (
    BATTERY_CAPACITY_DEFAULTS,
    BATTERY_POWER_DEFAULTS,
    BATTERY_SENSOR_DISPLAY_MODES,
    BATTERY_SENSOR_DISPLAY_RECOMMENDED,
    BATTERY_SYSTEM_CUSTOM,
    BATTERY_SYSTEM_TESLA,
    BATTERY_SYSTEMS,
    CONF_BATTERY_CONNECTION_PROFILE,
    CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY,
    CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID,
    # Smart Optimization configuration
    CONF_BATTERY_SENSOR_DISPLAY_MODE,
    # Battery system selection
    CONF_BATTERY_SYSTEM,
    CONF_DISPLAY_CURRENCY,
    CONF_FLEET_API_BASE_URL,
    CONF_POWERWALL_LOCAL_IP,
    CONF_TESLA_API_PROVIDER,
    CONF_TESLA_ENERGY_SITE_ID,
    CONF_TESLA_EV_API_PROVIDER,
    CONF_TESLA_EV_TELEMETRY_TOKEN,
    CONF_TESLEMETRY_API_TOKEN,
    DISPLAY_CURRENCIES,
    DISPLAY_CURRENCY_AUTOMATIC,
    DOMAIN,
    FLEET_API_BASE_URL,
    TESLA_EV_API_PROVIDER_FLEET_API,
    TESLA_EV_API_PROVIDER_NONE,
    TESLA_PROVIDER_FLEET_API,
)
from .currency import (
    currency_for_provider,
    selector_unit_for_provider,
)
from .monitoring import async_prepare_monitoring_handoff, finish_monitoring_handoff
from .powerwall_host import normalize_powerwall_gateway_host

_LOGGER = logging.getLogger(__name__)

# Per-brand connection/detection keys. Only Tesla Retained
BATTERY_SYSTEM_CONNECTION_KEYS: dict[str, tuple[str, ...]] = {
    BATTERY_SYSTEM_TESLA: (CONF_TESLA_ENERGY_SITE_ID,),
}

def _stored_wh_to_kwh(value: Any, default_wh: int) -> float:
    """Convert a stored Wh/kWh value to kWh for config flow display."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = float(default_wh)
    return amount / 1000.0 if amount >= 1000 else amount

def _stored_w_to_kw(value: Any, default_w: int) -> float:
    """Convert a stored W/kW value to kW for config flow display."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = float(default_w)
    return amount / 1000.0 if amount > 100 else amount

def _stored_optional_w_to_kw(value: Any) -> float | None:
    """Convert an optional stored W/kW value to kW for config flow display."""
    if value in (None, "", []):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return amount / 1000.0 if amount > 100 else amount

def _stored_ratio_to_percent(value: Any, default_ratio: float) -> int:
    """Convert a stored 0-1 ratio or 0-100 percent to a clamped whole percent."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = float(default_ratio)
    if amount <= 1:
        amount *= 100
    return max(0, min(100, round(amount)))

def _stored_optional_price_to_cents(value: Any) -> float:
    """Convert optional stored $/kWh or c/kWh to c/kWh for form display."""
    if value in (None, "", []):
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if amount <= 0:
        return 0.0
    return amount * 100.0 if amount <= 1 else amount

def _stored_normalized_price_to_cents(value: Any) -> float:
    """Convert an unambiguously stored $/kWh value to c/kWh."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, amount) * 100.0

def _normalize_optional_entity(value: Any) -> str | None:
    """Return a usable entity id, or None for unset optional entity fields."""
    if not isinstance(value, str):
        return None

    entity_id = value.strip()
    if not entity_id or entity_id.lower() == "none":
        return None
    return entity_id

def _form_kwh_to_wh(value: Any, default_kwh: float) -> int:
    """Convert a config flow kWh field to Wh for persisted optimizer config."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = default_kwh
    return round(amount * 1000)

def _form_kw_to_w(value: Any, default_kw: float) -> int:
    """Convert a config flow kW field to W for persisted optimizer config."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = default_kw
    return round(amount * 1000)

def _form_optional_kw_to_w(value: Any) -> int | None:
    """Convert an optional config flow kW field to W, preserving explicit zero."""
    if value in (None, "", []):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return round(amount * 1000)

def _form_optional_cents_to_price(value: Any) -> float | None:
    """Convert optional c/kWh form input to stored $/kWh."""
    if value in (None, "", []):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount / 100.0 if amount > 1 else amount

def _form_nonnegative_cents_to_price(value: Any) -> float:
    """Convert a required c/kWh field to normalized stored $/kWh."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, amount) / 100.0

def _form_percent_to_ratio(value: Any, default_ratio: float) -> float:
    """Convert a config flow percent field to a stored 0-1 ratio."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = default_ratio * 100
    return max(0.0, min(1.0, amount / 100.0))

def _default_optimizer_specs_for(battery_system: str) -> tuple[int, int, int]:
    capacity_wh = BATTERY_CAPACITY_DEFAULTS.get(
        battery_system,
        BATTERY_CAPACITY_DEFAULTS[BATTERY_SYSTEM_TESLA],
    )
    power_w = BATTERY_POWER_DEFAULTS.get(
        battery_system,
        BATTERY_POWER_DEFAULTS[BATTERY_SYSTEM_TESLA],
    )
    return capacity_wh, power_w, power_w

async def _validate_fleet_api_token_at(
    hass: HomeAssistant, api_token: str, base_url: str
) -> dict[str, Any]:
    """Validate a Fleet API token against a specific base URL."""
    session = async_get_clientsession(hass)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    async with session.get(
        f"{base_url}/api/1/products",
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        if response.status == 200:
            data = await response.json()
            products = data.get("response", [])
            energy_sites = [p for p in products if "energy_site_id" in p]
            if energy_sites:
                return {"success": True, "sites": energy_sites, "base_url": base_url}
            return {"success": False, "error": "no_energy_sites"}
        if response.status == 401:
            return {"success": False, "error": "invalid_auth"}
        if response.status == 421:
            error_text = await response.text()
            return {"success": False, "error": "out_of_region", "error_text": error_text}
        error_text = await response.text()
        _LOGGER.error("Fleet API error %s: %s", response.status, error_text[:200])
        return {"success": False, "error": "cannot_connect"}

async def validate_fleet_api_token(
    hass: HomeAssistant, api_token: str
) -> dict[str, Any]:
    """Validate the Fleet API token and get sites.

    On a 421 "user out of region" response, Tesla returns the correct regional
    base URL in the error body.  We parse it out and retry automatically so EU
    and AP users don't hit a dead end during setup.
    """
    try:
        result = await _validate_fleet_api_token_at(hass, api_token, FLEET_API_BASE_URL)
        if result.get("error") == "out_of_region":
            import re
            error_text = result.get("error_text", "")
            match = re.search(r"use base URL:\s*(https://[^\s,]+)", error_text)
            if match:
                regional_url = match.group(1).rstrip("/")
                _LOGGER.info(
                    "Fleet API 421 — retrying with regional endpoint: %s", regional_url
                )
                return await _validate_fleet_api_token_at(hass, api_token, regional_url)
            _LOGGER.error("Fleet API 421 but could not parse regional URL from: %s", error_text[:300])
            return {"success": False, "error": "cannot_connect"}
        return result
    except aiohttp.ClientError:
        _LOGGER.exception("Error connecting to Fleet API")
        return {"success": False, "error": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error validating Fleet API token")
        return {"success": False, "error": "unknown"}


def _detect_tesla_fleet_integration(hass: HomeAssistant) -> dict[str, bool]:
    """Detect whether the Tesla Fleet integration is loaded.

    Returns a dict like ``{"tesla_fleet": True}`` so the
    config flow can label provider options with their detection status.
    """
    result = {"tesla_fleet": False}
    for integration in ("tesla_fleet"):
        for entry in hass.config_entries.async_entries(integration):
            if entry.state == ConfigEntryState.LOADED:
                result[integration] = True
                break
    return result


class Teslav1rConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Teslav1r."""

    VERSION = 10

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._tesla_sites: list[dict[str, Any]] = []
        self._site_data: dict[str, Any] = {}
        self._tesla_fleet_available: bool = False
        self._tesla_fleet_token: str | None = None
        self._selected_provider: str | None = None
        self._reauth_entry: ConfigEntry | None = None
        # Battery system selection
        self._selected_battery_system: str = BATTERY_SYSTEM_TESLA
        self._battery_profile_data: dict[str, Any] = {}

    def _currency(self) -> str:
        """Return the currency for the currently selected provider."""
        return currency_for_provider(self._selected_electricity_provider, self.hass)

    def _selector_unit(self, unit_kind: str = "minor_rate") -> str:
        """Return a provider-aware unit label for setup selectors."""
        return selector_unit_for_provider(
            self._selected_electricity_provider,
            self.hass,
            unit_kind,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - choose battery system first."""
        # Check if already configured
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Electricity provider selection is the first step
        return await self.async_step_provider_selection()

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Handle reauthentication when the stored token is no longer valid.

        Triggered by ConfigEntryAuthFailed from the coordinator. We jump
        straight to the relevant token entry step based on which provider
        the user originally configured.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the reauth flow for the configured Tesla provider."""
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_failed")

        _provider = self._reauth_entry.data.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API)

        # Fleet API uses the existing tesla_fleet integration's tokens — no
        # token entry needed; abort and let the user fix tesla_fleet directly
        return self.async_abort(reason="reauth_fleet_api_use_tesla_fleet")

    async def _route_to_battery_setup(self) -> FlowResult:
        """Route to battery system setup based on selection."""
        return await self.async_step_tesla_provider()

    async def async_step_battery_connection_profile_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose the battery connection bundle during initial setup."""
        battery_system = self._selected_battery_system or BATTERY_SYSTEM_TESLA
        profiles = profiles_for_system(battery_system)
        errors: dict[str, str] = {}
        accepted_domains = {
            domain for profile in profiles for domain in profile.upstream_domains
        }
        upstream_entries = [
            entry
            for domain in sorted(accepted_domains)
            for entry in self.hass.config_entries.async_entries(domain)
        ]

        if user_input is not None:
            profile_id = str(user_input.get(CONF_BATTERY_CONNECTION_PROFILE) or "")
            profile = PROFILE_REGISTRY.get(profile_id)
            selected_entry_id = str(
                user_input.get(CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID) or ""
            ).strip()
            anchor_entity = str(
                user_input.get(CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY) or ""
            ).strip()
            if profile is None or profile.battery_system != battery_system:
                errors[CONF_BATTERY_CONNECTION_PROFILE] = "invalid_connection_profile"
            elif profile.requires_upstream:
                if not selected_entry_id and len(upstream_entries) == 1:
                    selected_entry_id = upstream_entries[0].entry_id
                selected_entry = (
                    self.hass.config_entries.async_get_entry(selected_entry_id)
                    if selected_entry_id
                    else None
                )
                yaml_anchor_allowed = False
                if selected_entry is None and not (yaml_anchor_allowed and anchor_entity):
                    errors[CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID] = (
                        "battery_integration_source_required"
                    )
                elif selected_entry is not None and selected_entry.domain not in profile.upstream_domains:
                    errors[CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID] = (
                        "battery_integration_source_mismatch"
                    )

                if not errors and (
                    profile.route_kind == "ha_monitoring"
                    or profile.profile_id in {"goodwe_ha", "solaredge_ha_only"}
                ):
                    catalog = discover_battery_sensor_catalog(
                        self.hass,
                        battery_system=battery_system,
                        profile_id=profile.profile_id,
                        allowed_domains=profile.upstream_domains,
                        config_entry_id=selected_entry_id or None,
                        anchor_entity_id=anchor_entity or None,
                        display_mode="all",
                    )
                    _canonical, missing = discover_canonical_entities(
                        catalog,
                        battery_system=battery_system,
                    )
                    if missing:
                        errors["base"] = "battery_integration_missing_telemetry"


            if not errors and profile is not None:
                if not profile.requires_upstream:
                    selected_entry_id = ""
                    anchor_entity = ""
                self._battery_profile_data = {
                    **getattr(self, "_battery_profile_data", {}),
                    CONF_BATTERY_CONNECTION_PROFILE: profile.profile_id,
                    CONF_BATTERY_SENSOR_DISPLAY_MODE: user_input.get(
                        CONF_BATTERY_SENSOR_DISPLAY_MODE,
                        BATTERY_SENSOR_DISPLAY_RECOMMENDED,
                    ),
                }
                if selected_entry_id:
                    self._battery_profile_data[
                        CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID
                    ] = selected_entry_id
                if anchor_entity:
                    self._battery_profile_data[
                        CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY
                    ] = anchor_entity
                return await self._route_to_battery_setup()

        schema_fields: dict[Any, Any] = {
            Required(
                CONF_BATTERY_CONNECTION_PROFILE,
                default=profiles[0].profile_id,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=profile.profile_id, label=profile.label)
                        for profile in profiles
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            Required(
                CONF_BATTERY_SENSOR_DISPLAY_MODE,
                default=BATTERY_SENSOR_DISPLAY_RECOMMENDED,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=value, label=label)
                        for value, label in BATTERY_SENSOR_DISPLAY_MODES.items()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if upstream_entries:
            schema_fields[
                Optional(CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID)
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=source.entry_id,
                            label=f"{source.title or source.entry_id} ({source.domain})",
                        )
                        for source in upstream_entries
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(
            step_id="battery_connection_profile_setup",
            data_schema=Schema(schema_fields),
            errors=errors,
        )

    def _create_final_entry(self) -> FlowResult:
        """Create final config entry after battery connection is established.

        Merges all collected data and creates the entry. Fine-tuning
        (curtailment, weather, demand charges, EV, inverter config, etc.)
        is done via the options flow or mobile app.
        """
        data = {
            **self._site_data,
            **getattr(self, "_custom_battery_data", {}),
            **getattr(self, "_battery_profile_data", {}),
        }

        # Set battery system type
        if self._selected_battery_system:
            data[CONF_BATTERY_SYSTEM] = self._selected_battery_system

        # Tesla EV API provider (chosen during async_step_tesla_provider).
        # Defaults to "none" so non-Tesla setups stay clean.
        ev_provider_choice = getattr(
            self, "_tesla_ev_provider", TESLA_EV_API_PROVIDER_NONE
        )
        data[CONF_TESLA_EV_API_PROVIDER] = ev_provider_choice
        ev_token = getattr(self, "_tesla_ev_teslemetry_token", None)
        if ev_token:
            data[CONF_TESLA_EV_TELEMETRY_TOKEN] = ev_token

        # Set appropriate title based on battery system and provider
        title = "Tesla v1r"

        return self.async_create_entry(title=title, data=data)

    async def async_step_battery_system(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user choose battery system - Tesla (first step)."""
        if user_input is not None:
            self._selected_battery_system = user_input.get(
                CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA
            )

        return self.async_show_form(
            step_id="battery_system",
            data_schema=Schema(
                {
                    Required(
                        CONF_BATTERY_SYSTEM, default=BATTERY_SYSTEM_TESLA
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=k, label=v)
                                for k, v in BATTERY_SYSTEMS.items()
                            ],
                            # Keep this as a dropdown so newer battery systems
                            # do not get pushed below the fold in the setup UI.
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    

    async def async_step_tesla_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let user choose between Tesla Fleet and Teslemetry."""
        # Check if Tesla Fleet integration is configured and loaded
        self._tesla_fleet_available = False
        self._tesla_fleet_token = None

        tesla_fleet_entries = self.hass.config_entries.async_entries("tesla_fleet")
        if tesla_fleet_entries:
            for tesla_entry in tesla_fleet_entries:
                if tesla_entry.state == ConfigEntryState.LOADED:
                    try:
                        if CONF_TOKEN in tesla_entry.data:
                            token_data = tesla_entry.data[CONF_TOKEN]
                            if CONF_ACCESS_TOKEN in token_data:
                                self._tesla_fleet_token = token_data[CONF_ACCESS_TOKEN]
                                self._tesla_fleet_available = True
                                _LOGGER.info(
                                    "Tesla Fleet integration detected and available"
                                )
                                break
                    except Exception as e:
                        _LOGGER.warning(
                            "Failed to extract tokens from Tesla Fleet integration: %s",
                            e,
                        )


        def _build_schema(include_fleet: bool) -> Schema:
            energy_options: list[SelectOptionDict] = [
                SelectOptionDict(
                    value=TESLA_PROVIDER_FLEET_API,
                    label="Tesla Fleet API (Free - uses existing Tesla Fleet integration)",
                ),
            ]


            return Schema(
                {
                    Required(
                        CONF_TESLA_API_PROVIDER, default=TESLA_PROVIDER_FLEET_API
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=energy_options,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            )

        async def _handle_ev_provider_selection(
            user_input_local: dict[str, Any],
        ) -> FlowResult | None:
            """Stash and validate the EV provider choice. Returns a follow-up
            FlowResult when the user picked Teslemetry-without-detection (token
            entry), or None to indicate the caller should continue normally."""
            ev_choice = user_input_local.get(
                CONF_TESLA_EV_API_PROVIDER, TESLA_EV_API_PROVIDER_NONE
            )
            self._tesla_ev_provider = ev_choice
            detected = _detect_tesla_fleet_integration(self.hass)
            if (
                ev_choice == TESLA_EV_API_PROVIDER_FLEET_API
                and not detected["tesla_fleet"]
            ):
                # Hard fail — Tesla Fleet OAuth can't be entered manually here
                return self.async_show_form(
                    step_id="tesla_provider",
                    data_schema=_build_schema(self._tesla_fleet_available),
                    errors={CONF_TESLA_EV_API_PROVIDER: "tesla_fleet_not_installed"},
                )

        # Tesla Fleet is available - let user choose
        if user_input is not None:
            ev_followup = await _handle_ev_provider_selection(user_input)
            if ev_followup is not None:
                return ev_followup

            self._selected_provider = user_input[CONF_TESLA_API_PROVIDER]

            if self._selected_provider == TESLA_PROVIDER_FLEET_API:
                # User chose Fleet API - validate and get sites
                _LOGGER.info("User selected Tesla Fleet API")
                validation_result = await validate_fleet_api_token(
                    self.hass, self._tesla_fleet_token
                )

                if validation_result["success"]:
                    # Store empty Teslemetry token (we'll use Fleet API in __init__.py)
                    # AND persist the provider choice so that on HA restart the
                    # integration remembers we picked Fleet API instead of
                    # defaulting back to Teslemetry (which would then 401 on
                    # the empty token and break the Tesla coordinator).
                    # Also persist the regional base URL so EU/AP users don't hit
                    # the hardcoded NA endpoint on every subsequent API call.
                    self._teslemetry_data = {
                        CONF_TESLEMETRY_API_TOKEN: "",
                        CONF_TESLA_API_PROVIDER: TESLA_PROVIDER_FLEET_API,
                        CONF_FLEET_API_BASE_URL: validation_result.get("base_url", FLEET_API_BASE_URL),
                    }
                    self._tesla_sites = validation_result.get("sites", [])
                    return await self.async_step_site_selection()
                else:
                    # Fleet API validation failed - show error
                    errors = {"base": validation_result.get("error", "unknown")}
                    return self.async_show_form(
                        step_id="tesla_provider",
                        data_schema=_build_schema(include_fleet=True),
                        errors=errors,
                    )

        # Show provider selection form — default to Fleet (free, recommended)
        return self.async_show_form(
            step_id="tesla_provider",
            data_schema=_build_schema(include_fleet=True),
            description_placeholders={
                "fleet_detected": "✓ Tesla Fleet integration detected!",
            },
        )

    @staticmethod
    def _months_in_tariff_season(from_month: int, to_month: int) -> list[int]:
        """Expand an inclusive, possibly year-wrapping month range."""
        if not 1 <= from_month <= 12 or not 1 <= to_month <= 12:
            return []
        if from_month <= to_month:
            return list(range(from_month, to_month + 1))
        return list(range(from_month, 13)) + list(range(1, to_month + 1))

    @classmethod
    def _tariff_seasons_error(cls, seasons: list[dict]) -> str | None:
        """Validate unique names and exact, non-overlapping year coverage."""
        if not seasons:
            return "season_required"

        names: set[str] = set()
        coverage: dict[int, str] = {}
        has_all_year_fallback = False
        for season in seasons:
            name = str(season.get("name", "")).strip()
            if not name or name.casefold() in names:
                return "season_name_invalid"
            names.add(name.casefold())
            try:
                from_month = int(season.get("from_month"))
                to_month = int(season.get("to_month"))
            except (TypeError, ValueError):
                return "season_month_invalid"
            months = cls._months_in_tariff_season(from_month, to_month)
            if not months:
                return "season_month_invalid"
            if name.casefold() == "all year":
                if months != list(range(1, 13)):
                    return "season_month_invalid"
                has_all_year_fallback = True
                continue
            for month in months:
                if month in coverage:
                    return "season_month_overlap"
                coverage[month] = name

        if not has_all_year_fallback and set(coverage) != set(range(1, 13)):
            return "season_month_gap"
        return None

    @staticmethod
    def _tariff_season_options(seasons: list[dict]) -> list[SelectOptionDict]:
        """Build selector options for existing tariff seasons."""
        return [
            SelectOptionDict(value=str(season["name"]), label=str(season["name"]))
            for season in seasons
        ]

    @staticmethod
    def _tariff_month_options() -> list[SelectOptionDict]:
        """Build month selector options without relying on locale state."""
        names = (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        )
        return [
            SelectOptionDict(value=str(index), label=name)
            for index, name in enumerate(names, 1)
        ]
    @staticmethod
    def _pop_tariff_period(
        periods: list[dict], selection: object
    ) -> dict | None:
        """Remove one explicitly selected tariff period, failing closed."""
        try:
            index = int(str(selection))
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(periods):
            return None
        return periods.pop(index)

    @staticmethod
    def _tariff_period_remove_options(
        periods: list[dict],
    ) -> list[SelectOptionDict]:
        """Build unambiguous selector options for removing a tariff period."""
        day_labels = {
            "weekdays": "Mon-Fri",
            "weekends": "Sat-Sun",
            "all_days": "Mon-Sun",
        }
        options = [
            SelectOptionDict(value="none", label="Keep all added periods")
        ]
        for index, period in enumerate(periods):
            label = {
                "PEAK": "Peak",
                "SHOULDER": "Shoulder",
                "OFF_PEAK": "Off-Peak",
                "SUPER_OFF_PEAK": "Super Off-Peak",
            }.get(period.get("name"), str(period.get("name", "Period")))
            options.append(
                SelectOptionDict(
                    value=str(index),
                    label=(
                        f"Remove {index + 1}. {label} "
                        f"{int(period.get('start', 0)):02d}:00-"
                        f"{int(period.get('end', 24)):02d}:00 "
                        f"{day_labels.get(period.get('days'), 'Mon-Sun')}"
                    ),
                )
            )
        return options

    async def async_step_site_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle site selection for both Amber and Tesla."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                gateway_ip = normalize_powerwall_gateway_host(
                    user_input.get(CONF_POWERWALL_LOCAL_IP)
                )
            except ValueError:
                errors[CONF_POWERWALL_LOCAL_IP] = "powerwall_gateway_invalid"
            else:

                # Store site selection data
                self._site_data = {
                    CONF_TESLA_ENERGY_SITE_ID: user_input[
                        CONF_TESLA_ENERGY_SITE_ID
                    ],
                }

                if gateway_ip:
                    self._site_data[CONF_POWERWALL_LOCAL_IP] = gateway_ip

                # Go directly to creating the entry (skip later setup steps).
                return self._create_final_entry()

        data_schema_dict: dict[Marker, Any] = {}

        if self._tesla_sites:
            # Build Tesla site options from Teslemetry API response
            tesla_site_options = [
                SelectOptionDict(
                    value=str(site.get("energy_site_id")),
                    label=f"{site.get('site_name', 'Tesla Energy Site ' + str(site.get('energy_site_id')))} ({site.get('energy_site_id')})",
                )
                for site in self._tesla_sites
            ]

            data_schema_dict[Required(CONF_TESLA_ENERGY_SITE_ID)] = SelectSelector(
                SelectSelectorConfig(
                    options=tesla_site_options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

            # Optional gateway LAN IP for direct local features (snapshot
            # polling, automated curtailment, fast operation-mode toggles).
            # Pairing itself is cloud-based (Fleet API key registration);
            # gateway control uses RSA signing — no password required.
            data_schema_dict[Optional(CONF_POWERWALL_LOCAL_IP, default="")] = str
        else:
            # No sites found - should not happen if validation worked
            _LOGGER.error("No Tesla energy sites found in Teslemetry account")
            return self.async_abort(reason="no_energy_sites")

        data_schema = Schema(data_schema_dict)

        return self.async_show_form(
            step_id="site_selection",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> Teslav1rOptionsFlow:
        """Get the options flow for this handler."""
        return Teslav1rOptionsFlow()


class Teslav1rOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Teslav1r."""

    def _get_option(self, key: str, default: Any = None) -> Any:
        """Get option value with fallback to data for backwards compatibility."""
        return self.config_entry.options.get(
            key, self.config_entry.data.get(key, default)
        )

    def _effective_battery_system(self) -> str:
        """Return the configured battery/control method."""
        return self._get_option(CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA)

    def _schedule_entry_reload(self) -> None:
        """Reload the entry after structural connection changes."""
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self.config_entry.entry_id)
        )

    def _save_battery_system_selection(self, battery_system: str) -> None:
        """Persist the selected battery/control method in data and options."""
        new_data = dict(self.config_entry.data)
        new_options = dict(self.config_entry.options)
        new_data[CONF_BATTERY_SYSTEM] = battery_system
        new_options[CONF_BATTERY_SYSTEM] = battery_system

        for brand, keys in BATTERY_SYSTEM_CONNECTION_KEYS.items():
            if brand == battery_system:
                continue
            for key in keys:
                new_data.pop(key, None)
                new_options.pop(key, None)

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=new_data,
            options=new_options,
        )

    def _save_connection_and_reload(
        self,
        data_updates: dict[str, Any],
        option_updates: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Persist connection/configuration changes and reload the integration."""
        new_data = dict(self.config_entry.data)
        new_options = dict(self.config_entry.options)
        new_data.update(data_updates)
        new_options.update(option_updates if option_updates is not None else data_updates)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data=new_data,
            options=new_options,
        )
        self._schedule_entry_reload()
        return self.async_create_entry(title="", data=new_options)

    async def _save_connection_profile_and_reload(
        self,
        data_updates: dict[str, Any],
    ) -> FlowResult:
        """Restore the old route, then atomically persist a profile change."""
        old_profile = resolve_connection_profile(
            self.config_entry.data,
            self.config_entry.options,
            self._effective_battery_system(),
        )
        route_changed = any(
            data_updates.get(key) != self._get_option(key)
            for key in (
                CONF_BATTERY_CONNECTION_PROFILE,
                CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID,
                CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY,
            )
        )
        handoff_started = False
        if route_changed and not old_profile.monitoring_only:
            try:
                await async_prepare_monitoring_handoff(
                    self.hass,
                    self.config_entry,
                )
                handoff_started = True
            except Exception as err:
                finish_monitoring_handoff(self.hass, self.config_entry)
                _LOGGER.warning(
                    "Battery connection profile remained '%s' because control "
                    "cleanup failed: %s",
                    old_profile.profile_id,
                    err,
                )
                return self.async_abort(reason="monitoring_cleanup_failed")
        try:
            return self._save_connection_and_reload(data_updates)
        finally:
            if handoff_started:
                finish_monitoring_handoff(self.hass, self.config_entry)

    def _selector_unit(self, unit_kind: str = "minor_rate") -> str:
        """Return a provider-aware unit label for options selectors."""
        return selector_unit_for_provider(
            self._electricity_provider(),
            self.hass,
            unit_kind,
        )

    def _currency(self) -> str:
        """Return the configured currency."""
        return currency_for_provider(self._electricity_provider(), self.hass)

    def _save_and_finish(self, section_data: dict[str, Any]) -> FlowResult:
        """Save a single section's data merged with existing options and finish."""
        final = dict(self.config_entry.options)
        final.update(section_data)
        self._apply_legacy_data_key_removals()
        return self.async_create_entry(title="", data=final)

    def _remove_legacy_data_keys(self, keys: tuple[str, ...]) -> None:
        """Mark option-owned keys for removal from legacy config entry data."""
        pending = set(getattr(self, "_legacy_data_keys_to_remove", ()))
        pending.update(keys)
        self._legacy_data_keys_to_remove = tuple(sorted(pending))

    def _apply_legacy_data_key_removals(self) -> None:
        """Remove pending option-owned keys from legacy config entry data."""
        keys = getattr(self, "_legacy_data_keys_to_remove", ())
        if not keys:
            return
        new_data = dict(self.config_entry.data)
        for key in keys:
            new_data.pop(key, None)
        if new_data != self.config_entry.data:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data,
            )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show options menu -- user picks which section to reconfigure."""
        battery_system = self._effective_battery_system()

        # Build menu options based on current config
        menu_options = [
            "display_currency",
            "pricing",
            "battery_system",
            "battery_connection_profile",
        ]

        # Battery connection settings
        if battery_system == BATTERY_SYSTEM_TESLA:
            menu_options.append("tesla_connection")
        elif battery_system == BATTERY_SYSTEM_CUSTOM:
            menu_options.append("custom_battery")

        menu_options.extend(
            [
                "optimization",
                "ev_charging",
                "ev_load_management",
                "advanced",
            ]
        )

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_display_currency(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose the non-converting display currency used by clients."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_DISPLAY_CURRENCY)
            if selected not in DISPLAY_CURRENCIES:
                errors[CONF_DISPLAY_CURRENCY] = "invalid_display_currency"
            else:
                # Keep this preference in options only: it must not alter a
                # provider's tariff input, stored prices, or optimizer units.
                return self._save_connection_and_reload(
                    {}, {CONF_DISPLAY_CURRENCY: selected}
                )

        current = self._get_option(
            CONF_DISPLAY_CURRENCY, DISPLAY_CURRENCY_AUTOMATIC
        )
        if current not in DISPLAY_CURRENCIES:
            current = DISPLAY_CURRENCY_AUTOMATIC
        return self.async_show_form(
            step_id="display_currency",
            data_schema=Schema({
                Required(CONF_DISPLAY_CURRENCY, default=current): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(
                                value=DISPLAY_CURRENCY_AUTOMATIC,
                                label="Automatic (provider / Home Assistant)",
                            ),
                            *[
                                SelectOptionDict(value=currency, label=currency)
                                for currency in DISPLAY_CURRENCIES
                                if currency != DISPLAY_CURRENCY_AUTOMATIC
                            ],
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }),
            errors=errors,
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show optional and specialist settings outside the main path."""
        menu_options = [
            "back",
            "network_export",
            "inverter",
            "curtailment",
            "demand_charges",
            "weather",
            "auto_update",
            "cloud_flow",
        ]

        return self.async_show_menu(
            step_id="advanced",
            menu_options=menu_options,
        )

    async def async_step_back(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Return from a nested options menu to the main settings menu."""
        return await self.async_step_init()

    @staticmethod
    def _network_export_power_state_valid(
        hass: HomeAssistant,
        entity_id: str,
        *,
        allow_negative: bool = False,
    ) -> bool:
        """Return whether an entity currently exposes finite W/kW power."""
        state = hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (None, "", "unknown", "unavailable"):
            return False
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return False
        if value != value or value in (float("inf"), float("-inf")):
            return False
        if value < 0 and not allow_negative:
            return False
        unit = str((state.attributes or {}).get("unit_of_measurement") or "").lower()
        return unit in {"w", "kw", "watt", "watts", "kilowatt", "kilowatts"}

    def _network_export_active_source_error(self, entity_id: str) -> str | None:
        """Return an active-mode provenance error for a limit source."""
        from homeassistant.helpers import entity_registry as er

        registry_entry = er.async_get(self.hass).async_get(entity_id)
        if registry_entry is None:
            return "network_export_source_unregistered"
        platform = str(getattr(registry_entry, "platform", "") or "").lower()
        if platform in {"template", DOMAIN}:
            return "network_export_source_untrusted"
        if not getattr(registry_entry, "unique_id", None):
            return "network_export_source_untrusted"
        if not (
            getattr(registry_entry, "device_id", None)
            or getattr(registry_entry, "config_entry_id", None)
        ):
            return "network_export_source_untrusted"
        if getattr(registry_entry, "config_entry_id", None) == self.config_entry.entry_id:
            return "network_export_source_untrusted"
        return None


    async def _route_to_battery_options(self, battery_system: str) -> FlowResult:
        """Route to the selected battery/control method options page."""
        if battery_system == BATTERY_SYSTEM_TESLA:
            return await self.async_step_tesla_connection()
        if battery_system == BATTERY_SYSTEM_CUSTOM:
            return await self.async_step_custom_battery()
        return await self.async_step_tesla_connection()

    async def async_step_battery_system(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu handler: choose or change battery/control method."""
        if user_input is not None:
            battery_system = user_input.get(
                CONF_BATTERY_SYSTEM, BATTERY_SYSTEM_TESLA
            )
            if battery_system == self._effective_battery_system():
                return await self.async_step_init()
            self._save_battery_system_selection(battery_system)
            return await self._route_to_battery_options(battery_system)

        return self.async_show_form(
            step_id="battery_system",
            data_schema=Schema(
                {
                    Required(
                        CONF_BATTERY_SYSTEM,
                        default=self._effective_battery_system(),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=k, label=v)
                                for k, v in BATTERY_SYSTEMS.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_battery_connection_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Select one validated connection bundle and upstream source."""
        battery_system = self._effective_battery_system()
        profiles = profiles_for_system(battery_system)
        current_profile = resolve_connection_profile(
            self.config_entry.data,
            self.config_entry.options,
            battery_system,
        )
        errors: dict[str, str] = {}

        accepted_domains = {
            domain for profile in profiles for domain in profile.upstream_domains
        }
        upstream_entries = [
            entry
            for domain in sorted(accepted_domains)
            for entry in self.hass.config_entries.async_entries(domain)
        ]

        if user_input is not None:
            profile_id = str(user_input.get(CONF_BATTERY_CONNECTION_PROFILE) or "")
            profile = PROFILE_REGISTRY.get(profile_id)
            selected_entry_id = str(
                user_input.get(CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID) or ""
            ).strip()
            anchor_entity = str(
                user_input.get(CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY) or ""
            ).strip()
            if profile is None or profile.battery_system != battery_system:
                errors[CONF_BATTERY_CONNECTION_PROFILE] = "invalid_connection_profile"
            elif profile.requires_upstream:
                if not selected_entry_id and len(upstream_entries) == 1:
                    selected_entry_id = upstream_entries[0].entry_id
                selected_entry = (
                    self.hass.config_entries.async_get_entry(selected_entry_id)
                    if selected_entry_id
                    else None
                )
                yaml_anchor_allowed = False
                if selected_entry is None and not (yaml_anchor_allowed and anchor_entity):
                    errors[CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID] = (
                        "battery_integration_source_required"
                    )
                elif selected_entry is not None and selected_entry.domain not in profile.upstream_domains:
                    errors[CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID] = (
                        "battery_integration_source_mismatch"
                    )

                if not errors and (
                    profile.route_kind == "ha_monitoring"
                    or profile.profile_id in {"goodwe_ha", "solaredge_ha_only"}
                ):
                    catalog = discover_battery_sensor_catalog(
                        self.hass,
                        battery_system=battery_system,
                        profile_id=profile.profile_id,
                        allowed_domains=profile.upstream_domains,
                        config_entry_id=selected_entry_id or None,
                        anchor_entity_id=anchor_entity or None,
                        display_mode="all",
                    )
                    _canonical, missing = discover_canonical_entities(
                        catalog,
                        battery_system=battery_system,
                    )
                    if missing:
                        errors["base"] = "battery_integration_missing_telemetry"

            if not errors and profile is not None:
                if not profile.requires_upstream:
                    selected_entry_id = ""
                    anchor_entity = ""
                updates: dict[str, Any] = {
                    CONF_BATTERY_CONNECTION_PROFILE: profile.profile_id,
                    CONF_BATTERY_SENSOR_DISPLAY_MODE: user_input.get(
                        CONF_BATTERY_SENSOR_DISPLAY_MODE,
                        BATTERY_SENSOR_DISPLAY_RECOMMENDED,
                    ),
                }
                if selected_entry_id:
                    updates[CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID] = selected_entry_id
                else:
                    updates[CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID] = None
                if anchor_entity:
                    updates[CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY] = anchor_entity
                else:
                    updates[CONF_BATTERY_INTEGRATION_ANCHOR_ENTITY] = None

                return await self._save_connection_profile_and_reload(updates)

        schema_fields: dict[Any, Any] = {
            Required(
                CONF_BATTERY_CONNECTION_PROFILE,
                default=current_profile.profile_id,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=profile.profile_id, label=profile.label)
                        for profile in profiles
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            Required(
                CONF_BATTERY_SENSOR_DISPLAY_MODE,
                default=self._get_option(
                    CONF_BATTERY_SENSOR_DISPLAY_MODE,
                    BATTERY_SENSOR_DISPLAY_RECOMMENDED,
                ),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=value, label=label)
                        for value, label in BATTERY_SENSOR_DISPLAY_MODES.items()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
        if upstream_entries:
            current_source = self._get_option(
                CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID, ""
            )
            schema_fields[
                Optional(
                    CONF_BATTERY_INTEGRATION_CONFIG_ENTRY_ID,
                    default=current_source or upstream_entries[0].entry_id,
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(
                            value=source.entry_id,
                            label=f"{source.title or source.entry_id} ({source.domain})",
                        )
                        for source in upstream_entries
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(
            step_id="battery_connection_profile",
            data_schema=Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "controls": current_profile.controls_summary,
            },
        )



    async def async_step_tesla_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Menu handler: Tesla Energy/EV API provider + local gateway IP."""
        errors: dict[str, str] = {}

        if user_input is not None:
            tesla_provider = user_input.get(CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API)
            # Optional Powerwall local LAN access. Empty gateway IP clears it
            # (back to cloud-only mode); a non-empty IP requires the gateway
            # customer password.
            gateway_ip_raw = user_input.get(CONF_POWERWALL_LOCAL_IP)
            try:
                gateway_ip = normalize_powerwall_gateway_host(gateway_ip_raw)
            except ValueError:
                gateway_ip = ""
                errors[CONF_POWERWALL_LOCAL_IP] = "powerwall_gateway_invalid"

            if not errors:
                new_data = dict(self.config_entry.data)
                new_data[CONF_TESLA_API_PROVIDER] = tesla_provider
                # Persist gateway IP changes; remove the key entirely when
                # cleared so the diagnostic binary_sensor flips correctly
                # rather than reading an empty string as "set".
                if gateway_ip:
                    new_data[CONF_POWERWALL_LOCAL_IP] = gateway_ip
                else:
                    new_data.pop(CONF_POWERWALL_LOCAL_IP, None)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

                # Route to token step
                self._tesla_provider = tesla_provider

                # Fleet API -- save directly
                self._schedule_entry_reload()
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )

        current_tesla_provider = self.config_entry.data.get(
            CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API
        )
        current_gateway_ip = self.config_entry.data.get(
            CONF_POWERWALL_LOCAL_IP, ""
        )

        tesla_providers = {
            TESLA_PROVIDER_FLEET_API: "Tesla Fleet API (Free - requires Tesla Fleet integration)",
        }

        return self.async_show_form(
            step_id="tesla_connection",
            data_schema=Schema(
                {
                    Required(
                        CONF_TESLA_API_PROVIDER,
                        default=current_tesla_provider,
                    ): SelectSelector(SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=k, label=v)
                            for k, v in tesla_providers.items()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )),
                    # Optional gateway LAN IP for direct local features.
                    # Pairing is cloud-based; gateway control uses RSA
                    # signing — no password required.
                    Required(
                        CONF_POWERWALL_LOCAL_IP,
                        default=current_gateway_ip,
                    ): str,
                }
            ),
            errors=errors,
        )


    async def async_step_init_tesla(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 for Tesla users: select electricity provider and Tesla API providers."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Store provider selections
            self._tesla_provider = user_input.get(
                CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API
            )

            current_tesla_provider = self.config_entry.data.get(
                CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API
            )

            if not errors:
                # Fleet API — no token entry needed
                new_data = dict(self.config_entry.data)
                if self._tesla_provider != current_tesla_provider:
                    new_data[CONF_TESLA_API_PROVIDER] = self._tesla_provider

                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

        current_tesla_provider = self.config_entry.data.get(
            CONF_TESLA_API_PROVIDER, TESLA_PROVIDER_FLEET_API
        )

        # Build Tesla provider choices
        tesla_providers = {
            TESLA_PROVIDER_FLEET_API: "Tesla Fleet API (Free - requires Tesla Fleet integration)",
        }

        return self.async_show_form(
            step_id="init_tesla",
            data_schema=Schema(
                {
                    Required(
                        CONF_TESLA_API_PROVIDER,
                        default=current_tesla_provider,
                    ): SelectSelector(SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=k, label=v)
                            for k, v in tesla_providers.items()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )),
                }
            ),
            errors=errors,
        )
