"""Select platform for calibration and profile lifecycle targets."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerFingerprintCoordinator
from .const import (
    MANUAL_FIXED_MODE_AUTO,
    MANUAL_FIXED_MODE_LOCKED,
    MANUAL_FIXED_MODE_PLACEHOLDER,
)
from .entity import PowerFingerprintEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    async_add_entities([
        CalibrationTargetSelect(coordinator),
        PowercalcGraduationTargetSelect(coordinator),
        ProfileManagementTargetSelect(coordinator),
        FixedPowerTargetSelect(coordinator),
        FixedPowerModeSelect(coordinator),
    ])


class CalibrationTargetSelect(PowerFingerprintEntity, SelectEntity):
    _attr_icon = "mdi:target"

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Calibration target"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_calibration_target"
        pairs = coordinator.calibration_options()
        entity_ids = {eid for eid, _label in pairs}
        if pairs and coordinator.calibration_target not in entity_ids:
            coordinator.calibration_target = pairs[0][0]

    @property
    def options(self) -> list[str]:
        return [label for _eid, label in self.coordinator.calibration_options()]

    @property
    def current_option(self) -> str | None:
        target = self.coordinator.calibration_target
        by_eid = {eid: label for eid, label in self.coordinator.calibration_options()}
        return by_eid.get(target) if target else None

    async def async_select_option(self, option: str) -> None:
        by_label = {label: eid for eid, label in self.coordinator.calibration_options()}
        if option not in by_label:
            raise ValueError(f"Unknown calibration option: {option}")
        self.coordinator.calibration_target = by_label[option]
        self.async_write_ha_state()


class PowercalcGraduationTargetSelect(PowerFingerprintEntity, SelectEntity):
    """Select a validated fixed/linear model eligible for local Powercalc."""

    _attr_icon = "mdi:school-outline"

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Powercalc graduation target"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_powercalc_graduation_target"

    @property
    def options(self) -> list[str]:
        pairs = self.coordinator.powercalc_graduation_options()
        entity_ids = {eid for eid, _label in pairs}
        if pairs and self.coordinator.powercalc_graduation_target not in entity_ids:
            self.coordinator.powercalc_graduation_target = pairs[0][0]
        elif not pairs:
            self.coordinator.powercalc_graduation_target = None
        return [label for _eid, label in pairs]

    @property
    def current_option(self) -> str | None:
        pairs = self.coordinator.powercalc_graduation_options()
        by_eid = {eid: label for eid, label in pairs}
        target = self.coordinator.powercalc_graduation_target
        if target not in by_eid:
            if pairs:
                target = pairs[0][0]
                self.coordinator.powercalc_graduation_target = target
            else:
                self.coordinator.powercalc_graduation_target = None
                return None
        return by_eid.get(target)

    async def async_select_option(self, option: str) -> None:
        by_label = {label: eid for eid, label in self.coordinator.powercalc_graduation_options()}
        if option not in by_label:
            raise ValueError(f"Unknown Powercalc graduation option: {option}")
        self.coordinator.powercalc_graduation_target = by_label[option]
        self.async_write_ha_state()


class ProfileManagementTargetSelect(PowerFingerprintEntity, SelectEntity):
    """Select any tracked device for lifecycle management/retraining."""

    _attr_icon = "mdi:state-machine"

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Profile management target"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_profile_management_target"

    @property
    def options(self) -> list[str]:
        pairs = self.coordinator.profile_management_options()
        entity_ids = {eid for eid, _label in pairs}
        if pairs and self.coordinator.profile_management_target not in entity_ids:
            self.coordinator.profile_management_target = pairs[0][0]
        return [label for _eid, label in pairs]

    @property
    def current_option(self) -> str | None:
        pairs = self.coordinator.profile_management_options()
        by_eid = {eid: label for eid, label in pairs}
        target = self.coordinator.profile_management_target
        if target not in by_eid:
            if not pairs:
                return None
            target = pairs[0][0]
            self.coordinator.profile_management_target = target
        return by_eid.get(target)

    async def async_select_option(self, option: str) -> None:
        by_label = {label: eid for eid, label in self.coordinator.profile_management_options()}
        if option not in by_label:
            raise ValueError(f"Unknown profile management option: {option}")
        self.coordinator.profile_management_target = by_label[option]
        self.async_write_ha_state()


class FixedPowerTargetSelect(PowerFingerprintEntity, SelectEntity):
    """Select any tracked device for a manual fixed-power value."""

    _attr_icon = "mdi:flash"

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Fixed power target"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fixed_power_target"

    @property
    def options(self) -> list[str]:
        pairs = self.coordinator.fixed_power_options()
        entity_ids = {eid for eid, _label in pairs}
        if pairs and self.coordinator.fixed_power_target not in entity_ids:
            self.coordinator.fixed_power_target = pairs[0][0]
        return [label for _eid, label in pairs]

    @property
    def current_option(self) -> str | None:
        pairs = self.coordinator.fixed_power_options()
        by_eid = {eid: label for eid, label in pairs}
        target = self.coordinator.fixed_power_target
        if target not in by_eid:
            if not pairs:
                self.coordinator.fixed_power_target = None
                return None
            target = pairs[0][0]
            self.coordinator.fixed_power_target = target
        return by_eid.get(target)

    async def async_select_option(self, option: str) -> None:
        by_label = {label: eid for eid, label in self.coordinator.fixed_power_options()}
        if option not in by_label:
            raise ValueError(f"Unknown fixed-power target: {option}")
        self.coordinator.fixed_power_target = by_label[option]
        self.coordinator.async_set_updated_data(self.coordinator.snapshot())


class FixedPowerModeSelect(PowerFingerprintEntity, SelectEntity):
    """Choose automatic, placeholder, or locked handling for the selected device."""

    _attr_icon = "mdi:lock-clock"
    _LABEL_TO_MODE = {
        "Automatic": MANUAL_FIXED_MODE_AUTO,
        "Placeholder — keep learning": MANUAL_FIXED_MODE_PLACEHOLDER,
        "Locked — stop learning": MANUAL_FIXED_MODE_LOCKED,
    }
    _MODE_TO_LABEL = {value: key for key, value in _LABEL_TO_MODE.items()}

    def __init__(self, coordinator: PowerFingerprintCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Fixed power mode"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fixed_power_mode"

    @property
    def options(self) -> list[str]:
        return list(self._LABEL_TO_MODE)

    @property
    def current_option(self) -> str | None:
        attrs = self.coordinator.fixed_power_target_attributes()
        mode = attrs.get("mode")
        return self._MODE_TO_LABEL.get(mode, "Automatic")

    @property
    def extra_state_attributes(self) -> dict:
        return self.coordinator.fixed_power_target_attributes()

    async def async_select_option(self, option: str) -> None:
        mode = self._LABEL_TO_MODE.get(option)
        if mode is None:
            raise ValueError(f"Unknown fixed-power mode: {option}")
        attrs = self.coordinator.fixed_power_target_attributes()
        entity_id = attrs.get("entity_id")
        if not entity_id:
            raise ValueError("No fixed-power target selected")
        await self.coordinator.async_set_fixed_power_mode(entity_id, mode)
