"""Sensor platform for Power Fingerprint."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PowerFingerprintCoordinator
from .entity import PowerFingerprintEntity
from .naming import source_display_name


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        GlobalPowerSensor(coordinator, "metered", "Metered power", "metered_power_w"),
        GlobalPowerSensor(coordinator, "residual", "Unmetered residual power", "residual_power_w"),
        GlobalPowerSensor(coordinator, "fingerprinted", "Fingerprint identified power", "fingerprinted_power_w"),
        GlobalPowerSensor(coordinator, "unexplained", "Unexplained power", "unexplained_power_w"),
        CoverageSensor(coordinator),
        LearningStatusSensor(coordinator),
        LiveIsolationSummarySensor(coordinator),
        OutdoorBlockPowerSensor(coordinator),
        OutdoorBlockEnergySensor(coordinator),
    ]
    for eid in coordinator.tracked_entities:
        entities.append(DevicePowerSensor(coordinator, eid))
        entities.append(DeviceEnergySensor(coordinator, eid))
        entities.append(IsolatedPowerSensor(coordinator, eid))
        entities.append(IsolatedEnergySensor(coordinator, eid))
    async_add_entities(entities)


class GlobalPowerSensor(PowerFingerprintEntity, SensorEntity):
    """One global power accounting sensor."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, key: str, name: str, data_key: str) -> None:
        super().__init__(coordinator)
        self._data_key = data_key
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}_power"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get(self._data_key) if self.coordinator.data else None


class CoverageSensor(PowerFingerprintEntity, SensorEntity):
    """How much of residual power is currently identified."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:chart-donut"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Fingerprint coverage"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_coverage"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get("coverage_percent")
        return float(value) if value is not None else None


class LiveIsolationSummarySensor(PowerFingerprintEntity, SensorEntity):
    """Small dashboard summary that changes only when its counts/status change."""

    _attr_icon = "mdi:fingerprint"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Live isolation summary"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_live_isolation_summary"

    @property
    def native_value(self) -> int:
        summary = (self.coordinator.data or {}).get("live_isolation_summary", {})
        return int(summary.get("ready_count", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict((self.coordinator.data or {}).get("live_isolation_summary", {}))


class LearningStatusSensor(PowerFingerprintEntity, SensorEntity):
    """Human-readable learning diagnostics."""

    _attr_icon = "mdi:brain"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_name = "Learning status"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_learning_status"

    @property
    def native_value(self) -> str:
        if not self.coordinator.learning_enabled:
            return "paused"
        if self.coordinator.calibration_batch_running:
            return "calibrating_batch"
        if self.coordinator.calibrating_entity:
            return "calibrating"
        data = self.coordinator.data or {}
        if data.get("source_power_w") is None:
            return "waiting_for_source"
        if self.coordinator.quarantined_meters:
            return "learning_with_quarantined_meters"
        if self.coordinator.stale_meters or self.coordinator.recovering_meters:
            return "learning_with_degraded_meter_data"
        return "learning"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        summary = data.get("live_isolation_summary", {})
        return {
            "learned_profile_ready_count": summary.get("learned_ready_count", 0),
            "not_yet_learned_count": summary.get("not_yet_learned_count", 0),
            "fixed_placeholder_count": summary.get("placeholder_count", 0),
            "fixed_locked_count": summary.get("locked_count", 0),
            "accepted_observations": data.get("accepted_observations", 0),
            "rejected_observations": data.get("rejected_observations", 0),
            "concurrent_decomposed_observations": data.get("concurrent_decomposed_observations", 0),
            "coverage_percent": data.get("coverage_percent", 0.0),
            "validated_coverage_percent": data.get("validated_coverage_percent", 0.0),
            "potential_coverage_percent": data.get("potential_coverage_percent", 0.0),
            "validated_runtime_power_w": data.get("validated_runtime_power_w", 0.0),
            "candidate_runtime_power_w": data.get("candidate_runtime_power_w", 0.0),
            "candidate_profile_power_w": data.get("candidate_profile_power_w", 0.0),
            "source_power_w": data.get("source_power_w"),
            "missing_meters": data.get("missing_meters", []),
            "stale_meters": data.get("stale_meters", []),
            "quarantined_meters": data.get("quarantined_meters", []),
            "recovering_meters": data.get("recovering_meters", []),
            "active_meter_count": data.get("active_meter_count", 0),
            "total_meter_count": data.get("total_meter_count", 0),
            "quarantined_meter_risk_w": data.get("quarantined_meter_risk_w", 0.0),
            "samples_last_60s": data.get("samples_last_60s", 0),
            "source_reports_last_60s": data.get("source_reports_last_60s", 0),
            "source_report_age_seconds": data.get("source_report_age_seconds"),
            "source_report_interval_seconds": data.get("source_report_interval_seconds"),
            "ordinary_edge_timeout_seconds": data.get("ordinary_edge_timeout_seconds"),
            "learning_blocker_counts": data.get("learning_blocker_counts", {}),
            "valid_samples_last_60s": data.get("valid_samples_last_60s", 0),
            "residual_median_60s": data.get("residual_median_60s"),
            "residual_mad_60s": data.get("residual_mad_60s"),
            "meter_transition_threshold_w": data.get("meter_transition_threshold_w"),
            "meter_transition_guard_seconds": data.get("meter_transition_guard_seconds"),
            "meter_transients_last_60s": data.get("meter_transients_last_60s", 0),
            "rejection_reasons": data.get("rejection_reasons", {}),
            "session_accepted_observations": data.get("session_accepted_observations", 0),
            "session_rejected_observations": data.get("session_rejected_observations", 0),
            "session_rejection_reasons": data.get("session_rejection_reasons", {}),
            "last_observation": data.get("last_observation"),
            "calibrating_entity": data.get("calibrating_entity"),
            "calibration_phase": data.get("calibration_phase"),
            "calibration_campaign_level_pct": data.get("calibration_campaign_level_pct"),
            "calibration_batch_running": data.get("calibration_batch_running", False),
            "calibration_batch_stop_requested": data.get("calibration_batch_stop_requested", False),
            "calibration_batch_status": data.get("calibration_batch_status"),
            "calibration_batch_started_at": data.get("calibration_batch_started_at"),
            "calibration_batch_total": data.get("calibration_batch_total", 0),
            "calibration_batch_completed": data.get("calibration_batch_completed", 0),
            "calibration_batch_validated": data.get("calibration_batch_validated", 0),
            "calibration_batch_failed": data.get("calibration_batch_failed", 0),
            "calibration_batch_skipped": data.get("calibration_batch_skipped", 0),
            "calibration_batch_retried": data.get("calibration_batch_retried", 0),
            "calibration_batch_current_entity": data.get("calibration_batch_current_entity"),
            "calibration_batch_last_result": data.get("calibration_batch_last_result"),
            "selected_calibration_last_result": data.get("selected_calibration_last_result"),
            "calibration_batch_candidate_count": data.get("calibration_batch_candidate_count", 0),
            "calibration_batch_candidates": data.get("calibration_batch_candidates", []),
            "calibration_batch_results": data.get("calibration_batch_results", []),
            "calibration_batch_outcome_counts": data.get("calibration_batch_outcome_counts", {}),
            "inverter_running": data.get("inverter_running"),
            "unified_model_version": data.get("unified_model_version"),
            "auto_export_energy_dashboard": data.get("auto_export_energy_dashboard"),
            "energy_dashboard_ready_count": data.get("energy_dashboard_ready_count", 0),
            "energy_dashboard_exported_count": data.get("energy_dashboard_exported_count", 0),
            "energy_dashboard_ready_entities": data.get("energy_dashboard_ready_entities", []),
            "energy_dashboard_exported_entities": data.get("energy_dashboard_exported_entities", []),
            "energy_dashboard_sync_error": data.get("energy_dashboard_sync_error"),
            "powercalc_graduation_eligible_count": data.get("powercalc_graduation_eligible_count", 0),
            "powercalc_graduation_eligible_entities": data.get("powercalc_graduation_eligible_entities", []),
            "powercalc_exported_count": data.get("powercalc_exported_count", 0),
            "powercalc_exported_entities": data.get("powercalc_exported_entities", []),
            "powercalc_active_count": data.get("powercalc_active_count", 0),
            "powercalc_active_entities": data.get("powercalc_active_entities", []),
            "powercalc_pending_handoff_entities": data.get("powercalc_pending_handoff_entities", []),
            "profile_lifecycle_counts": data.get("profile_lifecycle_counts", {}),
            "profile_learning_entities": data.get("profile_learning_entities", []),
            "profile_candidate_entities": data.get("profile_candidate_entities", []),
            "profile_validated_entities": data.get("profile_validated_entities", []),
            "profile_native_entities": data.get("profile_native_entities", []),
            "profile_audit_entities": data.get("profile_audit_entities", []),
            "profile_drift_entities": data.get("profile_drift_entities", []),
            "profile_retraining_entities": data.get("profile_retraining_entities", []),
            "powercalc_last_export": data.get("powercalc_last_export"),
            "runtime_profile_fallback_active_count": data.get("runtime_profile_fallback_active_count", 0),
            "energy_outlier_filtered_count": data.get("energy_outlier_filtered_count", 0),
            "energy_outlier_filtered_entities": data.get("energy_outlier_filtered_entities", []),
            "fixed_power_target": data.get("fixed_power_target"),
            "fixed_power_target_attributes": data.get("fixed_power_target_attributes", {}),
            "manual_fixed_count": data.get("manual_fixed_count", 0),
            "manual_fixed_placeholder_entities": data.get("manual_fixed_placeholder_entities", []),
            "manual_fixed_locked_entities": data.get("manual_fixed_locked_entities", []),
            "outdoor_block_entities": data.get("outdoor_block_entities", []),
            "outdoor_block_active": data.get("outdoor_block_active", False),
            "outdoor_block_power_w": data.get("outdoor_block_power_w", 0.0),
            "outdoor_block_energy_kwh": data.get("outdoor_block_energy_kwh", 0.0),
            "outdoor_block_legacy_energy_kwh": data.get("outdoor_block_legacy_energy_kwh", 0.0),
            "outdoor_block_sample_count": data.get("outdoor_block_sample_count", 0),
            "outdoor_block_mode": data.get("outdoor_block_mode", "idle"),
            "outdoor_ensemble_mature": data.get("outdoor_ensemble_mature", False),
            "outdoor_ensemble_confidence": data.get("outdoor_ensemble_confidence", 0.0),
            "outdoor_ensemble_equation_count": data.get("outdoor_ensemble_equation_count", 0),
            "outdoor_ensemble_unique_signature_count": data.get("outdoor_ensemble_unique_signature_count", 0),
            "outdoor_ensemble_rejected_equations": data.get("outdoor_ensemble_rejected_equations", {}),
            "outdoor_ensemble_rank": data.get("outdoor_ensemble_rank", 0),
            "outdoor_ensemble_required_rank": data.get("outdoor_ensemble_required_rank", 0),
            "outdoor_ensemble_rmse_w": data.get("outdoor_ensemble_rmse_w"),
            "outdoor_ensemble_member_power_w": data.get("outdoor_ensemble_member_power_w", {}),
            "outdoor_ensemble_member_evidence": data.get("outdoor_ensemble_member_evidence", {}),
            "outdoor_ensemble_predicted_total_w": data.get("outdoor_ensemble_predicted_total_w", 0.0),
            "outdoor_ensemble_individual_identified_w": data.get("outdoor_ensemble_individual_identified_w", 0.0),
        }


class OutdoorBlockPowerSensor(PowerFingerprintEntity, SensorEntity):
    """Aggregate residual assigned to the stable nighttime outdoor-light block."""
    _attr_name = "Outdoor lighting block power"
    _attr_icon = "mdi:outdoor-lamp"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_outdoor_block_power"

    @property
    def native_value(self) -> float:
        return self.coordinator.outdoor_block_power_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "members": list(self.coordinator.outdoor_block_entities),
            "aggregate_constraint": True,
            **self.coordinator.outdoor_ensemble_attributes,
        }


class OutdoorBlockEnergySensor(PowerFingerprintEntity, SensorEntity):
    """Energy integrated from aggregate outdoor-block attribution."""
    _attr_name = "Outdoor lighting block energy"
    _attr_icon = "mdi:lightning-bolt"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_outdoor_block_energy"

    @property
    def native_value(self) -> float:
        return self.coordinator.outdoor_block_energy_kwh

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "members": list(self.coordinator.outdoor_block_entities),
            "aggregate_constraint": True,
            **self.coordinator.outdoor_ensemble_attributes,
        }


class DevicePowerSensor(PowerFingerprintEntity, SensorEntity):
    """Deprecated power alias retained for entity-registry compatibility."""

    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, source_entity: str) -> None:
        super().__init__(coordinator)
        self.source_entity = source_entity
        key = coordinator.source_registry_key(source_entity)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}_estimated_power"

    @property
    def name(self) -> str:
        """Return a name that follows source-entity renames."""
        return f"{source_display_name(self.coordinator.hass, self.source_entity)} estimated power"

    @property
    def native_value(self) -> float:
        return self.coordinator.device_power_w(self.source_entity)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        power_entity, _energy_entity = self.coordinator._canonical_sensor_entity_ids(self.source_entity)
        return {
            "source_entity": self.source_entity,
            "deprecated": True,
            "replacement_entity": power_entity,
            **self.coordinator.device_attributes(self.source_entity),
        }


class IsolatedPowerSensor(PowerFingerprintEntity, SensorEntity):
    """Live session-isolated power for one tracked source entity."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:vector-difference"

    def __init__(self, coordinator, source_entity: str) -> None:
        super().__init__(coordinator)
        self.source_entity = source_entity
        key = coordinator.source_registry_key(source_entity)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}_isolated_power"

    @property
    def name(self) -> str:
        """Return a name that follows source-entity renames."""
        return f"{source_display_name(self.coordinator.hass, self.source_entity)} isolated power"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.device_isolated_power_w(self.source_entity)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source_entity": self.source_entity, **self.coordinator.device_runtime_attributes(self.source_entity)}


class IsolatedEnergySensor(PowerFingerprintEntity, SensorEntity):
    """Cumulative energy integrated only from trustworthy isolated power."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, coordinator, source_entity: str) -> None:
        super().__init__(coordinator)
        self.source_entity = source_entity
        key = coordinator.source_registry_key(source_entity)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}_isolated_energy"

    @property
    def name(self) -> str:
        """Return a name that follows source-entity renames."""
        return f"{source_display_name(self.coordinator.hass, self.source_entity)} isolated energy"

    @property
    def native_value(self) -> float:
        return self.coordinator.device_isolated_energy_kwh(self.source_entity)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_entity": self.source_entity,
            "method": "integrated unified live session isolation",
            **self.coordinator.device_runtime_attributes(self.source_entity),
        }


class DeviceEnergySensor(PowerFingerprintEntity, SensorEntity):
    """Deprecated cumulative-energy compatibility entity."""

    _attr_entity_registry_enabled_default = False
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, source_entity: str) -> None:
        super().__init__(coordinator)
        self.source_entity = source_entity
        key = coordinator.source_registry_key(source_entity)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}_estimated_energy"

    @property
    def name(self) -> str:
        """Return a name that follows source-entity renames."""
        return f"{source_display_name(self.coordinator.hass, self.source_entity)} estimated energy"

    @property
    def native_value(self) -> float:
        return self.coordinator.device_energy_kwh(self.source_entity)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _power_entity, energy_entity = self.coordinator._canonical_sensor_entity_ids(self.source_entity)
        return {
            "source_entity": self.source_entity,
            "method": "legacy cumulative offset + unified live-isolation energy increments",
            "deprecated": True,
            "replacement_entity": energy_entity,
        }
