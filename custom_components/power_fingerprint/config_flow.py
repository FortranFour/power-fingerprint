"""Config flow for Power Fingerprint."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ADDITIONAL_METERS,
    CONF_AUTO_EXPORT_ENERGY,
    CONF_CALIBRATION_EXCLUDED_ENTITIES,
    CONF_OUTDOOR_BLOCK_ENTITIES,
    CONF_CONCURRENCY_SECONDS,
    CONF_EXCLUDED_METERS,
    CONF_INVERTER_CONFIRM_SECONDS,
    CONF_INVERTER_MIN_STEP_W,
    CONF_MAX_NOISE_W,
    CONF_METER_GRACE_SECONDS,
    CONF_METER_REJOIN_SECONDS,
    CONF_MIN_SAMPLES,
    CONF_MIN_STEP_W,
    CONF_POST_WINDOW_SECONDS,
    CONF_SAMPLE_INTERVAL_SECONDS,
    CONF_PRE_WINDOW_SECONDS,
    CONF_SETTLE_SECONDS,
    CONF_SOURCE_POWER,
    CONF_TRACKED_ENTITIES,
    CONF_USE_ENERGY_METERS,
    DEFAULT_AUTO_EXPORT_ENERGY,
    DEFAULT_CONCURRENCY_SECONDS,
    DEFAULT_INVERTER_CONFIRM_SECONDS,
    DEFAULT_INVERTER_MIN_STEP_W,
    DEFAULT_MAX_NOISE_W,
    DEFAULT_METER_GRACE_SECONDS,
    DEFAULT_METER_REJOIN_SECONDS,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_MIN_STEP_W,
    DEFAULT_POST_WINDOW_SECONDS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_PRE_WINDOW_SECONDS,
    DEFAULT_SETTLE_SECONDS,
    DOMAIN,
    SUPPORTED_TRACKED_DOMAINS,
    SAFE_CALIBRATION_DOMAINS,
)
from .discovery import discover_unmetered_candidates




def _area_sorted_entity_options(hass, entity_ids: list[str]) -> list[SelectOptionDict]:
    """Build setup choices ordered by Home Assistant area, then entity name."""
    area_reg = ar.async_get(hass)
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    rows: list[tuple[bool, str, str, str, str, str]] = []
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        entry = entity_reg.async_get(entity_id)
        area_id = entry.area_id if entry else None
        if not area_id and entry and entry.device_id:
            device = device_reg.async_get(entry.device_id)
            area_id = device.area_id if device else None
        area = area_reg.async_get_area(area_id) if area_id else None
        area_name = area.name if area else "Unassigned"
        friendly = (state.name if state else entity_id) or entity_id
        rows.append((area is None, area_name.casefold(), friendly.casefold(), entity_id, area_name, friendly))

    rows.sort()
    return [
        SelectOptionDict(value=entity_id, label=f"{area_name} — {friendly} ({entity_id})")
        for _unassigned, _area_key, _name_key, entity_id, area_name, friendly in rows
    ]

def _validate_tracked(entity_ids: list[str]) -> bool:
    return all(eid.split(".", 1)[0] in SUPPORTED_TRACKED_DOMAINS for eid in entity_ids)


class PowerFingerprintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure one unified Power Fingerprint engine."""

    VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            tracked = list(user_input.get(CONF_TRACKED_ENTITIES, []))
            if not _validate_tracked(tracked):
                errors[CONF_TRACKED_ENTITIES] = "unsupported_domain"
            else:
                await self.async_set_unique_id("primary")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Power Fingerprint", data=user_input)

        # New installs are seeded from generic device-registry discovery rather
        # than this developer's own entity ids. Existing config entries are not
        # touched: their curated tracked list remains authoritative.
        tracked_default = [profile.entity_id for profile in discover_unmetered_candidates(self.hass)]
        tracked_options = _area_sorted_entity_options(self.hass, tracked_default)
        source_key = vol.Required(CONF_SOURCE_POWER)
        schema = vol.Schema(
            {
                source_key: EntitySelector(EntitySelectorConfig(domain=Platform.SENSOR, device_class=SensorDeviceClass.POWER)),
                vol.Required(CONF_TRACKED_ENTITIES, default=tracked_default): SelectSelector(
                    SelectSelectorConfig(
                        options=tracked_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        sort=False,
                    )
                ),
                vol.Required(CONF_USE_ENERGY_METERS, default=True): BooleanSelector(),
                vol.Required(CONF_AUTO_EXPORT_ENERGY, default=DEFAULT_AUTO_EXPORT_ENERGY): BooleanSelector(),
                vol.Optional(CONF_ADDITIONAL_METERS, default=[]): EntitySelector(
                    EntitySelectorConfig(domain=Platform.SENSOR, device_class=SensorDeviceClass.POWER, multiple=True)
                ),
                vol.Optional(CONF_EXCLUDED_METERS, default=[]): EntitySelector(
                    EntitySelectorConfig(domain=Platform.SENSOR, device_class=SensorDeviceClass.POWER, multiple=True)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        return PowerFingerprintOptionsFlow()


class PowerFingerprintOptionsFlow(OptionsFlowWithReload):
    """Tune entities, unified isolation, and meter handling."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            tracked = list(user_input.get(CONF_TRACKED_ENTITIES, []))
            if not _validate_tracked(tracked):
                errors[CONF_TRACKED_ENTITIES] = "unsupported_domain"
            else:
                return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE_POWER, default=current.get(CONF_SOURCE_POWER, self.config_entry.data.get(CONF_SOURCE_POWER, ""))): EntitySelector(
                    EntitySelectorConfig(domain=Platform.SENSOR, device_class=SensorDeviceClass.POWER)
                ),
                vol.Required(CONF_TRACKED_ENTITIES, default=current.get(CONF_TRACKED_ENTITIES, [])): EntitySelector(
                    EntitySelectorConfig(domain=sorted(SUPPORTED_TRACKED_DOMAINS), multiple=True)
                ),
                vol.Optional(CONF_CALIBRATION_EXCLUDED_ENTITIES, default=current.get(CONF_CALIBRATION_EXCLUDED_ENTITIES, [])): EntitySelector(
                    EntitySelectorConfig(domain=sorted(SAFE_CALIBRATION_DOMAINS), multiple=True)
                ),
                vol.Optional(CONF_OUTDOOR_BLOCK_ENTITIES, default=current.get(CONF_OUTDOOR_BLOCK_ENTITIES, list(getattr(getattr(self.config_entry, "runtime_data", None), "outdoor_block_entities", [])))): EntitySelector(
                    EntitySelectorConfig(domain=["light", "switch"], multiple=True)
                ),
                vol.Required(CONF_USE_ENERGY_METERS, default=current.get(CONF_USE_ENERGY_METERS, True)): BooleanSelector(),
                vol.Required(CONF_AUTO_EXPORT_ENERGY, default=current.get(CONF_AUTO_EXPORT_ENERGY, DEFAULT_AUTO_EXPORT_ENERGY)): BooleanSelector(),
                vol.Optional(CONF_ADDITIONAL_METERS, default=current.get(CONF_ADDITIONAL_METERS, [])): EntitySelector(
                    EntitySelectorConfig(domain=Platform.SENSOR, device_class=SensorDeviceClass.POWER, multiple=True)
                ),
                vol.Optional(CONF_EXCLUDED_METERS, default=current.get(CONF_EXCLUDED_METERS, [])): EntitySelector(
                    EntitySelectorConfig(domain=Platform.SENSOR, device_class=SensorDeviceClass.POWER, multiple=True)
                ),
                vol.Required(CONF_SETTLE_SECONDS, default=current.get(CONF_SETTLE_SECONDS, DEFAULT_SETTLE_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=10, max=120, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PRE_WINDOW_SECONDS, default=current.get(CONF_PRE_WINDOW_SECONDS, DEFAULT_PRE_WINDOW_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=10, max=90, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_POST_WINDOW_SECONDS, default=current.get(CONF_POST_WINDOW_SECONDS, DEFAULT_POST_WINDOW_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=10, max=90, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_MAX_NOISE_W, default=current.get(CONF_MAX_NOISE_W, DEFAULT_MAX_NOISE_W)): NumberSelector(
                    NumberSelectorConfig(min=5, max=250, step=5, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_MIN_STEP_W, default=current.get(CONF_MIN_STEP_W, DEFAULT_MIN_STEP_W)): NumberSelector(
                    NumberSelectorConfig(min=1, max=100, step=1, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_CONCURRENCY_SECONDS, default=current.get(CONF_CONCURRENCY_SECONDS, DEFAULT_CONCURRENCY_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=5, max=60, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_MIN_SAMPLES, default=current.get(CONF_MIN_SAMPLES, DEFAULT_MIN_SAMPLES)): NumberSelector(
                    NumberSelectorConfig(min=2, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
                # These keys are retained for config-entry compatibility with 0.3,
                # but in 0.4 they tune the generic latent climate activity adapter.
                vol.Required(CONF_INVERTER_MIN_STEP_W, default=current.get(CONF_INVERTER_MIN_STEP_W, DEFAULT_INVERTER_MIN_STEP_W)): NumberSelector(
                    NumberSelectorConfig(min=50, max=1500, step=25, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_INVERTER_CONFIRM_SECONDS, default=current.get(CONF_INVERTER_CONFIRM_SECONDS, DEFAULT_INVERTER_CONFIRM_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=5, max=60, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_SAMPLE_INTERVAL_SECONDS, default=current.get(CONF_SAMPLE_INTERVAL_SECONDS, DEFAULT_SAMPLE_INTERVAL_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_METER_GRACE_SECONDS, default=current.get(CONF_METER_GRACE_SECONDS, DEFAULT_METER_GRACE_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=0, max=120, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_METER_REJOIN_SECONDS, default=current.get(CONF_METER_REJOIN_SECONDS, DEFAULT_METER_REJOIN_SECONDS)): NumberSelector(
                    NumberSelectorConfig(min=10, max=600, step=10, mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
