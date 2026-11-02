"""Switch platform for Power Fingerprint."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerFingerprintCoordinator
from .entity import PowerFingerprintEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    async_add_entities([LearningSwitch(coordinator), AutoEnergyDashboardSwitch(coordinator)])


class LearningSwitch(PowerFingerprintEntity, SwitchEntity):
    """Pause/resume learning without stopping inference."""

    _attr_icon = "mdi:brain"

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Learning enabled"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_learning_enabled"

    @property
    def is_on(self) -> bool:
        return self.coordinator.learning_enabled

    async def async_turn_on(self, **kwargs) -> None:
        self.coordinator.learning_enabled = True
        self.coordinator.async_set_updated_data(self.coordinator.snapshot())

    async def async_turn_off(self, **kwargs) -> None:
        self.coordinator.learning_enabled = False
        self.coordinator.async_set_updated_data(self.coordinator.snapshot())


class AutoEnergyDashboardSwitch(PowerFingerprintEntity, SwitchEntity):
    """Enable/disable automatic export of newly-ready devices to Energy Dashboard."""

    _attr_icon = "mdi:home-lightning-bolt-outline"

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Auto-add to Energy Dashboard"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_auto_energy_dashboard"

    @property
    def is_on(self) -> bool:
        return self.coordinator.auto_export_energy

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_auto_export_energy(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_auto_export_energy(False)
