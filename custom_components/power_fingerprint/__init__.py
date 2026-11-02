"""Power Fingerprint custom integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_AUTO_EXPORT_ENERGY,
    CONF_INVERTER_CLIMATE,
    CONF_TRACKED_ENTITIES,
    DEFAULT_AUTO_EXPORT_ENERGY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import PowerFingerprintCoordinator

SERVICE_RESET_MODEL = "reset_model"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register domain-level services."""

    async def _reset_model(call: ServiceCall) -> None:
        entity_id = call.data.get("source_entity")
        reset_energy = bool(call.data.get("reset_energy", False))
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if isinstance(coordinator, PowerFingerprintCoordinator):
                await coordinator.async_reset_model(entity_id, reset_energy=reset_energy)

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_MODEL,
        _reset_model,
        schema=vol.Schema(
            {
                vol.Optional("source_entity"): cv.entity_id,
                vol.Optional("reset_energy", default=False): cv.boolean,
            }
        ),
    )
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate 0.3 configuration into the unified 0.4 tracked-device model."""
    if entry.version >= 2:
        return True

    data = dict(entry.data)
    options = dict(entry.options)
    effective = {**data, **options}
    tracked = list(effective.get(CONF_TRACKED_ENTITIES, []))
    legacy_inverter = effective.get(CONF_INVERTER_CLIMATE)
    if legacy_inverter and legacy_inverter not in tracked:
        tracked.append(legacy_inverter)

    # Put the migrated tracked list into whichever layer currently owns it so an
    # existing user's options continue to override data exactly as before.
    if CONF_TRACKED_ENTITIES in options:
        options[CONF_TRACKED_ENTITIES] = tracked
    else:
        data[CONF_TRACKED_ENTITIES] = tracked

    if CONF_AUTO_EXPORT_ENERGY not in effective:
        if options:
            options[CONF_AUTO_EXPORT_ENERGY] = DEFAULT_AUTO_EXPORT_ENERGY
        else:
            data[CONF_AUTO_EXPORT_ENERGY] = DEFAULT_AUTO_EXPORT_ENERGY

    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=2,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    coordinator = PowerFingerprintCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Entity registry rows for canonical power/energy sensors now exist, so it is
    # safe to reconcile eligible sensors with the Energy Dashboard.
    await coordinator.async_post_setup()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload config entry."""
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await coordinator.async_stop()
    return unloaded
