"""Base entity for Power Fingerprint."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION
from .coordinator import PowerFingerprintCoordinator


class PowerFingerprintEntity(CoordinatorEntity[PowerFingerprintCoordinator]):
    """Base coordinator entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=NAME,
            manufacturer="Power Fingerprint",
            model="Statistical load disaggregation",
            sw_version=VERSION,
        )
