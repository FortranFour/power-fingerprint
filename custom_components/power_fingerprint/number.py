"""Number platform for manual fixed-power overrides."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerFingerprintCoordinator
from .entity import PowerFingerprintEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    async_add_entities([FixedPowerValueNumber(coordinator)])


class FixedPowerValueNumber(PowerFingerprintEntity, NumberEntity):
    """Operating watts used by the selected manual fixed-power profile."""

    _attr_name = "Fixed power value"
    _attr_icon = "mdi:flash"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = 0.1
    _attr_native_max_value = 12000.0
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fixed_power_value"

    @property
    def native_value(self) -> float | None:
        attrs = self.coordinator.fixed_power_target_attributes()
        value = attrs.get("fixed_power_w")
        if value is None:
            value = attrs.get("suggested_power_w")
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.fixed_power_target_attributes()

    async def async_set_native_value(self, value: float) -> None:
        attrs = self.coordinator.fixed_power_target_attributes()
        entity_id = attrs.get("entity_id")
        if not entity_id:
            raise ValueError("No fixed-power target selected")
        await self.coordinator.async_set_fixed_power_value(entity_id, value)
