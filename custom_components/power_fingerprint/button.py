"""Button platform for Power Fingerprint."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerFingerprintCoordinator
from .entity import PowerFingerprintEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    async_add_entities([
        CalibrationButton(coordinator),
        CalibrationBatchButton(coordinator),
        CalibrationBatchStopButton(coordinator),
        PowercalcGraduateSelectedButton(coordinator),
        PowercalcGraduateAllButton(coordinator),
        RetrainSelectedProfileButton(coordinator),
        RevokeSelectedGraduationButton(coordinator),
    ])


class CalibrationButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:target-variant"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Calibrate selected device"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_calibrate_selected"
    @property
    def available(self) -> bool:
        return not self.coordinator.calibration_batch_running and not self.coordinator.calibrating_entity
    async def async_press(self) -> None:
        if self.available:
            self.coordinator.entry.async_create_background_task(self.hass, self.coordinator.async_calibrate_selected(), name="Power Fingerprint calibration", eager_start=True)


class CalibrationBatchButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:playlist-check"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Calibrate devices needing training"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_calibrate_training_batch"
    @property
    def available(self) -> bool:
        return not self.coordinator.calibration_batch_running and not self.coordinator.calibrating_entity
    async def async_press(self) -> None:
        if self.available:
            self.coordinator.entry.async_create_background_task(self.hass, self.coordinator.async_calibrate_training_batch(), name="Power Fingerprint intelligent calibration batch", eager_start=True)


class CalibrationBatchStopButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:stop-circle-outline"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Stop calibration batch"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_stop_calibration_batch"
    @property
    def available(self) -> bool:
        return self.coordinator.calibration_batch_running
    async def async_press(self) -> None:
        self.coordinator.request_calibration_batch_stop()


class PowercalcGraduateSelectedButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:school-outline"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Graduate selected device to Powercalc"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_powercalc_graduate_selected"
    @property
    def available(self) -> bool:
        return bool(self.coordinator.powercalc_graduation_options())
    async def async_press(self) -> None:
        if self.available:
            await self.coordinator.async_graduate_selected_to_powercalc()


class PowercalcGraduateAllButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:school"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Graduate all stable devices to Powercalc"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_powercalc_graduate_all"
    @property
    def available(self) -> bool:
        return bool(self.coordinator.powercalc_graduation_options())
    async def async_press(self) -> None:
        if self.available:
            await self.coordinator.async_graduate_all_to_powercalc()


class RetrainSelectedProfileButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:school-outline"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Retrain selected profile"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_retrain_selected_profile"
    @property
    def available(self) -> bool:
        return bool(self.coordinator.profile_management_target)
    async def async_press(self) -> None:
        if self.available:
            await self.coordinator.async_retrain_selected_profile()


class RevokeSelectedGraduationButton(PowerFingerprintEntity, ButtonEntity):
    _attr_icon = "mdi:school-off-outline"
    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Revoke selected Powercalc graduation"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_revoke_selected_graduation"
    @property
    def available(self) -> bool:
        target = self.coordinator.profile_management_target
        profile = self.coordinator.device_profiles.get(target) if target else None
        return bool(profile and profile.powercalc_exported)
    async def async_press(self) -> None:
        if self.available:
            await self.coordinator.async_revoke_selected_graduation()
