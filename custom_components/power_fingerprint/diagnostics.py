"""Diagnostics support for Power Fingerprint."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .coordinator import PowerFingerprintCoordinator
from .naming import source_display_name


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Return non-secret model and signal diagnostics."""
    coordinator: PowerFingerprintCoordinator = entry.runtime_data
    registry = er.async_get(hass)
    source_state = hass.states.get(coordinator.source_entity)
    source_reg = registry.async_get(coordinator.source_entity)
    meter_details: dict[str, dict] = {}
    for entity_id in coordinator.meter_entities:
        reg = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
        meter_details[entity_id] = {
            "platform": reg.platform if reg else None,
            "state": state.state if state else None,
            "unit": state.attributes.get("unit_of_measurement") if state else None,
            "friendly_name": source_display_name(hass, entity_id),
            "currently_missing": entity_id in coordinator.missing_meters,
            "currently_stale": entity_id in coordinator.stale_meters,
            "currently_quarantined": entity_id in coordinator.quarantined_meters,
            "recovering_before_rejoin": entity_id in coordinator.recovering_meters,
            "last_trusted_power_w": coordinator._meter_cache.get(entity_id, (None, None))[0],
            "observed_peak_power_w": coordinator._meter_peak_w.get(entity_id),
        }

    tracked_details: dict[str, dict] = {}
    for entity_id in coordinator.tracked_entities:
        state = hass.states.get(entity_id)
        tracked_details[entity_id] = {
            "friendly_name": source_display_name(hass, entity_id),
            "state": state.state if state else None,
            "model_attributes": coordinator.device_attributes(entity_id),
            "isolation_attributes": coordinator.device_isolation_attributes(entity_id),
        }

    return {
        "configuration": {**entry.data, **entry.options},
        "state": coordinator.snapshot(),
        "source_details": {
            "entity_id": coordinator.source_entity,
            "platform": source_reg.platform if source_reg else None,
            "state": source_state.state if source_state else None,
            "unit": source_state.attributes.get("unit_of_measurement") if source_state else None,
            "last_reported": source_state.last_reported.isoformat() if source_state else None,
            "last_updated": source_state.last_updated.isoformat() if source_state else None,
        },
        "meter_details": meter_details,
        "tracked_details": tracked_details,
        "models": {eid: model.as_dict() for eid, model in coordinator.models.items()},
        "fingerprint_profiles": {eid: model.fingerprint_summaries() for eid, model in coordinator.models.items()},
        "device_profiles": {eid: profile.as_dict() for eid, profile in coordinator.device_profiles.items()},
        "migration_cleanup": dict(coordinator.migration_cleanup),
        "outdoor_ensemble": {
            **coordinator.outdoor_ensemble_attributes,
            "recent_equations": list(coordinator._outdoor_ensemble_equations)[-30:],
        },
        "profile_lifecycle": {
            eid: {
                "lifecycle_state": profile.lifecycle_state,
                "powercalc_exported": profile.powercalc_exported,
                "powercalc_active": profile.powercalc_active,
                "powercalc_profile_type": profile.powercalc_profile_type,
                "powercalc_handoff_status": profile.powercalc_handoff_status,
                "powercalc_power_sensor_id": profile.powercalc_power_sensor_id,
                "powercalc_energy_sensor_id": profile.powercalc_energy_sensor_id,
                "audit_observations": profile.audit_observations,
                "audit_good": profile.audit_good,
                "audit_bad": profile.audit_bad,
                "audit_consecutive_bad": profile.audit_consecutive_bad,
                "audit_last_error_pct": profile.audit_last_error_pct,
                "audit_last_at": profile.audit_last_at,
            }
            for eid, profile in coordinator.device_profiles.items()
        },
        "calibration_batch": {
            "running": coordinator.calibration_batch_running,
            "stop_requested": coordinator.calibration_batch_stop_requested,
            "status": coordinator.calibration_batch_status,
            "started_at": coordinator.calibration_batch_started_at,
            "total": coordinator.calibration_batch_total,
            "completed": coordinator.calibration_batch_completed,
            "validated": coordinator.calibration_batch_validated,
            "failed": coordinator.calibration_batch_failed,
            "skipped": coordinator.calibration_batch_skipped,
            "retried": coordinator.calibration_batch_retried,
            "current_entity": coordinator.calibration_batch_current_entity,
            "last_result": coordinator.calibration_batch_last_result,
            "candidates": coordinator.calibration_training_candidates(),
            "results": list(coordinator.calibration_batch_results),
        },
        "legacy_inverter_migration": (
            coordinator._legacy_inverter_record.as_dict()
            if coordinator._legacy_inverter_record else None
        ),
    }
