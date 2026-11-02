"""Event-driven learning engine for Power Fingerprint."""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
from pathlib import Path
import time
from typing import Any

from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.state import async_reproduce_state
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import PowerConverter

from .areas import async_setup_area_inheritance
from .const import (
    CONF_ADDITIONAL_METERS,
    CONF_AUTO_EXPORT_ENERGY,
    CONF_CALIBRATION_EXCLUDED_ENTITIES,
    CONF_OUTDOOR_BLOCK_ENTITIES,
    CONF_CONCURRENCY_SECONDS,
    CONF_EXCLUDED_METERS,
    CONF_INVERTER_CLIMATE,
    CONF_INVERTER_CONFIRM_SECONDS,
    CONF_INVERTER_MIN_STEP_W,
    CONF_MAX_NOISE_W,
    CONF_METER_GRACE_SECONDS,
    CONF_METER_REJOIN_SECONDS,
    CONF_MAX_OBSERVATIONS,
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
    DEFAULT_OUTDOOR_BLOCK_SETTLE_SECONDS,
    OUTDOOR_ENSEMBLE_MAX_PER_SIGNATURE,
    OUTDOOR_ENSEMBLE_SINGLE_MAX_PRIOR_MULTIPLIER,
    OUTDOOR_ENSEMBLE_SINGLE_ABSOLUTE_MARGIN_W,
    OUTDOOR_ENSEMBLE_MULTI_ABSOLUTE_MARGIN_W,
    MANUAL_FIXED_MODE_AUTO,
    MANUAL_FIXED_MODE_PLACEHOLDER,
    MANUAL_FIXED_MODE_LOCKED,
    MANUAL_FIXED_MODES,
    MANUAL_FIXED_SUGGESTED_W,
    DEFAULT_CONCURRENCY_SECONDS,
    DEFAULT_INVERTER_CONFIRM_SECONDS,
    DEFAULT_INVERTER_MIN_STEP_W,
    DEFAULT_MAX_NOISE_W,
    DEFAULT_METER_GRACE_SECONDS,
    DEFAULT_METER_REJOIN_SECONDS,
    DEFAULT_MAX_OBSERVATIONS,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_MIN_STEP_W,
    DEFAULT_POST_WINDOW_SECONDS,
    DEFAULT_READY_VALIDATED_SESSIONS,
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    DEFAULT_PRE_WINDOW_SECONDS,
    DEFAULT_SETTLE_SECONDS,
    DOMAIN,
    FINGERPRINT_SCHEMA_VERSION,
    MODEL_SCHEMA_VERSION,
    SAFE_CALIBRATION_DOMAINS,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    VERSION,
)
from .models import (
    DeviceModel,
    DeviceProfile,
    InverterModel,
    IsolationSession,
    build_session_fingerprint,
    Observation,
    PendingTransition,
    Sample,
    mad,
    robust_median,
    utc_iso,
    prune_unpaired_latent_start_observations,
    prune_malformed_schema5_session_fingerprints,
    normalize_fingerprint_channel_alias,
)
from .discovery import default_device_profile
from .naming import source_display_name

_LOGGER = logging.getLogger(__name__)


class CalibrationDeferred(Exception):
    """A calibration could not continue because the meter baseline was not quiet."""


OFFISH_MEDIA_STATES = {"off", "standby", STATE_UNAVAILABLE, STATE_UNKNOWN}
ACTIVE_HVAC_ACTIONS = {"cooling", "heating", "fan", "drying", "preheating", "defrosting"}
ACTIVE_HVAC_MODES = {"cool", "heat", "heat_cool", "auto", "dry"}

# Live-isolation edge windows are intentionally much shorter than the legacy
# fixed-model windows. They provide responsive session baselines while still
# collecting enough 2-second residual samples for robust medians.
ISOLATION_PRE_SECONDS = 12.0
ISOLATION_SETTLE_SECONDS = 6.0
ISOLATION_POST_SECONDS = 6.0
ORDINARY_EDGE_SAMPLE_TIMEOUT_SECONDS = 18.0
MAX_SOURCE_ADAPTED_POST_SECONDS = 90.0
ISOLATION_CONCURRENCY_SECONDS = 8.0
# Climate integrations often report hvac_action a few seconds before/after the
# electrical compressor edge. Give their explicit edge task a little extra time
# to collect the required valid residual samples, and never train a compressor
# channel from a tiny thermostat/control-board step.
CLIMATE_EDGE_SAMPLE_TIMEOUT_SECONDS = 18.0
CLIMATE_COMPRESSOR_MIN_STEP_W = 50.0
CLIMATE_FAN_MIN_STEP_W = 20.0
# HVAC integrations can report hvac_action/state OFF before the compressor or
# indoor fan has electrically settled. Keep a validated start alive for a bounded
# grace interval so the actual electrical stop can be observed instead of
# rejecting the session immediately.
CLIMATE_ELECTRICAL_STOP_GRACE_SECONDS = 120.0
# If the residual baseline drifts while an HVAC run is active, the post-run
# residual may never return to the exact pre-run baseline even though the HVAC
# load has clearly shut down.  At the end of the normal grace interval, allow a
# conservative waveform-based recovery: look for a sustained relative drop near
# the OFF cue, while still rejecting noisy/ambiguous sessions.
CLIMATE_RELATIVE_STOP_LOOKBACK_SECONDS = 60.0
CLIMATE_RELATIVE_STOP_PRE_SECONDS = 16.0
CLIMATE_RELATIVE_STOP_POST_SECONDS = 16.0
CLIMATE_RELATIVE_STOP_EDGE_GAP_SECONDS = 3.0
CLIMATE_RELATIVE_STOP_MIN_DROP_FRACTION = 0.25
CLIMATE_RELATIVE_STOP_MAX_POST_PRE_FRACTION = 0.45
CLIMATE_RELATIVE_STOP_ABSOLUTE_POST_CEILING_W = 300.0
CLIMATE_RELATIVE_STOP_START_EDGE_MULTIPLIER = 1.25

# 0.6 learned-profile lifecycle / Powercalc audit defaults.
PROFILE_AUDIT_RELATIVE_TOLERANCE = 0.25
PROFILE_AUDIT_ABSOLUTE_TOLERANCE_W = 5.0
PROFILE_AUDIT_BAD_STREAK_FOR_DRIFT = 3
PROFILE_LINEAR_MIN_DISTINCT_LEVELS = 3
PROFILE_LINEAR_MIN_SESSIONS = 4
PROFILE_LINEAR_MIN_LEVEL_SPAN = 0.35

# 0.6.1 Powercalc-inspired runtime/calibration safeguards.
CALIBRATION_SWEEP_LEVELS_PCT = (25, 50, 75, 100)
ENERGY_OUTLIER_HISTORY_POINTS = 15
ENERGY_OUTLIER_MIN_HISTORY = 5
ENERGY_OUTLIER_SIGMA = 8.0
ENERGY_OUTLIER_MIN_ABSOLUTE_W = 25.0
ENERGY_OUTLIER_RELATIVE_RISE = 1.00

# 0.7 nighttime outdoor ensemble learner. Stable plateaus are differenced so
# unrelated background residual cancels rather than being attributed to lights.
OUTDOOR_ENSEMBLE_MAX_EQUATIONS = 160
OUTDOOR_ENSEMBLE_MAX_PAIR_GAP_SECONDS = 180.0
OUTDOOR_ENSEMBLE_PLATEAU_WINDOW_SECONDS = 12.0
OUTDOOR_ENSEMBLE_MIN_PLATEAU_SAMPLES = 5
OUTDOOR_ENSEMBLE_SETTLE_SECONDS = 30.0
OUTDOOR_ENSEMBLE_RIDGE_FRACTION = 0.02
OUTDOOR_ENSEMBLE_MIN_MEMBER_EVIDENCE = 2
OUTDOOR_ENSEMBLE_SINGLE_EDGE_WEIGHT = 0.60
OUTDOOR_ENSEMBLE_MULTI_EDGE_WEIGHT = 0.20


class PowerFingerprintCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Own the residual stream, learning models, inference, and persistence."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize engine."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=None)
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        self.source_entity: str = cfg[CONF_SOURCE_POWER]
        self.tracked_entities: list[str] = list(cfg.get(CONF_TRACKED_ENTITIES, []))
        # 0.4 retires the separate inverter power model. The config-entry migration
        # folds the old inverter selection into tracked_entities. Keep the legacy
        # key only so first-run storage migration can find the old Gree record; do
        # not force-readd it if the user later removes that climate entity.
        self.legacy_inverter_entity: str | None = cfg.get(CONF_INVERTER_CLIMATE) or None
        self.tracked_entities = list(dict.fromkeys(self.tracked_entities))
        self.use_energy_meters: bool = bool(cfg.get(CONF_USE_ENERGY_METERS, True))
        self.auto_export_energy: bool = bool(cfg.get(CONF_AUTO_EXPORT_ENERGY, DEFAULT_AUTO_EXPORT_ENERGY))
        self.additional_meters: list[str] = list(cfg.get(CONF_ADDITIONAL_METERS, []))
        self.excluded_meters: set[str] = set(cfg.get(CONF_EXCLUDED_METERS, []))
        self.calibration_excluded_entities: set[str] = set(cfg.get(CONF_CALIBRATION_EXCLUDED_ENTITIES, []))
        configured_block = cfg.get(CONF_OUTDOOR_BLOCK_ENTITIES)
        self._outdoor_block_explicitly_configured = configured_block is not None
        if configured_block is None:
            configured_block = []
        self.outdoor_block_entities: list[str] = [eid for eid in configured_block if eid in self.tracked_entities]
        self._outdoor_block_entity_set: set[str] = set(self.outdoor_block_entities)
        self._outdoor_block_last_transition_ts: float = 0.0
        self._outdoor_block_samples: deque[tuple[float, float]] = deque(maxlen=150)
        self._outdoor_block_power_w: float = 0.0
        self._outdoor_block_energy_kwh: float = 0.0
        self._outdoor_block_last_energy_ts: float | None = None
        self._outdoor_block_last_energy_power_w: float = 0.0
        self._outdoor_block_sample_count: int = 0
        self._outdoor_block_active: bool = False
        # 0.7 ensemble state. The legacy aggregate remains as a conservative
        # fallback until the difference-equation model is identifiable.
        self._outdoor_ensemble_equations: list[dict[str, Any]] = []
        self._outdoor_ensemble_member_power_w: dict[str, float] = {}
        self._outdoor_ensemble_member_evidence: dict[str, int] = {}
        self._outdoor_ensemble_rank: int = 0
        self._outdoor_ensemble_rmse_w: float | None = None
        self._outdoor_ensemble_mature: bool = False
        self._outdoor_ensemble_confidence: float = 0.0
        self._outdoor_ensemble_last_plateau: dict[str, Any] | None = None
        self._outdoor_block_mode: str = "idle"
        self._outdoor_block_predicted_total_w: float = 0.0
        self._outdoor_block_individual_identified_w: float = 0.0
        self._outdoor_block_legacy_energy_kwh: float = 0.0
        self._outdoor_ensemble_rejected_equations: Counter[str] = Counter()
        self._outdoor_ensemble_unique_signature_count: int = 0
        self.settle_seconds = int(cfg.get(CONF_SETTLE_SECONDS, DEFAULT_SETTLE_SECONDS))
        self.pre_window_seconds = int(cfg.get(CONF_PRE_WINDOW_SECONDS, DEFAULT_PRE_WINDOW_SECONDS))
        self.post_window_seconds = int(cfg.get(CONF_POST_WINDOW_SECONDS, DEFAULT_POST_WINDOW_SECONDS))
        self.max_noise_w = float(cfg.get(CONF_MAX_NOISE_W, DEFAULT_MAX_NOISE_W))
        self.min_step_w = float(cfg.get(CONF_MIN_STEP_W, DEFAULT_MIN_STEP_W))
        self.concurrency_seconds = int(cfg.get(CONF_CONCURRENCY_SECONDS, DEFAULT_CONCURRENCY_SECONDS))
        self.min_samples = int(cfg.get(CONF_MIN_SAMPLES, DEFAULT_MIN_SAMPLES))
        self.latent_climate_min_step_w = float(cfg.get(CONF_INVERTER_MIN_STEP_W, DEFAULT_INVERTER_MIN_STEP_W))
        self.latent_climate_confirm_seconds = int(cfg.get(CONF_INVERTER_CONFIRM_SECONDS, DEFAULT_INVERTER_CONFIRM_SECONDS))
        self.max_observations = int(cfg.get(CONF_MAX_OBSERVATIONS, DEFAULT_MAX_OBSERVATIONS))
        self.sample_interval_seconds = max(1, int(cfg.get(CONF_SAMPLE_INTERVAL_SECONDS, DEFAULT_SAMPLE_INTERVAL_SECONDS)))
        self.meter_grace_seconds = max(0, int(cfg.get(CONF_METER_GRACE_SECONDS, DEFAULT_METER_GRACE_SECONDS)))
        self.meter_rejoin_seconds = max(10, int(cfg.get(CONF_METER_REJOIN_SECONDS, DEFAULT_METER_REJOIN_SECONDS)))

        self.learning_enabled = True
        self.calibrating_entity: str | None = None
        self.calibration_phase = "idle"
        self.calibration_target: str | None = None
        self._calibration_lock = asyncio.Lock()
        self._calibration_batch_lock = asyncio.Lock()
        self.calibration_batch_running = False
        self.calibration_batch_stop_requested = False
        self.calibration_batch_total = 0
        self.calibration_batch_completed = 0
        self.calibration_batch_validated = 0
        self.calibration_batch_failed = 0
        self.calibration_batch_skipped = 0
        self.calibration_batch_retried = 0
        self.calibration_batch_current_entity: str | None = None
        self.calibration_batch_last_result: str | None = None
        self.selected_calibration_last_result: str | None = None
        self.calibration_batch_status = "idle"
        self.calibration_batch_started_at: str | None = None
        self.calibration_batch_candidates: list[str] = []
        self.calibration_batch_results: deque[dict[str, Any]] = deque(maxlen=50)
        self.calibration_campaign_level_pct: int | None = None

        # 0.5.7 local Powercalc graduation/export state. Export is deliberately
        # non-destructive: Power Fingerprint keeps running until the user has
        # restarted HA and confirmed the generated Powercalc sensors.
        self.powercalc_graduation_target: str | None = None
        self.profile_management_target: str | None = None
        self.fixed_power_target: str | None = None
        self.powercalc_last_export: dict[str, Any] | None = None

        self.meter_entities: list[str] = []
        self._energy_manager = None
        self._unsubs: list[Callable[[], None]] = []
        self._sample_buffer: deque[Sample] = deque(maxlen=3600)
        self._transition_log: deque[tuple[float, str]] = deque(maxlen=500)
        self._transition_detail_log: deque[dict[str, Any]] = deque(maxlen=500)
        self._concurrent_decomposed_count: int = 0
        self._meter_change_log: deque[tuple[float, str, float]] = deque(maxlen=1000)
        self._pending_tasks: set[asyncio.Task] = set()
        self._last_source_sample_ts: float | None = None
        self._source_report_times: deque[float] = deque(maxlen=30)
        self._last_energy_ts: float | None = None
        self._last_isolated_powers: dict[str, float | None] = {}
        # Energy integration gets a second, independent guard against isolated
        # upward spikes.  This deliberately does not alter the public power sensor
        # or learning evidence; it only prevents one anomalous sample from becoming
        # permanent cumulative kWh.
        self._energy_power_history: dict[str, deque[float]] = {
            eid: deque(maxlen=ENERGY_OUTLIER_HISTORY_POINTS) for eid in self.tracked_entities
        }
        self._energy_outlier_filtered_count: Counter[str] = Counter()
        self._energy_last_filter_reason: dict[str, str | None] = {}
        self._energy_last_guarded_power_w: dict[str, float | None] = {}
        self._runtime_fallback_last_w: dict[str, float | None] = {}
        self._meter_cache: dict[str, tuple[float, float]] = {}
        self._meter_peak_w: dict[str, float] = {}
        self._meter_unavailable_since: dict[str, float] = {}
        self._meter_recovery_since: dict[str, float] = {}
        self._quarantined_meters: set[str] = set()
        # Availability intervals are only recorded when a stale hold is proven
        # unsafe, or when a meter enters/leaves quarantine.  Short blips that
        # return at essentially the same power never poison learning.
        self._meter_availability_intervals: deque[tuple[float, float, str, str]] = deque(maxlen=500)
        self._sample_guard_until: float = 0.0
        self._held_meters_now: list[str] = []
        self._recovering_meters_now: list[str] = []
        self._last_publish_ts: float = 0.0
        self._last_periodic_save_ts: float = 0.0
        self._last_energy_sync_check_ts: float = 0.0
        self._energy_sync_task: asyncio.Task | None = None
        self._last_energy_sync_error: str | None = None
        self._exported_energy_entities: set[str] = set()
        self._last_explicit_activity_edge: dict[str, float] = {}
        self._transition_generation: dict[str, int] = {}
        self._sanity_profile_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._profile_assessment_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self._energy_readiness_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}

        self.models: dict[str, DeviceModel] = {
            eid: DeviceModel(eid) for eid in self.tracked_entities
        }
        # Stored records for entities temporarily removed from the tracked list
        # are preserved verbatim. Only an explicit forget operation should destroy
        # learned fingerprints or cumulative energy.
        self._untracked_stored_models: dict[str, Any] = {}
        self._untracked_stored_profiles: dict[str, Any] = {}
        # Classification metadata is deliberately separate from the power model.
        # Future discovery/reclassification can change a profile without resetting
        # learned evidence or requiring an existing user to run setup again.
        self.device_profiles: dict[str, DeviceProfile] = {
            eid: default_device_profile(hass, eid) for eid in self.tracked_entities
        }
        self.migration_cleanup: dict[str, Any] = {}
        self.isolation_sessions: dict[str, IsolationSession] = {
            eid: IsolationSession(eid) for eid in self.tracked_entities
        }
        # Legacy 0.3 inverter records are read only during migration; there is no
        # separate inverter runtime model in 0.4.
        self._legacy_inverter_record: InverterModel | None = None
        self.accepted_count = 0
        self.rejected_count = 0
        self.rejection_reasons: Counter[str] = Counter()
        # Session counters are intentionally not persisted. They make it easy to
        # judge a new algorithm release without erasing the learned model/history.
        self.session_accepted_count = 0
        self.session_rejected_count = 0
        self.session_rejection_reasons: Counter[str] = Counter()
        self.last_observation: dict[str, Any] | None = None
        self.last_source_w: float | None = None
        self.last_residual_w: float | None = None
        self.last_metered_w: float | None = None
        self.last_unexplained_w: float | None = None
        self.last_fingerprinted_w: float = 0.0
        self.missing_meters: list[str] = []
        self.stale_meters: list[str] = []
        self.quarantined_meters: list[str] = []
        self.recovering_meters: list[str] = []
        self._loaded_schema_version = MODEL_SCHEMA_VERSION

        self.store = Store[dict[str, Any]](
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry.entry_id}",
        )

    async def async_start(self) -> None:
        """Load storage and attach listeners."""
        await self._async_load()
        if self._loaded_schema_version < 8:
            # 0.5.7 generated an experimental local profile format. Re-render any
            # existing graduates once through the stricter 0.6 writer so the files
            # conform to the current Powercalc model schema without losing the
            # Power Fingerprint evidence or cumulative energy behind them.
            for entity_id, profile in self.device_profiles.items():
                if not profile.powercalc_exported or not profile.powercalc_model_id:
                    continue
                profile_type = profile.powercalc_profile_type or "fixed"
                assessment = {
                    "profile_type": profile_type,
                    "power_w": profile.powercalc_power_w,
                    "calibration_points": profile.powercalc_calibration_points or [],
                    "session_count": len(self._powercalc_validated_sessions(entity_id)),
                }
                usable = (
                    profile_type == "linear" and bool(assessment["calibration_points"])
                ) or (profile_type == "fixed" and assessment["power_w"] is not None)
                if not usable:
                    continue
                with suppress(Exception):
                    export_map = {
                        eid: str(p.powercalc_model_id)
                        for eid, p in self.device_profiles.items()
                        if p.powercalc_exported and p.powercalc_model_id
                    }
                    result = await self.hass.async_add_executor_job(
                        self._write_powercalc_export_sync,
                        entity_id,
                        assessment,
                        source_display_name(self.hass, entity_id),
                        self.hass.config.config_dir,
                        export_map,
                    )
                    profile.powercalc_power_sensor_id = str(result["power_sensor_id"])
                    profile.powercalc_energy_sensor_id = str(result["energy_sensor_id"])
        if any(
            profile.powercalc_exported and profile.powercalc_power_sensor_id
            for profile in self.device_profiles.values()
        ):
            # Re-render 0.5.7 bindings using 0.6 deterministic sensor names so
            # handoff reconciliation can find and seed them reliably.
            exports = [
                (eid, str(profile.powercalc_model_id))
                for eid, profile in self.device_profiles.items()
                if profile.powercalc_exported and profile.powercalc_model_id
            ]
            await self.hass.async_add_executor_job(
                self._rewrite_powercalc_bindings_sync, self.hass.config.config_dir, exports
            )
        await self._async_refresh_meter_entities()
        self._initialize_isolation_sessions()

        # Entity state events nominate device transitions and record direct-meter
        # changes.  Residual sampling itself deliberately runs on a fixed clock:
        # relying on source state-change events produced too few samples on real
        # installations when the whole-house value happened to remain unchanged.
        listen = list(dict.fromkeys([*self.meter_entities, *self.tracked_entities]))
        self._unsubs.append(async_track_state_change_event(self.hass, listen, self._async_state_changed))
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_sample_tick,
                timedelta(seconds=self.sample_interval_seconds),
            )
        )

        # Energy Dashboard preferences are read through its initialized manager.
        # The EnergyManager update listener API intentionally has no unsubscribe
        # callback, so we do not attach a long-lived listener here; changing the
        # Energy Dashboard meter list requires reloading this integration.
        if self.use_energy_meters:
            self._energy_manager = await async_get_manager(self.hass)

        self._append_source_sample()
        self.async_set_updated_data(self.snapshot())

    async def async_post_setup(self) -> None:
        """Run after sensor entities have been registered."""
        self._unsubs.append(async_setup_area_inheritance(self.hass, self.entry.entry_id))
        if self.use_energy_meters or self.auto_export_energy:
            self._energy_manager = await async_get_manager(self.hass)
        # Entity-registry ids for our power/energy sensors are available only after
        # platform forwarding, so Energy Dashboard export belongs here.
        await self._async_sync_energy_dashboard(force=True)
        # Powercalc is an optional after-dependency in 0.6. Reconcile any profile
        # exported before the restart and seed its Powercalc energy counter before
        # enabling the lightweight runtime mirror/audit state.
        await self._async_reconcile_powercalc_handoffs()

    async def async_stop(self) -> None:
        """Stop listeners/tasks and save."""
        for unsub in self._unsubs:
            with suppress(Exception):
                unsub()
        self._unsubs.clear()
        for task in list(self._pending_tasks):
            task.cancel()
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        await self.store.async_save(self._serialize())

    async def _async_load(self) -> None:
        """Restore evidence/energy and migrate 0.3 state without discarding it."""
        data = await self.store.async_load() or {}
        schema_version = int(data.get("schema_version", 1) or 1)
        self._loaded_schema_version = schema_version
        migration_performed = schema_version < MODEL_SCHEMA_VERSION
        stored_models = dict(data.get("models", {}) or {})
        stored_profiles = dict(data.get("device_profiles", {}) or {})
        self._untracked_stored_models = dict(stored_models)
        self._untracked_stored_profiles = dict(stored_profiles)
        self.migration_cleanup = dict(data.get("migration_cleanup") or {})
        block_state = dict(data.get("outdoor_block") or {})
        if not self._outdoor_block_explicitly_configured:
            # Earlier private builds supplied household-specific defaults. The
            # saved group is authoritative when adopting the public build; an
            # explicit option (including an empty list) always takes precedence.
            saved_members = block_state.get("entities")
            if isinstance(saved_members, list):
                self.outdoor_block_entities = list(dict.fromkeys(
                    eid for eid in saved_members
                    if isinstance(eid, str) and eid in self.tracked_entities
                ))
                self._outdoor_block_entity_set = set(self.outdoor_block_entities)
        stored_block_energy = float(block_state.get("energy_kwh", 0.0) or 0.0)
        self._outdoor_block_legacy_energy_kwh = float(
            block_state.get("legacy_energy_kwh", 0.0) or 0.0
        )
        # 0.7.0's all/almost-all fallback could assign the entire otherwise
        # unexplained household residual to the outdoor-light bucket. Its energy
        # total and equations are therefore not safe to carry forward. Preserve
        # the old total for diagnostics, but restart corrected outdoor accounting.
        if schema_version < 11:
            self._outdoor_block_legacy_energy_kwh = max(
                self._outdoor_block_legacy_energy_kwh, stored_block_energy
            )
            self._outdoor_block_energy_kwh = 0.0
            self._outdoor_block_sample_count = 0
            self._outdoor_ensemble_equations = []
            self._outdoor_ensemble_member_power_w = {}
            self.migration_cleanup["schema_11_outdoor_legacy_energy_kwh"] = round(
                self._outdoor_block_legacy_energy_kwh, 9
            )
            self.migration_cleanup["schema_11_outdoor_equations_reset"] = True
        else:
            self._outdoor_block_energy_kwh = stored_block_energy
            self._outdoor_block_sample_count = int(block_state.get("sample_count", 0) or 0)
            stored_equations = block_state.get("ensemble_equations") or []
            if isinstance(stored_equations, list):
                self._outdoor_ensemble_equations = [
                    dict(item) for item in stored_equations[-OUTDOOR_ENSEMBLE_MAX_EQUATIONS:]
                    if isinstance(item, dict)
                ]
            stored_member_power = block_state.get("ensemble_member_power_w") or {}
            if isinstance(stored_member_power, dict):
                self._outdoor_ensemble_member_power_w = {
                    str(eid): max(0.0, float(value or 0.0))
                    for eid, value in stored_member_power.items()
                    if str(eid) in self._outdoor_block_entity_set
                }
        # Refit from persisted equations rather than trusting stale diagnostics.
        self._refit_outdoor_ensemble(inject_evidence=False)
        if schema_version < 5:
            self.migration_cleanup["schema_5_rich_fingerprint_initialized"] = True
        removed_orphan_observations: list[dict[str, Any]] = []
        removed_malformed_rich_sessions: list[tuple[str, str, Any]] = []
        reclassified_alias_fingerprints: list[tuple[str, str]] = []
        for eid in self.tracked_entities:
            key = self.source_registry_key(eid)
            lookup_keys = list(dict.fromkeys((key, eid, slugify(eid))))
            model_payload = next((stored_models.get(candidate) for candidate in lookup_keys if stored_models.get(candidate) is not None), None)
            model = DeviceModel.from_dict(eid, model_payload)
            if schema_version < 11 and eid in self._outdoor_block_entity_set:
                before_soft = len(model.soft_evidence_observations)
                model.soft_evidence_observations = [
                    row for row in model.soft_evidence_observations
                    if str(row.get("kind") or "") not in {
                        "outdoor_plateau_single_transition",
                        "outdoor_ensemble_fit",
                    }
                ]
                removed_soft = before_soft - len(model.soft_evidence_observations)
                if removed_soft:
                    key_name = "schema_11_removed_outdoor_soft_evidence"
                    self.migration_cleanup[key_name] = int(
                        self.migration_cleanup.get(key_name, 0) or 0
                    ) + removed_soft
            for candidate in lookup_keys:
                self._untracked_stored_models.pop(candidate, None)
            # 0.3 ordinary-device isolated energy is already trustworthy live
            # isolation evidence. Preserve it exactly, but mark its provenance.
            if schema_version < 3 and model.isolated_energy_kwh > 0 and not model.migration_source:
                model.migration_energy_kwh = model.isolated_energy_kwh
                model.migration_source = "0.3_live_session_isolation"
            # 0.4.0-0.4.3 could persist a latent HVAC start edge before the cycle
            # had validated. Remove only unmatched starts; completed UP/DOWN pairs
            # are retained as legitimate training evidence. This never touches
            # accumulated energy or historical validated cycle data.
            if schema_version < 4:
                removed_orphan_observations.extend(prune_unpaired_latent_start_observations(model))
            # 0.5.0/0.5.1 could pair a stale accepted start edge with a later
            # accepted stop after an intervening start had been rejected. Those
            # hybrid rich fingerprints are identifiable because a real accepted
            # start always carries started_at, while the stale-pair artifact does
            # not. Remove only that rich-session evidence and its validation
            # credit; preserve edge observations and accumulated energy.
            if schema_version < 6:
                for channel_name, fingerprint in prune_malformed_schema5_session_fingerprints(model):
                    removed_malformed_rich_sessions.append((eid, channel_name, fingerprint))
            # 0.5.2 could label one physical HVAC mode as ``mixed`` when a
            # residual-start alias such as inferred_cool was later refined by
            # hvac_action to cooling. Reclassify only sessions whose event labels
            # all collapse to one true alias; genuine multi-stage sessions remain
            # mixed and retain their complete event timeline.
            if schema_version < 7 and eid.split(".", 1)[0] == "climate":
                for storage_channel in self._reclassify_equivalent_climate_alias_fingerprints(model):
                    reclassified_alias_fingerprints.append((eid, storage_channel))
            self.models[eid] = model

            profile_payload = next((stored_profiles.get(candidate) for candidate in lookup_keys if stored_profiles.get(candidate) is not None), None)
            for candidate in lookup_keys:
                self._untracked_stored_profiles.pop(candidate, None)
            self.device_profiles[eid] = (
                DeviceProfile.from_dict(eid, profile_payload)
                if profile_payload
                else default_device_profile(self.hass, eid)
            )
            if schema_version < 8 and self.device_profiles[eid].powercalc_exported:
                legacy_profile = self.device_profiles[eid]
                legacy_profile.lifecycle_state = "graduated_to_powercalc"
                legacy_profile.lifecycle_updated_at = utc_iso()
                legacy_profile.powercalc_profile_type = legacy_profile.powercalc_profile_type or "fixed"
                legacy_profile.powercalc_handoff_status = "pending_restart"
                legacy_profile.powercalc_active = False
                legacy_profile.powercalc_power_sensor_id, legacy_profile.powercalc_energy_sensor_id = self._powercalc_predicted_entity_ids(eid)

        # 0.3 stored the selected mini-split in a separate inverter record. 0.4
        # folds that evidence into the same DeviceModel used by every other load.
        # Keep loading the archived record on later 0.4 restarts as a rollback/audit
        # artifact, but perform the numerical migration only once.
        legacy_payload = data.get("legacy_inverter_migration") or data.get("inverter")
        if self.legacy_inverter_entity and legacy_payload:
            legacy = InverterModel.from_dict(self.legacy_inverter_entity, legacy_payload)
            self._legacy_inverter_record = legacy
            if schema_version < 3:
                model = self.models.setdefault(self.legacy_inverter_entity, DeviceModel(self.legacy_inverter_entity))
                if legacy.energy_kwh > 0:
                    # The old inverter accumulator and the new canonical accumulator
                    # cover the same physical device, so never add them together.
                    model.isolated_energy_kwh = max(model.isolated_energy_kwh, legacy.energy_kwh)
                    # The pre-0.4 Gree estimated-energy entity exposed this same
                    # accumulator, so seed its compatibility total as well.
                    model.energy_kwh = max(model.energy_kwh, legacy.energy_kwh)
                    model.migration_energy_kwh = max(model.migration_energy_kwh, legacy.energy_kwh)
                    model.migration_source = "0.3_inverter_change_point"
                if legacy.cycle_values_w:
                    model.legacy_cycle_values_w = list(legacy.cycle_values_w[-100:])
                    model.historical_validated_cycles = max(
                        model.historical_validated_cycles, len(legacy.cycle_values_w)
                    )
                    # This installation's pre-0.4 Gree training was cooling-only.
                    # Preserve that provenance as channel-specific historical evidence;
                    # it validates cooling, not heating/fan operation.
                    model.historical_channel_validations["cooling"] = max(
                        model.historical_channel_validations.get("cooling", 0),
                        len(legacy.cycle_values_w),
                    )

        self.accepted_count = int(data.get("accepted_count", 0) or 0)
        if removed_orphan_observations:
            self.accepted_count = max(0, self.accepted_count - len(removed_orphan_observations))
            self.migration_cleanup["schema_4_orphan_latent_start_observations_removed"] = (
                int(self.migration_cleanup.get("schema_4_orphan_latent_start_observations_removed", 0) or 0)
                + len(removed_orphan_observations)
            )
            self.migration_cleanup["schema_4_removed_entities"] = sorted(
                {str(obs.get("entity_id")) for obs in removed_orphan_observations if obs.get("entity_id")}
            )
        if removed_malformed_rich_sessions:
            self.migration_cleanup["schema_6_stale_pair_fingerprints_removed"] = (
                int(self.migration_cleanup.get("schema_6_stale_pair_fingerprints_removed", 0) or 0)
                + len(removed_malformed_rich_sessions)
            )
            self.migration_cleanup["schema_6_removed_entities"] = sorted(
                {eid for eid, _channel, _fingerprint in removed_malformed_rich_sessions}
            )
        elif schema_version < 6:
            self.migration_cleanup["schema_6_stale_pair_fingerprints_removed"] = 0
        if reclassified_alias_fingerprints:
            self.migration_cleanup["schema_7_equivalent_climate_alias_fingerprints_reclassified"] = (
                int(
                    self.migration_cleanup.get(
                        "schema_7_equivalent_climate_alias_fingerprints_reclassified", 0
                    )
                    or 0
                )
                + len(reclassified_alias_fingerprints)
            )
            self.migration_cleanup["schema_7_reclassified_entities"] = sorted(
                {eid for eid, _storage_channel in reclassified_alias_fingerprints}
            )
        elif schema_version < 7:
            self.migration_cleanup["schema_7_equivalent_climate_alias_fingerprints_reclassified"] = 0
        self.rejected_count = int(data.get("rejected_count", 0) or 0)
        self.selected_calibration_last_result = (
            str(data["last_selected_calibration_result"])
            if data.get("last_selected_calibration_result") else None
        )
        self.rejection_reasons = Counter({
            str(k): int(v) for k, v in (data.get("rejection_reasons") or {}).items()
        })
        if not self.rejection_reasons and self.rejected_count:
            for model in self.models.values():
                for channel in model.channels.values():
                    for obs in channel.observations:
                        if not obs.get("accepted") and obs.get("reason"):
                            self.rejection_reasons[str(obs["reason"])] += 1
        last_batch = data.get("last_calibration_batch") or {}
        if isinstance(last_batch, dict) and last_batch:
            self.calibration_batch_status = str(last_batch.get("status") or "idle")
            self.calibration_batch_started_at = (
                str(last_batch["started_at"]) if last_batch.get("started_at") else None
            )
            self.calibration_batch_total = int(last_batch.get("total", 0) or 0)
            self.calibration_batch_completed = int(last_batch.get("completed", 0) or 0)
            self.calibration_batch_validated = int(last_batch.get("validated", 0) or 0)
            self.calibration_batch_failed = int(last_batch.get("failed", 0) or 0)
            self.calibration_batch_skipped = int(last_batch.get("skipped", 0) or 0)
            self.calibration_batch_retried = int(last_batch.get("retried", 0) or 0)
            self.calibration_batch_last_result = (
                str(last_batch["last_result"]) if last_batch.get("last_result") else None
            )
            stored_candidates = last_batch.get("candidates") or []
            if isinstance(stored_candidates, list):
                self.calibration_batch_candidates = [str(item) for item in stored_candidates]
            stored_results = last_batch.get("results") or []
            if isinstance(stored_results, list):
                self.calibration_batch_results.extend(
                    item for item in stored_results[-50:] if isinstance(item, dict)
                )

        last = data.get("last_observation")
        self.last_observation = last if isinstance(last, dict) else None
        if self.last_observation and any(
            self.last_observation.get("ts") == obs.get("ts")
            and self.last_observation.get("entity_id") == obs.get("entity_id")
            and self.last_observation.get("channel") == obs.get("channel")
            for obs in removed_orphan_observations
        ):
            self.last_observation = None

        # Persist schema cleanup/profile migration immediately so a restart during
        # early startup cannot reapply the same one-time cleanup.
        if migration_performed:
            await self.store.async_save(self._serialize())

    def _serialize(self) -> dict[str, Any]:
        """Return JSON-serializable persistent state."""
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "models": {
                **self._untracked_stored_models,
                **{self.source_registry_key(eid): model.as_dict() for eid, model in self.models.items()},
            },
            "device_profiles": {
                **self._untracked_stored_profiles,
                **{self.source_registry_key(eid): profile.as_dict() for eid, profile in self.device_profiles.items()},
            },
            "migration_cleanup": dict(self.migration_cleanup),
            "outdoor_block": {
                "entities": list(self.outdoor_block_entities),
                "energy_kwh": self._outdoor_block_energy_kwh,
                "legacy_energy_kwh": self._outdoor_block_legacy_energy_kwh,
                "sample_count": self._outdoor_block_sample_count,
                "ensemble_equations": list(self._outdoor_ensemble_equations)[-OUTDOOR_ENSEMBLE_MAX_EQUATIONS:],
                "ensemble_member_power_w": dict(self._outdoor_ensemble_member_power_w),
                "ensemble_rank": self._outdoor_ensemble_rank,
                "ensemble_rmse_w": self._outdoor_ensemble_rmse_w,
                "ensemble_mature": self._outdoor_ensemble_mature,
            },
            # Keep a read-only copy of the migrated 0.3 inverter record for
            # diagnostics/auditability. It is never used for runtime inference.
            "legacy_inverter_migration": (
                self._legacy_inverter_record.as_dict() if self._legacy_inverter_record else None
            ),
            # Preserve the original 0.3 key as a rollback safety net. It is never
            # consulted by 0.4 after migration, but a temporary downgrade can still
            # recover the old Gree cycle history/energy.
            "inverter": (
                self._legacy_inverter_record.as_dict() if self._legacy_inverter_record else None
            ),
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "rejection_reasons": dict(self.rejection_reasons),
            "last_selected_calibration_result": self.selected_calibration_last_result,
            "last_observation": self.last_observation,
            "last_calibration_batch": {
                "status": (
                    "interrupted" if self.calibration_batch_running
                    else self.calibration_batch_status
                ),
                "started_at": self.calibration_batch_started_at,
                "total": self.calibration_batch_total,
                "completed": self.calibration_batch_completed,
                "validated": self.calibration_batch_validated,
                "failed": self.calibration_batch_failed,
                "skipped": self.calibration_batch_skipped,
                "retried": self.calibration_batch_retried,
                "last_result": self.calibration_batch_last_result,
                "candidates": list(self.calibration_batch_candidates),
                "results": list(self.calibration_batch_results),
            },
        }

    @callback
    def _schedule_save(self) -> None:
        """Debounce writes to .storage."""
        self.store.async_delay_save(self._serialize, STORAGE_SAVE_DELAY)

    async def _async_refresh_meter_entities(self) -> None:
        """Build direct-meter list from top-level Energy Dashboard devices."""
        meters: list[str] = []
        if self.use_energy_meters:
            manager = await async_get_manager(self.hass)
            if manager.data:
                for device in manager.data.get("device_consumption", []):
                    # If this device is explicitly included inside another Energy
                    # Dashboard statistic, subtracting both would double count it.
                    if device.get("included_in_stat"):
                        continue
                    rate = device.get("stat_rate")
                    if isinstance(rate, str) and rate.startswith("sensor."):
                        registry_entry = er.async_get(self.hass).async_get(rate)
                        # Never feed our own inferred power back into residual
                        # subtraction after the user adds its energy sensor to
                        # the Energy Dashboard.
                        if registry_entry and registry_entry.platform == DOMAIN:
                            continue
                        meters.append(rate)
        meters.extend(self.additional_meters)
        self.meter_entities = [
            eid
            for eid in dict.fromkeys(meters)
            if eid != self.source_entity and eid not in self.excluded_meters
        ]

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle all relevant state changes."""
        eid = event.data["entity_id"]
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        now = time.monotonic()

        if eid in self.meter_entities:
            self._handle_meter_state_event(eid, old_state, new_state, now)
            return

        if eid in self._outdoor_block_entity_set and old_state and new_state and old_state.state != new_state.state:
            self._outdoor_block_last_transition_ts = now
            self._outdoor_block_samples.clear()

        if eid in self.models and self.learning_enabled:
            self._handle_tracked_transition(eid, old_state, new_state, now)

    @callback
    def _async_sample_tick(self, _now) -> None:
        """Take one time-based residual sample from in-memory HA states."""
        self._append_source_sample(time.monotonic())

    @callback
    def _append_source_sample(self, now: float | None = None) -> None:
        """Sample residual while gracefully degrading around flaky meters.

        Fresh meters are subtracted normally. A brief unavailable period holds the
        last trusted value. If the outage outlives ``meter_grace_seconds``, that
        meter is automatically quarantined: it is temporarily *not* subtracted, so
        its load simply becomes part of the unknown residual instead of stopping
        the entire learner. A quarantined meter must remain continuously available
        for ``meter_rejoin_seconds`` before it rejoins the subtraction set.
        """
        now = now or time.monotonic()
        source_state = self.hass.states.get(self.source_entity)
        source_w = self._state_obj_power_w(source_state)
        self.last_source_w = source_w
        if source_w is None:
            self.last_metered_w = None
            self.last_residual_w = None
            self.last_unexplained_w = None
            self._publish_if_due(now, force=True)
            return
        source_reported_at = source_state.last_reported.timestamp() if source_state and source_state.last_reported else None
        if source_reported_at is not None and (
            not self._source_report_times or source_reported_at > self._source_report_times[-1]
        ):
            self._source_report_times.append(source_reported_at)

        metered = 0.0
        unavailable: list[str] = []
        held: list[str] = []
        recovering: list[str] = []

        for eid in self.meter_entities:
            value = self._state_power_w(eid)
            cached = self._meter_cache.get(eid)

            if value is not None:
                self._meter_peak_w[eid] = max(self._meter_peak_w.get(eid, 0.0), max(0.0, value))
                # Do not immediately rejoin a meter that was quarantined. This
                # hysteresis prevents an availability-flapping sensor from adding
                # and removing its entire load from the residual every few seconds.
                if eid in self._quarantined_meters:
                    recovered_at = self._meter_recovery_since.setdefault(eid, now)
                    self._meter_cache[eid] = (value, now)
                    if now - recovered_at < self.meter_rejoin_seconds:
                        recovering.append(eid)
                        continue

                    self._quarantined_meters.discard(eid)
                    self._meter_recovery_since.pop(eid, None)
                    self._meter_unavailable_since.pop(eid, None)
                    # Rejoining changes residual accounting by the meter's current
                    # value. Mark that instant so candidate observations spanning
                    # it are rejected, but learning resumes immediately afterwards.
                    if abs(value) >= self.min_step_w:
                        self._meter_availability_intervals.append((now, now, eid, "meter_rejoined"))
                        self._sample_guard_until = max(self._sample_guard_until, now + self.sample_interval_seconds * 2)
                        # Rejoining lowers residual by the meter value; move every
                        # active counterfactual baseline by the same accounting step.
                        self._shift_active_session_baselines(-max(0.0, value), reason="meter_rejoined")
                    metered += max(0.0, value)
                    continue

                self._meter_recovery_since.pop(eid, None)
                self._meter_unavailable_since.pop(eid, None)
                self._meter_cache[eid] = (value, now)
                metered += max(0.0, value)
                continue

            # Meter is unavailable/unknown now.
            unavailable.append(eid)
            self._meter_recovery_since.pop(eid, None)
            outage_start = self._meter_unavailable_since.setdefault(eid, now)

            if eid in self._quarantined_meters:
                continue

            outage_age = now - outage_start
            if cached is not None and self.meter_grace_seconds > 0 and outage_age <= self.meter_grace_seconds:
                # Short hold: keep the residual continuous. These samples remain
                # learnable. If the meter later returns at a materially different
                # value, the entire hold interval is retrospectively marked unsafe
                # and any overlapping candidate observation is rejected.
                metered += max(0.0, cached[0])
                held.append(eid)
                continue

            # Long/unbounded outage: quarantine rather than globally blocking the
            # learner. The meter's real load is now simply part of residual power.
            self._quarantined_meters.add(eid)
            self._meter_availability_intervals.append((now, now, eid, "meter_quarantined"))
            self._sample_guard_until = max(self._sample_guard_until, now + self.sample_interval_seconds * 2)
            held_w = max(0.0, cached[0]) if cached is not None else 0.0
            if held_w >= self.min_step_w:
                # Quarantine stops subtracting this meter, so residual rises by
                # approximately its held value. Shift active baselines likewise.
                self._shift_active_session_baselines(held_w, reason="meter_quarantined")

        self.missing_meters = unavailable
        self.stale_meters = held
        self._held_meters_now = held
        self._recovering_meters_now = recovering
        self.quarantined_meters = sorted(self._quarantined_meters)
        self.recovering_meters = sorted(recovering)

        residual = max(0.0, source_w - metered)
        self._update_isolation_sessions(now, residual)
        ordinary_power = self._ordinary_isolated_power()
        corrected = max(0.0, residual - ordinary_power)
        self._sample_buffer.append(
            Sample(
                now,
                residual,
                corrected,
                valid_for_learning=now >= self._sample_guard_until,
                source_reported_at=source_reported_at,
            )
        )
        self._last_source_sample_ts = now
        self.last_metered_w = metered
        self.last_residual_w = residual

        # Climate integrations can expose a reliable hvac_action edge, but some
        # devices only expose HVAC mode. The generic latent activity adapter may
        # nominate a climate start/stop from a residual change-point; once a
        # session exists, power is still computed by this same isolation engine.
        if self.learning_enabled:
            self._update_latent_climate_activity(now)
        runtime_power = {eid: self.device_isolated_power_w(eid) for eid in self.models}
        self._integrate_energy(now, runtime_power)
        ordinary_identified = sum(max(0.0, value) for value in runtime_power.values() if value is not None)
        self._update_outdoor_block(now, residual, runtime_power)
        self.last_fingerprinted_w = ordinary_identified + self._outdoor_block_power_w
        self.last_unexplained_w = max(0.0, residual - self.last_fingerprinted_w)
        if self.auto_export_energy and now - self._last_energy_sync_check_ts >= 60.0:
            self._last_energy_sync_check_ts = now
            self._schedule_energy_dashboard_sync()
        self._publish_if_due(now)

    @staticmethod
    def _matrix_rank(rows: list[list[float]], tolerance: float = 1e-8) -> int:
        """Return numerical row rank for a small dense matrix."""
        if not rows:
            return 0
        matrix = [list(map(float, row)) for row in rows]
        n_rows = len(matrix)
        n_cols = len(matrix[0]) if matrix else 0
        rank = 0
        col = 0
        while rank < n_rows and col < n_cols:
            pivot = max(range(rank, n_rows), key=lambda r: abs(matrix[r][col]))
            if abs(matrix[pivot][col]) <= tolerance:
                col += 1
                continue
            matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
            pivot_value = matrix[rank][col]
            for c in range(col, n_cols):
                matrix[rank][c] /= pivot_value
            for r in range(n_rows):
                if r == rank:
                    continue
                factor = matrix[r][col]
                if abs(factor) <= tolerance:
                    continue
                for c in range(col, n_cols):
                    matrix[r][c] -= factor * matrix[rank][c]
            rank += 1
            col += 1
        return rank

    @staticmethod
    def _solve_dense(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
        """Solve a small dense linear system using pivoted Gauss-Jordan elimination."""
        n = len(vector)
        if n == 0 or len(matrix) != n:
            return None
        augmented = [list(map(float, matrix[i])) + [float(vector[i])] for i in range(n)]
        for col in range(n):
            pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
            if abs(augmented[pivot][col]) < 1e-10:
                return None
            augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
            pv = augmented[col][col]
            for c in range(col, n + 1):
                augmented[col][c] /= pv
            for r in range(n):
                if r == col:
                    continue
                factor = augmented[r][col]
                if abs(factor) < 1e-12:
                    continue
                for c in range(col, n + 1):
                    augmented[r][c] -= factor * augmented[col][c]
        return [augmented[i][n] for i in range(n)]

    def _add_outdoor_soft_evidence(
        self, entity_id: str, value_w: float, weight: float, *, kind: str, source_ts: str
    ) -> None:
        """Feed conservative ensemble-derived evidence into the fixed-load learner."""
        if not self._supports_soft_fixed_evidence(entity_id) or value_w <= 0 or weight <= 0:
            return
        if value_w > self._maximum_plausible_w(entity_id):
            return
        model = self.models.get(entity_id)
        if not model:
            return
        # Do not duplicate evidence if a refit is repeated without a new equation.
        signature = f"{kind}:{source_ts}"
        if any(row.get("ensemble_signature") == signature for row in model.soft_evidence_observations[-20:]):
            return
        model.soft_evidence_observations.append({
            "ts": source_ts,
            "value_w": round(float(value_w), 3),
            "weight": round(float(weight), 3),
            "base_weight": round(float(weight), 3),
            "pair_bonus": 0.0,
            "snr": None,
            "noise_w": None,
            "kind": kind,
            "source_reason": "outdoor_ensemble",
            "direction": "paired_plateau",
            "ensemble_signature": signature,
        })
        if len(model.soft_evidence_observations) > 80:
            del model.soft_evidence_observations[:-80]
        self._energy_readiness_cache.pop(entity_id, None)
        self._profile_assessment_cache.pop(entity_id, None)
        self._refresh_profile_lifecycle(entity_id)

    def _refit_outdoor_ensemble(self, *, inject_evidence: bool = True) -> None:
        """Fit per-member watts from persistent differences between stable plateaus."""
        members = self.outdoor_block_entities
        n = len(members)
        equations = [eq for eq in self._outdoor_ensemble_equations if len(eq.get("dx") or []) == n]
        rows = [[float(v) for v in eq["dx"]] for eq in equations]
        self._outdoor_ensemble_unique_signature_count = len({
            tuple(int(v) for v in (eq.get("dx") or [])) for eq in equations
        })
        evidence = {eid: 0 for eid in members}
        for row in rows:
            for i, value in enumerate(row):
                if abs(value) > 0.5:
                    evidence[members[i]] += 1
        self._outdoor_ensemble_member_evidence = evidence
        if not rows:
            self._outdoor_ensemble_rank = 0
            self._outdoor_ensemble_member_power_w = {}
            self._outdoor_ensemble_rmse_w = None
            self._outdoor_ensemble_mature = False
            self._outdoor_ensemble_confidence = 0.0
            return

        def solve(indices: list[int]) -> list[float] | None:
            a = [[0.0 for _ in range(n)] for _ in range(n)]
            b = [0.0 for _ in range(n)]
            for idx in indices:
                eq = equations[idx]
                row = rows[idx]
                noise = max(1.0, float(eq.get("noise_w", 1.0) or 1.0))
                weight = 1.0 / min(25.0, noise) ** 2
                y = float(eq.get("dy_w", 0.0) or 0.0)
                for i in range(n):
                    b[i] += weight * row[i] * y
                    for j in range(n):
                        a[i][j] += weight * row[i] * row[j]
            # Scale regularization to the matrix itself; a fixed penalty would
            # dominate whenever noise-weighted equations have small coefficients.
            diagonal_mean = sum(a[i][i] for i in range(n)) / max(1, n)
            ridge = max(1e-8, OUTDOOR_ENSEMBLE_RIDGE_FRACTION * diagonal_mean)
            for i in range(n):
                a[i][i] += ridge
            return self._solve_dense(a, b)

        all_indices = list(range(len(equations)))
        solution = solve(all_indices)
        if solution is None:
            self._outdoor_ensemble_rank = self._matrix_rank(rows)
            self._outdoor_ensemble_member_power_w = {}
            self._outdoor_ensemble_rmse_w = None
            self._outdoor_ensemble_mature = False
            self._outdoor_ensemble_confidence = 0.0
            return

        # Robust second pass: a coincident unmetered appliance can change between
        # two otherwise stable lighting plateaus. Trim only gross residual outliers.
        first_errors = [
            sum(rows[idx][i] * solution[i] for i in range(n))
            - float(equations[idx].get("dy_w", 0.0) or 0.0)
            for idx in all_indices
        ]
        error_center = robust_median(first_errors) or 0.0
        error_sigma = 1.4826 * mad(first_errors, error_center) if first_errors else 0.0
        robust_limit = max(10.0, 3.0 * error_sigma)
        inlier_indices = [
            idx for idx, error in enumerate(first_errors)
            if abs(error - error_center) <= robust_limit
        ]
        if len(inlier_indices) >= max(n, 5) and len(inlier_indices) < len(all_indices):
            refined = solve(inlier_indices)
            if refined is not None:
                solution = refined
        else:
            inlier_indices = all_indices

        inlier_rows = [rows[idx] for idx in inlier_indices]
        self._outdoor_ensemble_rank = self._matrix_rank(inlier_rows)
        coeffs = [max(0.0, min(500.0, float(value))) for value in solution]
        self._outdoor_ensemble_member_power_w = {
            eid: round(coeffs[i], 3) for i, eid in enumerate(members)
        }
        errors: list[float] = []
        for idx in inlier_indices:
            row = rows[idx]
            predicted = sum(row[i] * coeffs[i] for i in range(n))
            errors.append(predicted - float(equations[idx].get("dy_w", 0.0) or 0.0))
        self._outdoor_ensemble_rmse_w = math.sqrt(sum(err * err for err in errors) / len(errors)) if errors else None
        min_equations = max(10, n + 2)
        all_members_seen = all(evidence.get(eid, 0) >= OUTDOOR_ENSEMBLE_MIN_MEMBER_EVIDENCE for eid in members)
        inlier_noises = [max(0.5, float(equations[idx].get("noise_w", 0.5) or 0.5)) for idx in inlier_indices]
        typical_equation_noise = robust_median(inlier_noises) or 0.5
        rmse_limit = max(6.0, self.min_step_w * 1.5, 2.5 * typical_equation_noise)
        self._outdoor_ensemble_mature = bool(
            len(inlier_indices) >= min_equations
            and self._outdoor_ensemble_rank >= n
            and all_members_seen
            and self._outdoor_ensemble_rmse_w is not None
            and self._outdoor_ensemble_rmse_w <= rmse_limit
        )
        rank_fraction = min(1.0, self._outdoor_ensemble_rank / max(1, n))
        count_fraction = min(1.0, len(inlier_indices) / max(1, min_equations))
        error_fraction = 0.0 if self._outdoor_ensemble_rmse_w is None else max(
            0.0, min(1.0, rmse_limit / max(rmse_limit, self._outdoor_ensemble_rmse_w))
        )
        evidence_fraction = sum(
            min(1.0, evidence.get(eid, 0) / OUTDOOR_ENSEMBLE_MIN_MEMBER_EVIDENCE)
            for eid in members
        ) / max(1, n)
        self._outdoor_ensemble_confidence = round(
            100.0 * rank_fraction * count_fraction * error_fraction * evidence_fraction, 1
        )

        if inject_evidence and equations:
            newest = equations[-1]
            changed = [i for i, value in enumerate(newest["dx"]) if abs(float(value)) > 0.5]
            if self._outdoor_ensemble_mature and len(changed) > 1:
                ts = str(newest.get("ts") or utc_iso())
                for i in changed:
                    value = coeffs[i]
                    if value >= max(1.0, self.min_step_w * 0.5):
                        self._add_outdoor_soft_evidence(
                            members[i], value, OUTDOOR_ENSEMBLE_MULTI_EDGE_WEIGHT,
                            kind="outdoor_ensemble_fit", source_ts=ts,
                        )

    def _outdoor_prior_power_w(self, entity_id: str) -> float:
        """Return a conservative prior used only to sanity-check group equations."""
        manual = self._manual_fixed_value_w(entity_id)
        if manual is not None and self._manual_fixed_mode(entity_id) != MANUAL_FIXED_MODE_AUTO:
            return max(0.1, manual)
        suggestion = self.fixed_power_suggestion(entity_id)
        value = suggestion.get("power_w")
        if value is None:
            return 10.0
        return max(0.1, float(value))

    def _validate_outdoor_equation(
        self, dx: list[int], dy_w: float, noise_w: float
    ) -> tuple[bool, str]:
        """Reject outdoor equations that are obviously contaminated by other loads."""
        changed = [i for i, value in enumerate(dx) if value]
        if not changed:
            return False, "no_member_change"
        members = self.outdoor_block_entities
        noise = max(0.5, float(noise_w or 0.0))
        priors = [self._outdoor_prior_power_w(members[i]) for i in changed]
        signs = [1 if dx[i] > 0 else -1 for i in changed]

        # When every changed member moved in the same direction, the whole-group
        # differential must move that way too. A refrigerator/compressor event
        # cannot be allowed to turn a light OFF into a +282 W "light" equation.
        if all(sign == signs[0] for sign in signs) and dy_w * signs[0] <= 0:
            return False, "wrong_direction"

        if len(changed) == 1:
            prior = priors[0]
            magnitude = abs(float(dy_w))
            # A noisy 8 W-light observation is not rescued merely because the
            # noise is large. High noise means the equation cannot identify a
            # small member and should be discarded, not granted a wider ceiling.
            if noise > max(5.0, prior * 0.75):
                return False, "noisy_single_member"
            min_useful = max(2.0, 1.25 * noise)
            max_plausible = max(
                prior * OUTDOOR_ENSEMBLE_SINGLE_MAX_PRIOR_MULTIPLIER,
                prior + OUTDOOR_ENSEMBLE_SINGLE_ABSOLUTE_MARGIN_W,
            )
            if magnitude < min_useful:
                return False, "too_small"
            if magnitude > max_plausible:
                return False, "implausible_single_member"
            return True, "accepted"

        # Multi-member equations get a wider envelope because positive and
        # negative member changes may partially cancel. The envelope is still
        # bounded by the sum of reasonable member priors rather than 750 W.
        prior_span = sum(
            max(
                prior * OUTDOOR_ENSEMBLE_SINGLE_MAX_PRIOR_MULTIPLIER,
                prior + OUTDOOR_ENSEMBLE_SINGLE_ABSOLUTE_MARGIN_W,
            )
            for prior in priors
        )
        max_plausible = max(
            prior_span + OUTDOOR_ENSEMBLE_MULTI_ABSOLUTE_MARGIN_W,
            8.0 * noise + OUTDOOR_ENSEMBLE_MULTI_ABSOLUTE_MARGIN_W,
        )
        if abs(float(dy_w)) > max_plausible:
            return False, "implausible_multi_member"
        return True, "accepted"

    def _record_outdoor_plateau(self, now: float, vector: list[int], plateau_w: float, noise_w: float) -> None:
        """Persist one stable nighttime combination and difference it from the last."""
        current = {
            "mono_ts": now,
            "ts": self._utc_for_monotonic(now),
            "vector": list(vector),
            "power_w": round(float(plateau_w), 3),
            "noise_w": round(float(noise_w), 3),
        }
        previous = self._outdoor_ensemble_last_plateau
        if not previous:
            self._outdoor_ensemble_last_plateau = current
            return
        age = now - float(previous.get("mono_ts", 0.0) or 0.0)
        prior_vector = list(previous.get("vector") or [])
        if age <= 0 or age > OUTDOOR_ENSEMBLE_MAX_PAIR_GAP_SECONDS or len(prior_vector) != len(vector):
            self._outdoor_ensemble_last_plateau = current
            return
        dx = [int(vector[i]) - int(prior_vector[i]) for i in range(len(vector))]
        if not any(dx):
            # Refresh the baseline throughout a long unchanged lighting state.
            # The next edge must compare adjacent plateaus, not the first reading
            # of an hours-long ON session against the final OFF reading.
            self._outdoor_ensemble_last_plateau = current
            return
        prior_ts = float(previous["mono_ts"])
        if any(
            prior_ts <= ts <= now and eid not in self._outdoor_block_entity_set
            for ts, eid in self._transition_log
        ) or any(prior_ts <= ts <= now for ts, _eid, _delta in self._meter_change_log):
            self._outdoor_ensemble_rejected_equations["concurrent_nonblock_transition"] += 1
            self._outdoor_ensemble_last_plateau = current
            self._schedule_save()
            return
        if any(start <= now and end >= prior_ts for start, end, _eid, _kind in self._meter_availability_intervals):
            self._outdoor_ensemble_rejected_equations["meter_availability_changed"] += 1
            self._outdoor_ensemble_last_plateau = current
            self._schedule_save()
            return
        dy = float(plateau_w) - float(previous.get("power_w", 0.0) or 0.0)
        combined_noise = max(0.5, float(noise_w or 0.0) + float(previous.get("noise_w", 0.0) or 0.0))
        valid, rejection_reason = self._validate_outdoor_equation(dx, dy, combined_noise)
        if not valid:
            # Start a new adjacent pair instead of accumulating unrelated
            # background drift across several rejected state combinations.
            self._outdoor_ensemble_rejected_equations[rejection_reason] += 1
            self._outdoor_ensemble_last_plateau = current
            self._schedule_save()
            return

        self._outdoor_ensemble_last_plateau = current
        signature = ",".join(str(int(value)) for value in dx)
        same_signature_indices = [
            idx for idx, item in enumerate(self._outdoor_ensemble_equations)
            if str(item.get("signature") or ",".join(str(int(v)) for v in (item.get("dx") or []))) == signature
        ]
        # Repeated dusk/dawn behavior is useful evidence, but must never crowd all
        # other state combinations out of the bounded equation history.
        if len(same_signature_indices) >= OUTDOOR_ENSEMBLE_MAX_PER_SIGNATURE:
            del self._outdoor_ensemble_equations[same_signature_indices[0]]

        equation = {
            "ts": current["ts"],
            "dx": dx,
            "dy_w": round(dy, 3),
            "noise_w": round(combined_noise, 3),
            "from_pattern": "".join(str(int(v)) for v in prior_vector),
            "to_pattern": "".join(str(int(v)) for v in vector),
            "signature": signature,
        }
        self._outdoor_ensemble_equations.append(equation)
        if len(self._outdoor_ensemble_equations) > OUTDOOR_ENSEMBLE_MAX_EQUATIONS:
            del self._outdoor_ensemble_equations[:-OUTDOOR_ENSEMBLE_MAX_EQUATIONS]

        changed = [i for i, value in enumerate(dx) if value]
        if len(changed) == 1:
            i = changed[0]
            direction = dx[i]
            magnitude = abs(dy)
            threshold = max(3.0, 1.5 * combined_noise)
            if dy * direction > 0 and magnitude >= threshold:
                self._add_outdoor_soft_evidence(
                    self.outdoor_block_entities[i], magnitude, OUTDOOR_ENSEMBLE_SINGLE_EDGE_WEIGHT,
                    kind="outdoor_plateau_single_transition", source_ts=str(current["ts"]),
                )
        self._refit_outdoor_ensemble(inject_evidence=True)
        self._schedule_save()

    def _update_outdoor_block(self, now: float, residual_w: float, runtime_power: dict[str, float | None]) -> None:
        """Learn and attribute the nighttime outdoor-light ensemble.

        Stable combinations are converted to difference equations. Differencing
        cancels the unknown residual baseline, allowing staggered dusk/dawn states
        to identify individual member watts. Until the system is fully identifiable,
        the bounded all/almost-all prior remains as a fallback.
        """
        entities = self.outdoor_block_entities
        ordinary_identified_w = sum(max(0.0, value) for value in runtime_power.values() if value is not None)
        if len(entities) < 2:
            self._outdoor_block_power_w = 0.0
            self._outdoor_block_active = False
            self._outdoor_block_mode = "idle"
            return
        sun = self.hass.states.get("sun.sun")
        if not sun or sun.state != "below_horizon":
            self._outdoor_block_power_w = 0.0
            self._outdoor_block_active = False
            self._outdoor_block_mode = "daylight"
            self._outdoor_block_samples.clear()
            self._outdoor_ensemble_last_plateau = None
            self._outdoor_block_predicted_total_w = 0.0
            self._outdoor_block_individual_identified_w = 0.0
            self._integrate_outdoor_block_energy(now, 0.0)
            return

        member_states = [self.hass.states.get(eid) for eid in entities]
        if any(not st or st.state in (STATE_UNKNOWN, STATE_UNAVAILABLE) for st in member_states):
            self._outdoor_block_power_w = 0.0
            self._outdoor_block_active = False
            self._outdoor_block_mode = "member_unavailable"
            self._outdoor_block_samples.clear()
            self._outdoor_ensemble_last_plateau = None
            self._outdoor_block_predicted_total_w = 0.0
            self._integrate_outdoor_block_energy(now, 0.0)
            return
        vector = [1 if self._state_is_active(eid, st) else 0 for eid, st in zip(entities, member_states)]
        on_count = sum(vector)
        stable_for_ensemble = now - self._outdoor_block_last_transition_ts >= OUTDOOR_ENSEMBLE_SETTLE_SECONDS
        stable_for_fallback = now - self._outdoor_block_last_transition_ts >= DEFAULT_OUTDOOR_BLOCK_SETTLE_SECONDS
        block_individual_identified = sum(
            max(0.0, runtime_power.get(eid) or 0.0) for eid in entities
        )
        nonblock_identified = max(0.0, ordinary_identified_w - block_individual_identified)
        ensemble_target = max(0.0, residual_w - nonblock_identified)
        self._outdoor_block_individual_identified_w = block_individual_identified

        if stable_for_ensemble and now >= self._sample_guard_until:
            self._outdoor_block_samples.append((now, ensemble_target))
            plateau_window = max(
                OUTDOOR_ENSEMBLE_PLATEAU_WINDOW_SECONDS,
                (OUTDOOR_ENSEMBLE_MIN_PLATEAU_SAMPLES + 1) * self.sample_interval_seconds,
            )
            while self._outdoor_block_samples and now - self._outdoor_block_samples[0][0] > plateau_window:
                self._outdoor_block_samples.popleft()
            if (
                len(self._outdoor_block_samples) >= OUTDOOR_ENSEMBLE_MIN_PLATEAU_SAMPLES
            ):
                samples = [value for _ts, value in self._outdoor_block_samples]
                plateau = robust_median(samples)
                noise = 1.4826 * mad(samples, plateau) if plateau is not None else 0.0
                if plateau is not None and noise <= max(self.max_noise_w, 15.0):
                    self._record_outdoor_plateau(now, vector, plateau, noise)

        available_remainder = max(0.0, residual_w - ordinary_identified_w)
        if self._outdoor_ensemble_mature and on_count > 0:
            predicted_total = sum(
                self._outdoor_ensemble_member_power_w.get(eid, 0.0) * vector[i]
                for i, eid in enumerate(entities)
            )
            self._outdoor_block_predicted_total_w = max(0.0, predicted_total)
            supplemental = max(0.0, predicted_total - block_individual_identified)
            self._outdoor_block_power_w = max(0.0, min(available_remainder, supplemental))
            self._outdoor_block_active = self._outdoor_block_power_w > 0.0
            self._outdoor_block_mode = "ensemble"
            self._integrate_outdoor_block_energy(now, self._outdoor_block_power_w)
            return

        # 0.7.1 fallback: never assign the entire unexplained household residual
        # to outdoor lighting. Until the ensemble is identifiable, cap the block at
        # the sum of per-member evidence-informed priors. This preserves useful
        # nighttime accounting without turning refrigerators/HVAC into "lights."
        required = max(2, len(entities) - 1)
        if on_count < required or not stable_for_fallback:
            self._outdoor_block_power_w = 0.0
            self._outdoor_block_active = False
            self._outdoor_block_predicted_total_w = 0.0
            self._outdoor_block_mode = "learning" if stable_for_ensemble else "settling"
            self._integrate_outdoor_block_energy(now, 0.0)
            return
        prior_total = sum(
            self._outdoor_prior_power_w(eid) * vector[i]
            for i, eid in enumerate(entities)
        )
        supplemental = max(0.0, prior_total - block_individual_identified)
        self._outdoor_block_power_w = max(0.0, min(available_remainder, supplemental))
        self._outdoor_block_predicted_total_w = block_individual_identified + self._outdoor_block_power_w
        self._outdoor_block_active = self._outdoor_block_power_w > 0.0
        self._outdoor_block_mode = "prior_fallback"
        self._outdoor_block_sample_count += 1
        self._integrate_outdoor_block_energy(now, self._outdoor_block_power_w)

    def _integrate_outdoor_block_energy(self, now: float, power_w: float) -> None:
        if self._outdoor_block_last_energy_ts is not None and now > self._outdoor_block_last_energy_ts:
            dt_h = (now - self._outdoor_block_last_energy_ts) / 3600.0
            avg_w = (self._outdoor_block_last_energy_power_w + max(0.0, power_w)) / 2.0
            self._outdoor_block_energy_kwh += avg_w * dt_h / 1000.0
        self._outdoor_block_last_energy_ts = now
        self._outdoor_block_last_energy_power_w = max(0.0, power_w)

    @property
    def outdoor_block_power_w(self) -> float:
        return self._outdoor_block_power_w

    @property
    def outdoor_block_energy_kwh(self) -> float:
        return self._outdoor_block_energy_kwh

    @property
    def outdoor_ensemble_attributes(self) -> dict[str, Any]:
        return {
            "mode": self._outdoor_block_mode,
            "mature": self._outdoor_ensemble_mature,
            "confidence": self._outdoor_ensemble_confidence,
            "equation_count": len(self._outdoor_ensemble_equations),
            "unique_signature_count": self._outdoor_ensemble_unique_signature_count,
            "rejected_equations": dict(self._outdoor_ensemble_rejected_equations),
            "rank": self._outdoor_ensemble_rank,
            "required_rank": len(self.outdoor_block_entities),
            "rmse_w": round(self._outdoor_ensemble_rmse_w, 3) if self._outdoor_ensemble_rmse_w is not None else None,
            "legacy_energy_kwh": round(self._outdoor_block_legacy_energy_kwh, 6),
            "member_power_w": dict(self._outdoor_ensemble_member_power_w),
            "member_evidence": dict(self._outdoor_ensemble_member_evidence),
            "predicted_total_w": round(self._outdoor_block_predicted_total_w, 3),
            "individual_identified_w": round(self._outdoor_block_individual_identified_w, 3),
        }

    def _publish_if_due(self, now: float, *, force: bool = False) -> None:
        """Publish coordinator data without waking every entity on every 2 s sample."""
        if force or now - self._last_publish_ts >= 5.0:
            self._last_publish_ts = now
            self.async_set_updated_data(self.snapshot())

    def _state_power_w(self, entity_id: str) -> float | None:
        """Read a power sensor and normalize it to watts."""
        return self._state_obj_power_w(self.hass.states.get(entity_id))


    def _state_obj_power_w(self, state: State | None) -> float | None:
        """Normalize a supplied State object to watts."""
        if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, "", None):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfPower.WATT:
            try:
                value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
            except (ValueError, TypeError):
                return None
        return value if math.isfinite(value) else None

    @callback
    def _handle_meter_state_event(
        self, entity_id: str, old_state: State | None, new_state: State | None, now: float
    ) -> None:
        """Track numeric meter changes and availability episodes separately."""
        old_w = self._state_obj_power_w(old_state)
        new_w = self._state_obj_power_w(new_state)
        # Availability recovery needs a relatively sensitive threshold because a
        # stale held value can bias the residual for the whole outage. Ordinary
        # numeric meter updates are different: direct-meter power is already
        # subtracted from the residual, so only a substantial update needs a short
        # synchronization guard against source/meter update skew.
        availability_threshold = max(2.0, self.min_step_w)
        transition_threshold = max(15.0, self.min_step_w * 3.0)

        if old_w is not None and new_w is not None:
            delta_w = abs(new_w - old_w)
            if delta_w >= transition_threshold:
                self._record_meter_transition(now, entity_id, delta_w)
            return

        if old_w is not None and new_w is None:
            self._meter_unavailable_since.setdefault(entity_id, now)
            return

        if old_w is None and new_w is not None:
            outage_start = self._meter_unavailable_since.get(entity_id)
            cached = self._meter_cache.get(entity_id)
            # A short held outage is harmless if the meter returns at essentially
            # the same value. If it returns changed, retrospectively mark that
            # entire interval uncertain so an overlapping fingerprint is rejected.
            if (
                entity_id not in self._quarantined_meters
                and outage_start is not None
                and cached is not None
                and abs(new_w - cached[0]) >= availability_threshold
            ):
                self._meter_availability_intervals.append(
                    (outage_start, now, entity_id, "meter_changed_while_unavailable")
                )
            # If the meter is quarantined, the live value is intentionally still
            # part of residual until the recovery hysteresis expires. Numeric
            # changes remain useful as concurrency evidence.
            self._meter_recovery_since.setdefault(entity_id, now)
            return

    def _record_meter_transition(self, now: float, entity_id: str, delta_w: float) -> None:
        """Guard only samples immediately surrounding a substantial direct-meter change.

        Direct-meter changes should not veto an entire 20-25 second measurement
        window. The meter is already subtracted from residual power; the real risk
        is only a few seconds of asynchronous source/meter update skew. Mark the
        nearby samples unusable and let robust medians/MAD handle the rest.
        """
        guard_seconds = max(4.0, self.sample_interval_seconds * 2.0)
        self._meter_change_log.append((now, entity_id, delta_w))
        self._outdoor_block_samples.clear()
        # Retrospectively invalidate the immediately preceding samples, then guard
        # the same short interval after the meter update.
        for sample in reversed(self._sample_buffer):
            if now - sample.ts > guard_seconds:
                break
            sample.valid_for_learning = False
        self._sample_guard_until = max(self._sample_guard_until, now + guard_seconds)

    def _recent_quarantined_risk_w(self) -> float:
        """Approximate how much unknown power quarantined meters could contribute."""
        risk = 0.0
        for eid in self._quarantined_meters:
            cached = self._meter_cache.get(eid)
            risk += max(self._meter_peak_w.get(eid, 0.0), cached[0] if cached else 0.0)
        return risk

    def _initialize_isolation_sessions(self) -> None:
        """Initialize runtime isolation state without fabricating startup baselines."""
        now = time.monotonic()
        for eid in self.tracked_entities:
            session = self.isolation_sessions.setdefault(eid, IsolationSession(eid))
            profile = self._sanity_profile(eid)
            session.prior_source = str(profile["source"])
            session.prior_low_w = float(profile["low_w"])
            session.prior_high_w = float(profile["high_w"])
            state = self.hass.states.get(eid)
            channel = self._activity_channel(eid, state)
            active = channel is not None
            session.active = active
            session.channel = channel
            session.validation_channel = self._validation_channel(channel)
            session.pending_energy_kwh = 0.0
            session.started_ts = now if active else None
            session.started_utc = utc_iso() if active else None
            session.activity_source = "startup_state" if active else "unknown"
            session.feature_samples.clear()
            session.setting_events.clear()
            session.baseline_w = None
            session.reported_power_w = None if active else 0.0
            session.raw_power_w = None if active else 0.0
            session.status = "awaiting_clean_edge" if active else "off"
            session.quality = "unknown" if active else "off"

    def _validation_channel(self, channel: str | None) -> str | None:
        """Normalize climate sub-actions into independently validated energy channels."""
        if not channel:
            return None
        key = str(channel).lower()
        if key.startswith("inferred_"):
            key = key.removeprefix("inferred_")
        if key in {"heat", "heating", "preheat", "preheating", "defrost", "defrosting"}:
            return "heating"
        if key in {"cool", "cooling", "dry", "drying"}:
            return "cooling"
        if key in {"fan", "fan_only"}:
            return "fan"
        return key

    def _climate_minimum_edge_w(self, channel: str | None) -> float:
        """Return the minimum electrical edge worth treating as HVAC activity."""
        vchan = self._validation_channel(channel)
        if vchan == "fan":
            return max(self.min_step_w, CLIMATE_FAN_MIN_STEP_W)
        return max(self.min_step_w, CLIMATE_COMPRESSOR_MIN_STEP_W)

    def _channel_is_validated(self, entity_id: str, channel: str | None) -> bool:
        """Return whether a climate channel has completed a trusted cycle."""
        if entity_id.split(".", 1)[0] != "climate":
            return True
        key = self._validation_channel(channel)
        if not key:
            return False
        model = self.models.get(entity_id)
        if not model:
            return False
        return (model.validated_channel_sessions.get(key, 0) + model.historical_channel_validations.get(key, 0)) > 0

    def _commit_pending_channel_energy(self, entity_id: str, session: IsolationSession) -> None:
        """Commit escrowed first-cycle climate energy after channel validation."""
        if session.pending_energy_kwh <= 0:
            return
        model = self.models[entity_id]
        model.isolated_energy_kwh += session.pending_energy_kwh
        model.energy_kwh += session.pending_energy_kwh
        session.pending_energy_kwh = 0.0

    def _activity_channel(self, entity_id: str, state: State | None) -> str | None:
        """Map entity state to an active channel; None means inactive/unknown."""
        if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        domain = entity_id.split(".", 1)[0]
        if domain == "climate":
            action = str(state.attributes.get("hvac_action", "idle")).lower()
            return action if action in ACTIVE_HVAC_ACTIONS else None
        level = self._entity_level(entity_id, state)
        return "level" if level is not None and level > 0.05 else None

    def _state_is_active(self, entity_id: str, state: State | None) -> bool:
        """Return whether a tracked entity is physically active."""
        return self._activity_channel(entity_id, state) is not None

    def _sanity_profile(self, entity_id: str) -> dict[str, Any]:
        """Return a generic, evidence-narrowed plausibility envelope.

        0.6.2 deliberately removes installation/name-specific wattage assumptions.
        Domain ceilings are broad enough for ordinary real-world devices; once
        sufficient accepted evidence exists, history narrows the envelope.
        """
        domain = entity_id.split(".", 1)[0]
        profile = self.device_profiles.get(entity_id)
        domain_high = {
            "light": 500.0,
            "fan": 1200.0,
            "media_player": 2500.0,
            "climate": 12000.0,
            "switch": 5000.0,
        }.get(domain, 5000.0)
        if profile and profile.heavy_load:
            domain_high = max(domain_high, 12000.0)

        model = self.models.get(entity_id)
        revision: tuple[Any, ...] = ()
        if model:
            revision = tuple(
                (key, len(channel.observations), channel.observations[-1].get("ts") if channel.observations else None)
                for key, channel in sorted(model.channels.items())
            ) + (len(model.legacy_cycle_values_w),)
        cached = self._sanity_profile_cache.get(entity_id)
        if cached and cached[0] == revision:
            return dict(cached[1])

        historical: list[float] = []
        if model:
            for key, channel in model.channels.items():
                values = channel.accepted_values
                if domain == "climate":
                    floor = self._climate_minimum_edge_w(key)
                    values = [v for v in values if v >= floor]
                historical.extend(values)
            historical.extend(model.legacy_cycle_values_w)

        result = {
            "low_w": 0.0,
            "high_w": domain_high,
            "source": "domain_default",
            "historical_samples": len(historical),
        }
        if len(historical) >= 3:
            center = robust_median(historical) or 0.0
            spread = 1.4826 * mad(historical, center) if center else 0.0
            historical_high = max(center * 2.5, center + 4.0 * spread + 10.0, self.min_step_w)
            if domain in {"light", "switch", "fan", "media_player"}:
                result["high_w"] = min(domain_high, historical_high)
            else:
                result["high_w"] = max(domain_high * 0.35, historical_high)
            result["source"] = "historical_and_domain_default"
        self._sanity_profile_cache[entity_id] = (revision, dict(result))
        return result

    def _shift_active_session_baselines(
        self, delta_w: float, *, except_entity: str | None = None, reason: str = "tracked_transition"
    ) -> None:
        """Move active counterfactual baselines by a known residual accounting step."""
        if abs(delta_w) < 0.001:
            return
        for eid, session in self.isolation_sessions.items():
            if eid == except_entity or not session.active or session.baseline_w is None:
                continue
            session.baseline_w += delta_w
            session.baseline_adjustments += 1
            if reason == "tracked_transition":
                session.overlap_seen = True
            else:
                session.quality = "good_with_accounting_adjustment"

    def _update_isolation_sessions(self, now: float, residual_w: float) -> None:
        """Update live isolated power for sessions with a clean baseline."""
        for eid, session in self.isolation_sessions.items():
            if not session.active:
                session.raw_power_w = 0.0
                session.reported_power_w = 0.0
                continue
            if session.baseline_w is None or session.pending_direction:
                session.reported_power_w = None
                continue
            raw = residual_w - session.baseline_w
            session.raw_power_w = raw
            profile = self._sanity_profile(eid)
            session.prior_source = str(profile["source"])
            session.prior_low_w = float(profile["low_w"])
            session.prior_high_w = float(profile["high_w"])
            if raw < -max(5.0, session.prior_high_w * 0.10):
                session.reported_power_w = None
                session.sanity_status = "negative_baseline_drift"
                session.quality = "degraded"
                continue
            value = max(0.0, raw)
            if value > session.prior_high_w:
                session.reported_power_w = None
                session.sanity_status = "implausible_live_power"
                session.quality = "degraded"
                continue
            session.reported_power_w = value
            session.sanity_status = "plausible"
            if session.quality == "unknown":
                session.quality = "good"
            session.add_power_sample(value, now)

    @staticmethod
    def _utc_for_monotonic(monotonic_ts: float) -> str:
        """Map a recent monotonic timestamp to approximate wall-clock UTC."""
        age = max(0.0, time.monotonic() - monotonic_ts)
        return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()

    def _reclassify_equivalent_climate_alias_fingerprints(
        self, model: DeviceModel
    ) -> list[str]:
        """Move falsely mixed HVAC fingerprints into their canonical mode bucket."""
        mixed_sessions = list(model.session_fingerprints.get("mixed") or [])
        if not mixed_sessions:
            return []

        kept: list[Any] = []
        moved_to: list[str] = []
        for fingerprint in mixed_sessions:
            observed_channels = {
                normalized
                for event in fingerprint.setting_events
                if isinstance(event, dict) and event.get("channel")
                if (normalized := normalize_fingerprint_channel_alias(str(event.get("channel"))))
            }
            if len(observed_channels) != 1:
                kept.append(fingerprint)
                continue

            canonical_channel = next(iter(observed_channels))
            validation_channel = self._validation_channel(canonical_channel)
            storage_channel = validation_channel or canonical_channel
            fingerprint.channel = canonical_channel
            fingerprint.validation_channel = validation_channel
            destination = model.session_fingerprints.setdefault(storage_channel, [])
            destination.append(fingerprint)
            destination.sort(key=lambda item: item.completed_at)
            if len(destination) > 40:
                del destination[: len(destination) - 40]
            moved_to.append(storage_channel)

        if kept:
            model.session_fingerprints["mixed"] = kept
        else:
            model.session_fingerprints.pop("mixed", None)
        return moved_to

    def _record_completed_session_fingerprint(
        self,
        entity_id: str,
        session: IsolationSession,
        *,
        stop_edge_w: float | None,
        end_ts: float,
        completed_at: str | None = None,
        quality: str | None = None,
    ) -> None:
        """Extract and persist rich electrical features from a validated session.

        0.5 keeps these records separate from the edge-observation model. They can
        therefore become richer over time without changing canonical isolated
        power/energy or forcing existing installations to relearn devices.
        """
        model = self.models.get(entity_id)
        if model is None or session.started_ts is None:
            return
        channel = session.channel or "level"
        raw_observed_channels = {
            str(event.get("channel"))
            for event in session.setting_events
            if event.get("channel")
        }
        if entity_id.split(".", 1)[0] == "climate":
            observed_channels = {
                normalized
                for raw_channel in raw_observed_channels
                if (normalized := normalize_fingerprint_channel_alias(raw_channel))
            }
            normalized_session_channel = normalize_fingerprint_channel_alias(channel)
            if normalized_session_channel:
                channel = normalized_session_channel
        else:
            observed_channels = raw_observed_channels
        mixed_mode = len(observed_channels) > 1
        if mixed_mode:
            channel = "mixed"
            validation_channel = None
            storage_channel = "mixed"
        else:
            if observed_channels:
                channel = next(iter(observed_channels))
            validation_channel = session.validation_channel or self._validation_channel(channel)
            storage_channel = validation_channel or channel
        profile = self.device_profiles.get(entity_id)
        device_type = profile.device_type if profile else "generic_load"
        duration_s = max(0.0, end_ts - session.started_ts)
        feature_samples = [
            (t, w) for t, w in session.feature_samples
            if t <= duration_s + self.sample_interval_seconds
        ]
        annotation: dict[str, Any] = {}
        if self.calibrating_entity == entity_id:
            annotation["training_source"] = (
                "native_batch_calibration"
                if self.calibration_batch_running
                else "selected_device_calibration"
            )
            if self.calibration_campaign_level_pct is not None:
                annotation["calibration_level_pct"] = int(self.calibration_campaign_level_pct)

        fingerprint = build_session_fingerprint(
            completed_at=completed_at or utc_iso(),
            started_at=session.started_utc,
            channel=channel,
            validation_channel=validation_channel,
            duration_s=duration_s,
            activity_source=session.activity_source or session.last_edge_source or "unknown",
            device_type=device_type,
            samples=feature_samples,
            start_step_w=session.start_edge_w,
            stop_step_w=stop_edge_w,
            edge_symmetry_error_w=session.edge_symmetry_error_w,
            baseline_adjustments=session.baseline_adjustments,
            overlap_seen=session.overlap_seen,
            quality=quality or session.quality or "validated",
            setting_events=list(session.setting_events),
            annotation=annotation,
        )
        model.add_session_fingerprint(storage_channel, fingerprint)
        self._refresh_profile_lifecycle(entity_id)

    def _ordinary_isolated_power(self) -> float:
        """Sum currently trustworthy live isolated device power."""
        total = 0.0
        for eid in self.models:
            value = self.device_isolated_power_w(eid)
            if value is not None:
                total += max(0.0, value)
        return total

    def _supports_soft_fixed_evidence(self, entity_id: str) -> bool:
        """Return whether an entity can use 0.6.6 weighted fixed-load evidence."""
        domain = entity_id.split(".", 1)[0]
        return domain in {"light", "switch", "fan", "media_player"} and not self._profile_supports_variable_levels(entity_id)

    def _manual_fixed_mode(self, entity_id: str) -> str:
        """Return a normalized explicit fixed-power mode for one device."""
        profile = self.device_profiles.get(entity_id)
        mode = str(profile.fixed_power_mode if profile else MANUAL_FIXED_MODE_AUTO)
        return mode if mode in MANUAL_FIXED_MODES else MANUAL_FIXED_MODE_AUTO

    def _manual_fixed_value_w(self, entity_id: str) -> float | None:
        profile = self.device_profiles.get(entity_id)
        if not profile or profile.fixed_power_w is None:
            return None
        return max(0.0, float(profile.fixed_power_w))

    def _manual_fixed_active(self, entity_id: str) -> bool:
        return (
            self._manual_fixed_mode(entity_id) != MANUAL_FIXED_MODE_AUTO
            and self._manual_fixed_value_w(entity_id) is not None
        )

    def _manual_fixed_locked(self, entity_id: str) -> bool:
        return (
            self._manual_fixed_mode(entity_id) == MANUAL_FIXED_MODE_LOCKED
            and self._manual_fixed_value_w(entity_id) is not None
        )

    def fixed_power_options(self) -> list[tuple[str, str]]:
        """Return every tracked entity for manual fixed-power selection."""
        pairs: list[tuple[str, str]] = []
        for entity_id in self.tracked_entities:
            suggestion = self.fixed_power_suggestion(entity_id)
            suffix = (
                f"suggested {float(suggestion['power_w']):g} W"
                if suggestion.get("power_w") is not None else "no suggestion"
            )
            pairs.append((
                entity_id,
                f"{source_display_name(self.hass, entity_id)} — {suffix}",
            ))
        return sorted(pairs, key=lambda item: item[1].casefold())

    def fixed_power_suggestion(self, entity_id: str) -> dict[str, Any]:
        """Return a conservative starting fixed wattage and its evidence source."""
        model = self.models.get(entity_id)
        profile = self.device_profiles.get(entity_id)

        # For ordinary fixed/non-climate loads, the robust accepted-edge estimate
        # is usually the best practical starting value once several observations
        # exist. It is less sensitive than one rich session to unrelated steady
        # residual variation during the session.
        if model and entity_id.split(".", 1)[0] != "climate":
            level_channel = model.channels.get("level")
            if (
                level_channel
                and level_channel.sample_count >= max(3, self.min_samples)
                and level_channel.estimate_w is not None
                and float(level_channel.estimate_w) > 0
            ):
                return {
                    "power_w": round(float(level_channel.estimate_w), 1),
                    "source": "accepted_edge_model",
                    "evidence_count": int(level_channel.sample_count),
                }

        # Rich steady-state sessions are the closest thing Power Fingerprint has
        # to a direct operating-power observation, so prefer them when available.
        rich_values: list[float] = []
        if model:
            for sessions in model.session_fingerprints.values():
                rich_values.extend(
                    float(session.steady_median_w)
                    for session in sessions
                    if session.steady_median_w is not None
                    and float(session.steady_median_w) > 0
                    and not session.overlap_seen
                )
        if rich_values:
            value = robust_median(rich_values)
            if value is not None:
                return {
                    "power_w": round(float(value), 1),
                    "source": "validated_session_median",
                    "evidence_count": len(rich_values),
                }

        soft = self._soft_evidence_summary(entity_id)
        if float(soft.get("evidence_units", 0.0) or 0.0) >= 1.0 and soft.get("power_w") is not None:
            return {
                "power_w": round(float(soft["power_w"]), 1),
                "source": "weighted_transition_evidence",
                "evidence_count": int(soft.get("observation_count", 0) or 0),
            }

        accepted_values: list[float] = []
        if model:
            for channel in model.channels.values():
                accepted_values.extend(float(v) for v in channel.accepted_values if float(v) > 0)
        if accepted_values:
            value = robust_median(accepted_values)
            if value is not None:
                return {
                    "power_w": round(float(value), 1),
                    "source": "accepted_transition_median",
                    "evidence_count": len(accepted_values),
                }

        device_type = str(profile.device_type if profile else "generic_load")
        # Generic switches often control a very recognizable load. Use the source
        # name only as a last-resort type hint; learned evidence always wins.
        if device_type == "generic_load":
            label = source_display_name(self.hass, entity_id).casefold()
            hint_defaults = [
                (("light", "lamp", "chandelier", "sconce", "vanity", "porch", "closet", "counter", "island", "mantel", "mirror"), "lighting"),
                (("fan",), "fan"),
                (("refrigerator", "fridge", "freezer"), "refrigerator"),
                (("washer", "washing machine"), "washer"),
                (("dryer",), "dryer"),
                (("dishwasher",), "dishwasher"),
                (("microwave",), "microwave"),
                (("toaster",), "toaster"),
                (("water heater",), "water_heater"),
                (("tv", "television", "amplifier", "receiver", "computer", "server"), "electronics"),
            ]
            for needles, hinted_type in hint_defaults:
                if any(needle in label for needle in needles):
                    return {
                        "power_w": float(MANUAL_FIXED_SUGGESTED_W[hinted_type]),
                        "source": f"name_hint_default:{hinted_type}",
                        "evidence_count": 0,
                    }
        return {
            "power_w": float(MANUAL_FIXED_SUGGESTED_W.get(device_type, 50.0)),
            "source": f"device_type_default:{device_type}",
            "evidence_count": 0,
        }

    def fixed_power_target_attributes(self) -> dict[str, Any]:
        """Describe the selected manual fixed-power target."""
        entity_id = self.fixed_power_target
        if entity_id not in self.models:
            pairs = self.fixed_power_options()
            entity_id = pairs[0][0] if pairs else None
            self.fixed_power_target = entity_id
        if not entity_id:
            return {}
        profile = self.device_profiles.get(entity_id)
        suggestion = self.fixed_power_suggestion(entity_id)
        mode = self._manual_fixed_mode(entity_id)
        value = self._manual_fixed_value_w(entity_id)
        return {
            "entity_id": entity_id,
            "friendly_name": source_display_name(self.hass, entity_id),
            "mode": mode,
            "fixed_power_w": value,
            "suggested_power_w": suggestion.get("power_w"),
            "suggestion_source": suggestion.get("source"),
            "suggestion_evidence_count": suggestion.get("evidence_count", 0),
            "device_type": profile.device_type if profile else "generic_load",
            "monitoring_for_improvement": mode != MANUAL_FIXED_MODE_LOCKED,
        }

    async def async_set_fixed_power_value(self, entity_id: str, value_w: float) -> None:
        """Persist an explicit fixed operating wattage for a tracked entity."""
        if entity_id not in self.device_profiles:
            raise ValueError(f"Unknown tracked entity: {entity_id}")
        value = max(0.1, min(12000.0, float(value_w)))
        profile = self.device_profiles[entity_id]
        profile.fixed_power_w = round(value, 3)
        profile.fixed_power_updated_at = utc_iso()
        self._energy_readiness_cache.pop(entity_id, None)
        self._profile_assessment_cache.pop(entity_id, None)
        self._schedule_save()
        if self.auto_export_energy and self._manual_fixed_active(entity_id):
            self._schedule_energy_dashboard_sync()
        self.async_set_updated_data(self.snapshot())

    async def async_set_fixed_power_mode(self, entity_id: str, mode: str) -> None:
        """Set automatic, learning-placeholder, or locked fixed-power behavior."""
        if entity_id not in self.device_profiles:
            raise ValueError(f"Unknown tracked entity: {entity_id}")
        mode = str(mode)
        if mode not in MANUAL_FIXED_MODES:
            raise ValueError(f"Unknown fixed-power mode: {mode}")
        profile = self.device_profiles[entity_id]
        if mode != MANUAL_FIXED_MODE_AUTO and profile.fixed_power_w is None:
            suggestion = self.fixed_power_suggestion(entity_id).get("power_w")
            if suggestion is not None:
                profile.fixed_power_w = round(max(0.1, min(12000.0, float(suggestion))), 3)
        profile.fixed_power_mode = mode
        profile.fixed_power_updated_at = utc_iso()
        if mode == MANUAL_FIXED_MODE_LOCKED:
            # Invalidate pending edge tasks and discard any live isolation state.
            self._transition_generation[entity_id] = self._transition_generation.get(entity_id, 0) + 1
            session = self.isolation_sessions.get(entity_id)
            if session:
                session.active = False
                session.pending_direction = None
                session.baseline_w = None
                session.reported_power_w = 0.0
                session.raw_power_w = 0.0
                session.status = "manual_fixed_locked"
                session.quality = "manual_fixed"
                session.clear_feature_capture()
        self._energy_readiness_cache.pop(entity_id, None)
        self._profile_assessment_cache.pop(entity_id, None)
        self._schedule_save()
        if self.auto_export_energy and mode != MANUAL_FIXED_MODE_AUTO:
            self._schedule_energy_dashboard_sync()
        self.async_set_updated_data(self.snapshot())

    def _manual_fixed_runtime_w(self, entity_id: str) -> float | None:
        """Return explicit fixed watts for the current active state, if configured."""
        value = self._manual_fixed_value_w(entity_id)
        if value is None or self._manual_fixed_mode(entity_id) == MANUAL_FIXED_MODE_AUTO:
            return None
        state = self.hass.states.get(entity_id)
        if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        return value if self._state_is_active(entity_id, state) else 0.0

    def _soft_evidence_summary(self, entity_id: str) -> dict[str, Any]:
        """Summarize quality-weighted low-power observations for one entity."""
        model = self.models.get(entity_id)
        rows = list(model.soft_evidence_observations) if model else []
        weighted = [
            (float(row.get("value_w", 0.0) or 0.0), float(row.get("weight", 0.0) or 0.0), str(row.get("ts", "")))
            for row in rows
            if float(row.get("value_w", 0.0) or 0.0) > 0 and float(row.get("weight", 0.0) or 0.0) > 0
        ]
        total_weight = sum(weight for _value, weight, _ts in weighted)
        if not weighted:
            return {
                "evidence_units": 0.0, "observation_count": 0, "paired_count": 0,
                "power_w": None, "dispersion_w": None, "relative_dispersion_pct": None,
                "distinct_days": 0,
            }
        expanded: list[float] = []
        for value, weight, _ts in weighted:
            expanded.extend([value] * max(1, int(round(weight * 4))))
        center = robust_median(expanded)
        spread = 1.4826 * mad(expanded, center) if center is not None else None
        rel = spread / center if center and spread is not None and center > 0 else None
        days = {ts[:10] for _value, _weight, ts in weighted if len(ts) >= 10}
        effective_n = max(1.0, total_weight)
        standard_error = (float(spread) / math.sqrt(effective_n)) if spread is not None else None
        half_width = 1.96 * standard_error if standard_error is not None else None
        return {
            "evidence_units": round(total_weight, 3),
            "observation_count": len(weighted),
            "paired_count": int(model.paired_evidence_count if model else 0),
            "power_w": round(float(center), 3) if center is not None else None,
            "dispersion_w": round(float(spread), 3) if spread is not None else None,
            "relative_dispersion_pct": round(float(rel) * 100.0, 2) if rel is not None else None,
            "confidence_interval_low_w": round(max(0.0, float(center) - half_width), 3) if center is not None and half_width is not None else None,
            "confidence_interval_high_w": round(float(center) + half_width, 3) if center is not None and half_width is not None else None,
            "distinct_days": len(days),
        }

    def _record_soft_edge_evidence(
        self, pending: PendingTransition, *, delta_w: float, noise_w: float,
        accepted: bool, reason: str, concurrent: bool, availability_issue: bool,
    ) -> None:
        """Accumulate graded state-confirmed evidence without weakening hard acceptance."""
        if not self._supports_soft_fixed_evidence(pending.entity_id) or availability_issue:
            return
        level_delta = pending.new_level - pending.old_level
        if abs(level_delta) < 0.95:
            return
        magnitude = abs(delta_w)
        local_noise = max(0.5, float(noise_w or 0.0))
        weak_threshold = max(3.0, 1.5 * local_noise)
        strong_threshold = max(self.min_step_w, 3.0 * local_noise)
        if magnitude < weak_threshold or delta_w * level_delta <= 0:
            return
        if magnitude > self._maximum_plausible_w(pending.entity_id):
            return
        snr = magnitude / local_noise
        if accepted:
            # Clean accepted edges already contribute through validated sessions;
            # retain only a small independent evidence weight here to avoid
            # double-counting the same physical cycle.
            weight = 0.25 if magnitude >= strong_threshold else 0.20
            kind = "accepted_strong" if magnitude >= strong_threshold else "accepted_weak_snr"
        elif reason == "step_too_small":
            weight = max(0.35, min(0.6, snr / 3.0))
            kind = "weak_step"
        elif concurrent or reason == "concurrent_tracked_transition":
            weight = max(0.2, min(0.4, snr / 5.0))
            kind = "confounded_transition"
        else:
            return
        model = self.models[pending.entity_id]
        ts = self._utc_for_monotonic(pending.event_ts)
        direction = "up" if level_delta > 0 else "down"
        pair_bonus = 0.0
        prior = model.pending_soft_edge
        if isinstance(prior, dict) and prior.get("direction") != direction:
            try:
                prior_value = float(prior.get("value_w", 0.0) or 0.0)
                prior_ts = datetime.fromisoformat(str(prior.get("ts"))).astimezone(timezone.utc)
                current_ts = datetime.fromisoformat(ts).astimezone(timezone.utc)
                pair_age = current_ts - prior_ts
            except (TypeError, ValueError):
                prior_value = 0.0
                pair_age = timedelta(days=99)
            if prior_value > 0 and timedelta(0) <= pair_age <= timedelta(hours=24):
                mismatch = abs(magnitude - prior_value) / max(0.1, (magnitude + prior_value) / 2.0)
                if mismatch <= 0.30:
                    pair_bonus = 0.35 if mismatch <= 0.15 else 0.20
                    model.paired_evidence_count += 1
                    model.pending_soft_edge = None
        if model.pending_soft_edge is not None or pair_bonus == 0.0:
            model.pending_soft_edge = {"ts": ts, "direction": direction, "value_w": round(magnitude, 3)}
        row = {
            "ts": ts, "value_w": round(magnitude, 3), "weight": round(weight + pair_bonus, 3),
            "base_weight": round(weight, 3), "pair_bonus": round(pair_bonus, 3),
            "snr": round(snr, 2), "noise_w": round(local_noise, 3), "kind": kind,
            "source_reason": reason, "direction": direction,
        }
        model.soft_evidence_observations.append(row)
        if len(model.soft_evidence_observations) > 80:
            del model.soft_evidence_observations[:-80]
        self._energy_readiness_cache.pop(pending.entity_id, None)
        self._profile_assessment_cache.pop(pending.entity_id, None)
        self._refresh_profile_lifecycle(pending.entity_id)

    def _fixed_power_prior_w(self, entity_id: str) -> float | None:
        """Return a conservative prior for decomposing simultaneous fixed-load edges."""
        if not self._supports_soft_fixed_evidence(entity_id):
            return None
        model = self.models.get(entity_id)
        candidates: list[float] = []
        if model:
            channel = model.channels.get("level")
            if channel and len(channel.accepted_values) >= 3:
                center = robust_median(channel.accepted_values)
                if center is not None and center > 0:
                    candidates.append(float(center))
        soft = self._soft_evidence_summary(entity_id)
        if float(soft.get("evidence_units", 0.0) or 0.0) >= 3.0 and soft.get("power_w") is not None:
            candidates.append(float(soft["power_w"]))
        profile = self.device_profiles.get(entity_id)
        if profile and profile.powercalc_power_w is not None and profile.lifecycle_state in {
            "validated", "validated_native", "audit", "graduated_to_powercalc"
        }:
            candidates.append(float(profile.powercalc_power_w))
        return robust_median(candidates) if candidates else None

    def _record_decomposed_concurrent_evidence(
        self, pending: PendingTransition, *, delta_w: float, noise_w: float,
        observation_end: float,
    ) -> bool:
        """Subtract known simultaneous fixed-load edges and learn from the remainder."""
        if not self._supports_soft_fixed_evidence(pending.entity_id):
            return False
        start = pending.event_ts - ISOLATION_CONCURRENCY_SECONDS
        contributors: list[dict[str, Any]] = []
        expected_other_delta = 0.0
        for item in self._transition_detail_log:
            ts = float(item.get("ts", 0.0) or 0.0)
            eid = str(item.get("entity_id") or "")
            if eid == pending.entity_id or not (start <= ts <= observation_end):
                continue
            prior = self._fixed_power_prior_w(eid)
            level_delta = float(item.get("level_delta", 0.0) or 0.0)
            if prior is None or abs(level_delta) < 0.05:
                continue
            contribution = prior * level_delta
            expected_other_delta += contribution
            contributors.append({
                "entity_id": eid,
                "prior_w": round(prior, 3),
                "level_delta": round(level_delta, 3),
                "expected_delta_w": round(contribution, 3),
            })
        if not contributors:
            return False
        corrected = delta_w - expected_other_delta
        target_level_delta = pending.new_level - pending.old_level
        if abs(target_level_delta) < 0.95 or corrected * target_level_delta <= 0:
            return False
        value = abs(corrected / target_level_delta)
        local_noise = max(0.5, float(noise_w or 0.0))
        threshold = max(3.0, 1.5 * local_noise)
        if value < threshold or value > self._maximum_plausible_w(pending.entity_id):
            return False
        model = self.models[pending.entity_id]
        ts = self._utc_for_monotonic(pending.event_ts)
        signature = f"decomposed:{ts}"
        if any(row.get("ensemble_signature") == signature for row in model.soft_evidence_observations[-20:]):
            return True
        snr = value / local_noise
        weight = max(0.35, min(0.65, snr / 4.0))
        model.soft_evidence_observations.append({
            "ts": ts,
            "value_w": round(value, 3),
            "weight": round(weight, 3),
            "base_weight": round(weight, 3),
            "pair_bonus": 0.0,
            "snr": round(snr, 2),
            "noise_w": round(local_noise, 3),
            "kind": "decomposed_concurrent_transition",
            "source_reason": "concurrent_tracked_transition",
            "direction": "up" if target_level_delta > 0 else "down",
            "ensemble_signature": signature,
            "raw_delta_w": round(delta_w, 3),
            "subtracted_other_delta_w": round(expected_other_delta, 3),
            "contributors": contributors,
        })
        if len(model.soft_evidence_observations) > 80:
            del model.soft_evidence_observations[:-80]
        self._concurrent_decomposed_count += 1
        self._energy_readiness_cache.pop(pending.entity_id, None)
        self._profile_assessment_cache.pop(pending.entity_id, None)
        self._refresh_profile_lifecycle(pending.entity_id)
        return True

    def _edge_symmetry_plausible(self, pending: PendingTransition, value_w: float) -> bool:
        """Check whether an OFF edge agrees with recent live/start-edge power."""
        if not (pending.old_level > 0.05 and pending.new_level <= 0.05):
            return True
        session = self.isolation_sessions.get(pending.entity_id)
        if not session:
            return True
        refs = [v for v in (session.recent_median_w, session.start_edge_w) if v is not None and v >= self.min_step_w]
        if not refs:
            return True
        domain = pending.entity_id.split(".", 1)[0]
        reference = refs[0]
        if domain in {"light", "switch"} and self._sanity_profile(pending.entity_id)["high_w"] <= 250:
            tolerance = max(8.0, reference * 0.40)
        else:
            tolerance = max(20.0, reference * 0.65)
        best_error = min(abs(value_w - ref) for ref in refs)
        session.edge_symmetry_error_w = best_error
        session.edge_symmetry_status = "confirmed" if best_error <= tolerance else "mismatch"
        return best_error <= tolerance

    def _note_isolation_transition_pending(self, pending: PendingTransition) -> None:
        """Immediately reflect physical state while an edge is being measured."""
        session = self.isolation_sessions[pending.entity_id]
        session.pending_direction = pending.direction
        if pending.new_level <= 0.05:
            # For climate entities, hvac_action can lag the actual compressor
            # stop. Keep the established session alive (but pending/paused) until
            # the electrical edge is evaluated so latent near-zero detection can
            # still validate the run. Ordinary entities can reflect OFF
            # immediately because their state edge is the physical switching edge.
            if pending.entity_id.split(".", 1)[0] != "climate":
                session.active = False
                session.reported_power_w = 0.0
                session.raw_power_w = 0.0
            session.status = "measuring_off_edge"
        elif pending.old_level <= 0.05:
            # Every candidate start begins a fresh pairing epoch. A rejected
            # start must never inherit a prior accepted start_edge_w and later
            # validate against an unrelated OFF edge.
            session.active = True
            session.channel = pending.channel
            session.validation_channel = self._validation_channel(pending.channel)
            session.pending_energy_kwh = 0.0
            session.started_ts = pending.event_ts
            session.start_edge_w = None
            session.start_edge_accepted = False
            session.baseline_w = None
            session.reported_power_w = None
            session.raw_power_w = None
            session.recent_power_w.clear()
            session.edge_symmetry_status = "unknown"
            session.edge_symmetry_error_w = None
            session.climate_off_seen_since = None
            session.clear_feature_capture()
            session.status = "measuring_on_edge"
        else:
            session.status = "measuring_level_edge"

    def _apply_isolation_observation(self, pending: PendingTransition, obs: Observation) -> None:
        """Update live-session attribution from one evaluated tracked edge."""
        session = self.isolation_sessions[pending.entity_id]
        turning_on = pending.old_level <= 0.05 and pending.new_level > 0.05
        turning_off = pending.old_level > 0.05 and pending.new_level <= 0.05
        level_change = pending.old_level > 0.05 and pending.new_level > 0.05
        session.pending_direction = None
        session.last_edge_w = abs(obs.delta_w)
        session.last_edge_direction = pending.direction
        session.last_edge_source = pending.edge_source

        if obs.accepted:
            if turning_on:
                self._shift_active_session_baselines(obs.delta_w, except_entity=pending.entity_id)
                session.active = True
                session.channel = pending.channel
                session.validation_channel = self._validation_channel(pending.channel)
                session.pending_energy_kwh = 0.0
                session.baseline_w = obs.before_w
                session.started_ts = pending.event_ts
                session.start_edge_w = abs(obs.delta_w)
                session.start_edge_accepted = True
                session.climate_off_seen_since = None
                session.baseline_adjustments = 0
                session.edge_symmetry_error_w = None
                session.overlap_seen = any(
                    other.active for other_eid, other in self.isolation_sessions.items()
                    if other_eid != pending.entity_id
                )
                session.reset_feature_capture(
                    pending.event_ts,
                    started_utc=self._utc_for_monotonic(pending.event_ts),
                    activity_source=pending.edge_source,
                    channel=pending.channel,
                    old_level=pending.old_level,
                    new_level=pending.new_level,
                )
                # The edge evaluator necessarily waits for post-event evidence.
                # Recover those first seconds from the rolling residual buffer so
                # 0.5 retains startup/ramp shape rather than beginning the waveform
                # only after the edge has already settled.
                for sample in self._sample_buffer:
                    if pending.event_ts <= sample.ts and sample.valid_for_learning:
                        session.add_power_sample(max(0.0, sample.residual_w - obs.before_w), sample.ts)
                session.status = "tracking"
                session.quality = "good"
                session.sanity_status = "plausible"
                session.edge_symmetry_status = "awaiting_off_edge"
            elif turning_off:
                self._shift_active_session_baselines(obs.delta_w, except_entity=pending.entity_id)
                if session.started_ts is not None:
                    session.last_completed_duration_s = max(0.0, pending.event_ts - session.started_ts)
                if session.start_edge_accepted and session.start_edge_w is not None:
                    model = self.models[pending.entity_id]
                    model.validated_sessions += 1
                    self._set_last_session_rejection(pending.entity_id, None)
                    vchan = session.validation_channel or self._validation_channel(pending.channel)
                    if pending.entity_id.split(".", 1)[0] == "climate" and vchan:
                        was_validated = self._channel_is_validated(pending.entity_id, vchan)
                        model.validated_channel_sessions[vchan] = model.validated_channel_sessions.get(vchan, 0) + 1
                        if not was_validated:
                            self._commit_pending_channel_energy(pending.entity_id, session)
                    self._record_completed_session_fingerprint(
                        pending.entity_id,
                        session,
                        stop_edge_w=abs(obs.delta_w),
                        end_ts=pending.event_ts,
                        quality=(
                            "validated_edge_symmetric"
                            if session.edge_symmetry_status == "confirmed"
                            else "validated"
                        ),
                    )
                session.active = False
                session.channel = None
                session.validation_channel = None
                session.pending_energy_kwh = 0.0
                session.baseline_w = None
                session.status = "off"
                session.quality = "off" if session.edge_symmetry_status != "mismatch" else "last_session_edge_mismatch"
                session.reported_power_w = 0.0
                session.raw_power_w = 0.0
                session.start_edge_accepted = False
                session.start_edge_w = None
                session.started_ts = None
                session.climate_off_seen_since = None
                session.clear_feature_capture()
            elif level_change:
                self._shift_active_session_baselines(obs.delta_w, except_entity=pending.entity_id)
                session.active = True
                session.note_setting_event(
                    pending.event_ts,
                    channel=pending.channel,
                    old_level=pending.old_level,
                    new_level=pending.new_level,
                    source=pending.edge_source,
                    kind="level_change",
                )
                session.status = "tracking"
            return

        # Rejected edges are handled conservatively. If a known session turns off,
        # use its recent live median only to keep the baselines of other active
        # devices approximately continuous; never publish that fallback as the
        # target's measured edge.
        if turning_off:
            is_climate = pending.entity_id.split(".", 1)[0] == "climate"
            # HVAC action/state callbacks can precede the actual electrical stop.
            # A valid climate start therefore enters a bounded settling state
            # instead of being rejected merely because this callback lacks an
            # immediate downward residual edge.
            if (
                is_climate
                and session.active
                and session.baseline_w is not None
                and session.start_edge_accepted
            ):
                threshold = self._latent_climate_threshold(pending.entity_id)
                raw_now = max(0.0, (self.last_residual_w or session.baseline_w) - session.baseline_w)
                if raw_now <= max(35.0, threshold * 0.25):
                    self._finish_latent_climate_session(
                        pending.entity_id,
                        time.monotonic(),
                        record_observation=(session.activity_source == "residual_change_point"),
                        edge_source="hvac_action_low_residual",
                    )
                    return
                session.climate_off_seen_since = session.climate_off_seen_since or time.monotonic()
                session.status = "settling_after_climate_off"
                session.quality = "awaiting_electrical_stop"
                session.last_edge_source = "hvac_action_off_wait"
                return

            fallback = session.recent_median_w or session.start_edge_w
            if fallback is not None and fallback >= self.min_step_w:
                self._shift_active_session_baselines(
                    -fallback,
                    except_entity=pending.entity_id,
                    reason="estimated_off_adjustment",
                )
            if session.start_edge_accepted and session.start_edge_w is not None:
                model = self.models[pending.entity_id]
                vchan = session.validation_channel or self._validation_channel(pending.channel)
                benign_confounded = (
                    not is_climate
                    and obs.reason in {"concurrent_tracked_transition", "step_too_small", "insufficient_samples"}
                )
                if not benign_confounded:
                    model.rejected_sessions += 1
                    if is_climate and vchan:
                        model.rejected_channel_sessions[vchan] = model.rejected_channel_sessions.get(vchan, 0) + 1
                    self._set_last_session_rejection(
                        pending.entity_id,
                        obs.reason or "unconfirmed_off_edge",
                        vchan or pending.channel,
                    )
            session.pending_energy_kwh = 0.0
            if session.started_ts is not None:
                session.last_completed_duration_s = max(0.0, pending.event_ts - session.started_ts)
            session.active = False
            session.channel = None
            session.validation_channel = None
            session.baseline_w = None
            session.status = "off"
            session.quality = "last_session_unconfirmed"
            session.reported_power_w = 0.0
            session.raw_power_w = 0.0
            session.start_edge_accepted = False
            session.start_edge_w = None
            session.started_ts = None
            session.climate_off_seen_since = None
            session.clear_feature_capture()
        elif turning_on:
            # The physical entity may now be ON, but without an accepted start
            # edge there is no trustworthy isolation session to pair with a later
            # OFF edge. Climate devices may still be recovered by residual
            # change-point fallback after the explicit-edge grace window.
            session.active = False
            session.channel = None
            session.validation_channel = None
            session.pending_energy_kwh = 0.0
            session.baseline_w = None
            session.started_ts = None
            session.start_edge_w = None
            session.start_edge_accepted = False
            session.climate_off_seen_since = None
            session.status = "awaiting_clean_edge"
            session.quality = "ambiguous_start"
            session.reported_power_w = None
            session.raw_power_w = None
            session.recent_power_w.clear()
            session.clear_feature_capture()
        else:
            session.status = "tracking_degraded" if session.active else "off"
            session.quality = "degraded" if session.active else "off"

    def _handle_tracked_transition(
        self,
        entity_id: str,
        old_state: State | None,
        new_state: State | None,
        now: float,
    ) -> None:
        """Nominate an entity transition for later residual evaluation."""
        if not old_state or not new_state:
            return
        if old_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE) or new_state.state in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return

        domain = entity_id.split(".", 1)[0]
        if domain == "climate":
            old_action = str(old_state.attributes.get("hvac_action", "idle")).lower()
            new_action = str(new_state.attributes.get("hvac_action", "idle")).lower()
            if old_action == new_action:
                return
            old_active = old_action in ACTIVE_HVAC_ACTIONS
            new_active = new_action in ACTIVE_HVAC_ACTIONS
            if old_active and new_active:
                # A channel change inside one physical HVAC run does not require a
                # different power model. Keep the same counterfactual baseline and
                # simply annotate the unified session with its new activity channel.
                session = self.isolation_sessions.get(entity_id)
                if session:
                    old_vchan = session.validation_channel
                    new_vchan = self._validation_channel(new_action)
                    if old_vchan != new_vchan and session.pending_energy_kwh > 0:
                        # No complete validating cycle occurred for the provisional
                        # channel segment, so do not leak it into canonical energy.
                        session.pending_energy_kwh = 0.0
                    session.note_setting_event(
                        now,
                        channel=new_action,
                        old_level=1.0,
                        new_level=1.0,
                        source="hvac_action",
                        kind="channel_change",
                    )
                    session.channel = new_action
                    session.validation_channel = new_vchan
                    if session.baseline_w is not None:
                        session.status = "tracking"
                return
            session = self.isolation_sessions.get(entity_id)
            if old_active and not new_active:
                # A residual change-point may already have closed a compressor
                # session while the thermostat still reported an active action.
                # Do not manufacture a duplicate OFF edge when hvac_action later
                # catches up.
                if session and not session.active:
                    self._last_explicit_activity_edge[entity_id] = now
                    return
                channel = old_action
                old_level, new_level = 1.0, 0.0
            elif new_active and not old_active:
                # Likewise, if a residual edge already established a session, an
                # eventual hvac_action change only refines its channel label.
                if session and session.active and session.baseline_w is not None:
                    session.note_setting_event(
                        now,
                        channel=new_action,
                        old_level=0.0,
                        new_level=1.0,
                        source="hvac_action",
                        kind="channel_refined",
                    )
                    session.channel = new_action
                    session.validation_channel = self._validation_channel(new_action)
                    self._last_explicit_activity_edge[entity_id] = now
                    return
                channel = new_action
                old_level, new_level = 0.0, 1.0
            else:
                return
            self._last_explicit_activity_edge[entity_id] = now
        else:
            old_level = self._entity_level(entity_id, old_state)
            new_level = self._entity_level(entity_id, new_state)
            if old_level is None or new_level is None or abs(new_level - old_level) < 0.05:
                return
            channel = "level"

        direction = "up" if new_level > old_level else "down"
        self._transition_log.append((now, entity_id))
        self._transition_detail_log.append({
            "ts": now,
            "entity_id": entity_id,
            "level_delta": float(new_level - old_level),
            "channel": channel,
        })
        # Locked manual fixed-power profiles still participate in concurrency
        # accounting, but are no longer monitored for model improvement.
        if self._manual_fixed_locked(entity_id):
            session = self.isolation_sessions.get(entity_id)
            if session:
                active = self._state_is_active(entity_id, new_state)
                session.active = False
                session.pending_direction = None
                session.baseline_w = None
                session.reported_power_w = 0.0
                session.raw_power_w = 0.0
                session.status = "manual_fixed_locked" if active else "off"
                session.quality = "manual_fixed" if active else "off"
            return
        generation = self._transition_generation.get(entity_id, 0) + 1
        self._transition_generation[entity_id] = generation
        pending = PendingTransition(
            entity_id=entity_id,
            channel=channel,
            event_ts=now,
            old_level=old_level,
            new_level=new_level,
            direction=direction,
            edge_source="entity_state",
            generation=generation,
        )
        self._note_isolation_transition_pending(pending)
        task = self.entry.async_create_task(
            self.hass,
            self._async_evaluate_transition(pending),
            name=f"Power Fingerprint evaluate {entity_id}",
            eager_start=True,
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _async_evaluate_transition(self, pending: PendingTransition) -> None:
        """Evaluate a transition once enough post-event residual evidence exists."""
        is_climate = pending.entity_id.split(".", 1)[0] == "climate"

        # The sampler and entity callbacks are asynchronous for every domain.
        # Extend ordinary windows too when jitter or a guarded sample leaves the
        # nominal six-second window short. Never reduce the clean-sample gate.
        if is_climate:
            await asyncio.sleep(ISOLATION_SETTLE_SECONDS + 1.0)
        else:
            await asyncio.sleep(ISOLATION_SETTLE_SECONDS + ISOLATION_POST_SECONDS + 1.0)
        if not self.learning_enabled or self._manual_fixed_locked(pending.entity_id):
            return
        if pending.generation and pending.generation != self._transition_generation.get(pending.entity_id):
            return

        before_start = pending.event_ts - max(
            ISOLATION_PRE_SECONDS, self.min_samples * self.sample_interval_seconds + 2.0
        )
        before_end = pending.event_ts - 2.0
        after_start = pending.event_ts + ISOLATION_SETTLE_SECONDS
        after_deadline = after_start + self._edge_sample_timeout_seconds(is_climate)
        event_wall_ts = datetime.fromisoformat(self._utc_for_monotonic(pending.event_ts)).timestamp()

        after_all: list[Sample] = []
        after: list[float] = []
        while True:
            now = time.monotonic()
            after_end = min(now, after_deadline)
            after_all = [s for s in self._sample_buffer if after_start <= s.ts <= after_end]
            # Timer ticks do not imply a new whole-house reading. If the source
            # exposes report timestamps, exclude held pre-event readings until
            # the meter has actually reported again. This also gives a polling
            # source time to reflect the physical device edge.
            after = [
                s.residual_w for s in after_all
                if s.valid_for_learning
                and (s.source_reported_at is None or s.source_reported_at >= event_wall_ts)
            ]
            if len(after) >= self.min_samples or now >= after_deadline:
                break
            await asyncio.sleep(max(0.5, min(1.0, self.sample_interval_seconds / 2.0)))
            if not self.learning_enabled:
                return
            if pending.generation and pending.generation != self._transition_generation.get(pending.entity_id):
                return

        if pending.generation and pending.generation != self._transition_generation.get(pending.entity_id):
            return
        now = time.monotonic()
        before_all = [s for s in self._sample_buffer if before_start <= s.ts <= before_end]
        before = [s.residual_w for s in before_all if s.valid_for_learning]

        reason = "accepted"
        accepted = False
        before_med = robust_median(before)
        after_med = robust_median(after)
        noise = max(mad(before), mad(after)) if before and after else 9999.0
        delta = (after_med - before_med) if before_med is not None and after_med is not None else 0.0
        level_delta = pending.new_level - pending.old_level
        value = abs(delta / level_delta) if abs(level_delta) >= 0.05 else 0.0
        effective_min_step = self._climate_minimum_edge_w(pending.channel) if is_climate else self.min_step_w
        used_change_point = False

        # Treat hvac_action as a cue, not as proof that the compressor changed on
        # that exact callback. If the direct pre/post view is incomplete, tiny, or
        # has the wrong sign, search a bounded neighborhood for the actual whole-
        # house residual change-point. Both Gree and Ecobee use this same adapter.
        if is_climate and (
            len(before) < self.min_samples
            or len(after) < self.min_samples
            or abs(delta) < effective_min_step
            or delta * level_delta <= 0
        ):
            candidate = self._find_climate_change_point(pending)
            if candidate is not None:
                before_med = candidate["before_w"]
                after_med = candidate["after_w"]
                noise = candidate["noise_w"]
                delta = candidate["delta_w"]
                value = abs(delta / level_delta) if abs(level_delta) >= 0.05 else abs(delta)
                pending.edge_source = "climate_cue_change_point"
                pending.event_ts = candidate.get("edge_ts", pending.event_ts)
                used_change_point = True

        culprit_meter: str | None = None
        enough_samples = (
            used_change_point
            or (len(before) >= self.min_samples and len(after) >= self.min_samples)
        )
        concurrent_transition = (
            enough_samples and not used_change_point
            and self._has_concurrent_transition(pending, observation_end=after_end)
        )
        if not enough_samples:
            reason = (
                "source_not_refreshed"
                if after_all and all(
                    s.source_reported_at is not None and s.source_reported_at < event_wall_ts
                    for s in after_all
                )
                else "insufficient_samples"
            )
        elif concurrent_transition:
            reason = "concurrent_tracked_transition"
        else:
            availability_issue = self._meter_availability_overlap(pending, now, observation_end=after_end)
            if availability_issue:
                culprit_meter, availability_kind = availability_issue
                reason = availability_kind
            else:
                if noise > self.max_noise_w:
                    reason = "background_too_noisy"
                elif abs(delta) < effective_min_step:
                    reason = "climate_step_too_small" if is_climate else "step_too_small"
                elif delta * level_delta <= 0:
                    reason = "wrong_step_direction"
                elif value > self._maximum_plausible_w(pending.entity_id):
                    reason = "implausible_for_device"
                elif not self._edge_symmetry_plausible(pending, abs(delta)):
                    reason = "edge_symmetry_mismatch"
                else:
                    accepted = True

        obs = Observation(
            ts=self._utc_for_monotonic(pending.event_ts),
            entity_id=pending.entity_id,
            channel=pending.channel,
            value_w=value,
            delta_w=delta,
            level_delta=level_delta,
            direction=pending.direction,
            before_w=before_med or 0.0,
            after_w=after_med or 0.0,
            noise_w=noise,
            accepted=accepted,
            reason=reason,
            culprit_meter=culprit_meter,
            edge_source=pending.edge_source,
            before_sample_count=len(before),
            after_sample_count=len(after),
        )
        self.models[pending.entity_id].channel(pending.channel).add(obs, self.max_observations)
        decomposed_concurrent = False
        if concurrent_transition and not culprit_meter:
            decomposed_concurrent = self._record_decomposed_concurrent_evidence(
                pending, delta_w=delta, noise_w=noise, observation_end=after_end
            )
        if not decomposed_concurrent:
            self._record_soft_edge_evidence(
                pending,
                delta_w=delta,
                noise_w=noise,
                accepted=accepted,
                reason=reason,
                concurrent=bool(concurrent_transition),
                availability_issue=bool(culprit_meter),
            )
        self._apply_isolation_observation(pending, obs)
        self.last_observation = obs.as_dict()
        if accepted:
            self.accepted_count += 1
            self.session_accepted_count += 1
            self._process_profile_audit_observation(pending, obs)
        else:
            self.rejected_count += 1
            self.session_rejected_count += 1
            self.rejection_reasons[reason] += 1
            self.session_rejection_reasons[reason] += 1
        self._schedule_save()
        if self.auto_export_energy and self.device_energy_dashboard_ready(pending.entity_id):
            self._schedule_energy_dashboard_sync()
        self._publish_if_due(time.monotonic(), force=True)

    def _source_report_interval_seconds(self) -> float | None:
        """Observed report cadence, distinct from the residual timer cadence."""
        reports = list(self._source_report_times)[-11:]
        return robust_median([b - a for a, b in zip(reports, reports[1:]) if b > a])

    def _edge_sample_timeout_seconds(self, is_climate: bool = False) -> float:
        """Bound post-edge waits while allowing the configured sampler to catch up."""
        minimum = CLIMATE_EDGE_SAMPLE_TIMEOUT_SECONDS if is_climate else ORDINARY_EDGE_SAMPLE_TIMEOUT_SECONDS
        interval = self._source_report_interval_seconds()
        source_wait = min(
            MAX_SOURCE_ADAPTED_POST_SECONDS,
            (interval or 0.0) + (self.min_samples + 1) * self.sample_interval_seconds,
        )
        return max(minimum, (self.min_samples + 2) * self.sample_interval_seconds, source_wait)

    def _other_tracked_transition_near(self, entity_id: str, when: float, radius: float) -> bool:
        """Return whether another tracked entity changed near an electrical edge."""
        return any(
            other_eid != entity_id and abs(ts - when) <= radius
            for ts, other_eid in self._transition_log
        )

    def _find_climate_change_point(self, pending: PendingTransition) -> dict[str, float] | None:
        """Find an HVAC electrical edge near an hvac_action cue.

        Start cues search a short window around/after the callback. Stop cues also
        look backwards because thermostats commonly report idle only after the
        compressor has already dropped out. Candidate edges require robust pre/post
        medians, the expected sign, a climate-sized step, and no nearby transition
        from another tracked device.
        """
        turning_on = pending.new_level > pending.old_level
        if turning_on:
            search_start = pending.event_ts - 6.0
            search_end = pending.event_ts + 16.0
        else:
            search_start = pending.event_ts - 35.0
            search_end = pending.event_ts + 8.0

        minimum = self._climate_minimum_edge_w(pending.channel)
        maximum = self._maximum_plausible_w(pending.entity_id)
        candidates: list[tuple[float, float, float, float, float, float]] = []

        split_times = [
            s.ts for s in self._sample_buffer
            if search_start <= s.ts <= search_end and s.valid_for_learning
        ]
        for split in split_times:
            if self._other_tracked_transition_near(pending.entity_id, split, ISOLATION_CONCURRENCY_SECONDS):
                continue
            pre_rows = [
                s for s in self._sample_buffer
                if split - 12.0 <= s.ts <= split - 2.0 and s.valid_for_learning
            ]
            post_rows = [
                s for s in self._sample_buffer
                if split + 2.0 <= s.ts <= split + 12.0 and s.valid_for_learning
            ]
            if len(pre_rows) < self.min_samples or len(post_rows) < self.min_samples:
                continue
            pre_samples = [s.corrected_w for s in pre_rows]
            post_samples = [s.corrected_w for s in post_rows]
            pre = robust_median(pre_samples)
            post = robust_median(post_samples)
            raw_pre = robust_median([s.residual_w for s in pre_rows])
            if pre is None or post is None or raw_pre is None:
                continue
            delta = post - pre
            if delta * (pending.new_level - pending.old_level) <= 0:
                continue
            if abs(delta) < minimum or abs(delta) > maximum:
                continue
            noise = max(mad(pre_samples), mad(post_samples))
            if noise > self.max_noise_w:
                continue
            # Strong, quiet edges win; proximity to the thermostat cue is a mild
            # tie-breaker rather than a hard assumption about exact timing.
            score = abs(delta) / max(1.0, noise) - abs(split - pending.event_ts) * 0.15
            candidates.append((score, split, pre, post, raw_pre, noise))

        if not candidates:
            return None
        _score, split, pre, post, raw_pre, noise = max(candidates, key=lambda x: x[0])
        delta = post - pre
        return {
            "edge_ts": split,
            "before_w": raw_pre,
            "after_w": raw_pre + delta,
            "delta_w": delta,
            "noise_w": noise,
        }

    def _find_climate_relative_stop(
        self, entity_id: str, now: float
    ) -> dict[str, float] | None:
        """Recover a climate stop from a sustained *relative* electrical drop.

        The normal climate stop path intentionally prefers an absolute return to
        the session's counterfactual baseline.  That is the cleanest evidence,
        but an unrelated unmetered load can appear during a long HVAC run and
        permanently lift the residual baseline.  In that case an otherwise good
        HVAC session used to time out even when its waveform contained a clear
        compressor/blower shutdown.

        This fallback is deliberately used only after the climate OFF grace
        interval has elapsed.  It searches the session's already-baseline-adjusted
        feature waveform from shortly before the OFF cue through the end of the
        grace interval.  A candidate must have quiet pre/post plateaus, a
        climate-sized downward step, no nearby transition from another tracked
        entity, and a post-step level that is substantially below the pre-step
        level.  The remaining post-step power is left unexplained rather than
        being attributed to the HVAC device.
        """
        session = self.isolation_sessions.get(entity_id)
        if (
            session is None
            or not session.active
            or session.started_ts is None
            or session.climate_off_seen_since is None
            or not session.start_edge_accepted
            or session.start_edge_w is None
        ):
            return None

        cue_rel = max(0.0, session.climate_off_seen_since - session.started_ts)
        now_rel = max(0.0, now - session.started_ts)
        search_start_rel = max(0.0, cue_rel - CLIMATE_RELATIVE_STOP_LOOKBACK_SECONDS)
        search_end_rel = min(
            now_rel,
            cue_rel + CLIMATE_ELECTRICAL_STOP_GRACE_SECONDS,
        )
        rows = [
            (float(t), float(w))
            for t, w in session.feature_samples
            if search_start_rel <= t <= search_end_rel
        ]
        if len(rows) < self.min_samples * 2:
            return None

        minimum_drop = self._climate_minimum_edge_w(session.channel)
        post_ceiling = max(
            CLIMATE_RELATIVE_STOP_ABSOLUTE_POST_CEILING_W,
            min(
                500.0,
                session.start_edge_w * CLIMATE_RELATIVE_STOP_START_EDGE_MULTIPLIER,
            ),
        )
        candidates: list[dict[str, float]] = []

        for split_rel, _ in rows:
            # Need a complete post-step plateau before treating this as settled.
            if split_rel + CLIMATE_RELATIVE_STOP_POST_SECONDS > search_end_rel:
                continue
            split_abs = session.started_ts + split_rel
            if self._other_tracked_transition_near(
                entity_id, split_abs, ISOLATION_CONCURRENCY_SECONDS
            ):
                continue

            pre = [
                w
                for t, w in rows
                if split_rel - CLIMATE_RELATIVE_STOP_PRE_SECONDS
                <= t
                <= split_rel - CLIMATE_RELATIVE_STOP_EDGE_GAP_SECONDS
            ]
            post = [
                w
                for t, w in rows
                if split_rel + CLIMATE_RELATIVE_STOP_EDGE_GAP_SECONDS
                <= t
                <= split_rel + CLIMATE_RELATIVE_STOP_POST_SECONDS
            ]
            if len(pre) < self.min_samples or len(post) < self.min_samples:
                continue

            pre_med = robust_median(pre)
            post_med = robust_median(post)
            if pre_med is None or post_med is None or pre_med <= 0.0:
                continue
            drop = pre_med - post_med
            if drop < minimum_drop:
                continue
            drop_fraction = drop / max(pre_med, 1.0)
            if drop_fraction < CLIMATE_RELATIVE_STOP_MIN_DROP_FRACTION:
                continue
            if (
                post_med > post_ceiling
                and post_med > pre_med * CLIMATE_RELATIVE_STOP_MAX_POST_PRE_FRACTION
            ):
                continue

            noise = max(mad(pre), mad(post))
            # Variable-speed HVAC can move somewhat during the pre-stop plateau,
            # so scale the noise allowance with a strong drop while still
            # requiring a stable post-stop plateau.
            if noise > max(self.max_noise_w * 2.0, drop * 0.25):
                continue

            candidates.append({
                "edge_ts": split_abs,
                "edge_w": drop,
                "pre_w": pre_med,
                "post_w": post_med,
                "noise_w": noise,
                "cue_distance_s": abs(split_abs - session.climate_off_seen_since),
            })

        if not candidates:
            return None

        # Prefer the candidate that leaves the lowest sustained post-stop power;
        # this naturally favors the final stage of a staggered compressor/blower
        # shutdown.  Proximity to the thermostat cue and larger drop are
        # secondary tie-breakers.
        return min(
            candidates,
            key=lambda c: (c["post_w"], c["cue_distance_s"], -c["edge_w"]),
        )

    def _has_concurrent_transition(
        self, pending: PendingTransition, *, observation_end: float | None = None
    ) -> bool:
        """True if another tracked entity changed in the observation window."""
        start = pending.event_ts - max(
            ISOLATION_PRE_SECONDS, self.min_samples * self.sample_interval_seconds + 2.0
        )
        end = observation_end if observation_end is not None else pending.event_ts + ISOLATION_CONCURRENCY_SECONDS
        for ts, eid in self._transition_log:
            if eid != pending.entity_id and start <= ts <= end:
                return True
        return False

    def _meter_availability_overlap(
        self, pending: PendingTransition, evaluation_now: float, *, observation_end: float | None = None
    ) -> tuple[str, str] | None:
        """Return a meter/kind when availability uncertainty overlaps observation."""
        obs_start = pending.event_ts - max(
            ISOLATION_PRE_SECONDS, self.min_samples * self.sample_interval_seconds + 2.0
        )
        obs_end = observation_end if observation_end is not None else pending.event_ts + ISOLATION_SETTLE_SECONDS + ISOLATION_POST_SECONDS
        for start, end, eid, kind in reversed(self._meter_availability_intervals):
            if end < obs_start:
                break
            if start <= obs_end and end >= obs_start:
                return eid, kind
        return None

    def _maximum_plausible_w(self, entity_id: str) -> float:
        """Device-aware sanity ceiling used to reject contaminated edges."""
        return float(self._sanity_profile(entity_id)["high_w"])

    def _entity_level(self, entity_id: str, state: State) -> float | None:
        """Map a source state to a normalized 0..1 operating level."""
        domain = entity_id.split(".", 1)[0]
        if domain == "light":
            if state.state == STATE_OFF:
                return 0.0
            brightness = state.attributes.get("brightness")
            if brightness is not None:
                with suppress(TypeError, ValueError):
                    return max(0.0, min(1.0, float(brightness) / 255.0))
            return 1.0 if state.state == STATE_ON else None
        if domain == "fan":
            if state.state == STATE_OFF:
                return 0.0
            percentage = state.attributes.get("percentage")
            if percentage is not None:
                with suppress(TypeError, ValueError):
                    return max(0.0, min(1.0, float(percentage) / 100.0))
            return 1.0 if state.state == STATE_ON else None
        if domain == "switch":
            return 1.0 if state.state == STATE_ON else 0.0 if state.state == STATE_OFF else None
        if domain == "media_player":
            return 0.0 if state.state in OFFISH_MEDIA_STATES else 1.0
        return None

    def _latent_climate_threshold(self, entity_id: str) -> float:
        """Return generic climate change-point threshold for activity detection."""
        model = self.models.get(entity_id)
        prior_values: list[float] = []
        if model:
            for key, channel in model.channels.items():
                floor = self._climate_minimum_edge_w(key)
                prior_values.extend(v for v in channel.accepted_values if v >= floor)
            prior_values.extend(model.legacy_cycle_values_w)
        center = robust_median(prior_values)
        if center and center > 0:
            return max(50.0, min(self.latent_climate_min_step_w, center * 0.35))
        return max(50.0, self.latent_climate_min_step_w)

    def _record_latent_climate_observation(
        self, entity_id: str, channel: str, *, direction: str, value_w: float,
        before_w: float, after_w: float, noise_w: float, event_ts: float
    ) -> None:
        """Persist a generic change-point activity edge as normal evidence."""
        level_delta = 1.0 if direction == "up" else -1.0
        delta_w = abs(value_w) if direction == "up" else -abs(value_w)
        obs = Observation(
            ts=self._utc_for_monotonic(event_ts),
            entity_id=entity_id,
            channel=channel,
            value_w=abs(value_w),
            delta_w=delta_w,
            level_delta=level_delta,
            direction=direction,
            before_w=before_w,
            after_w=after_w,
            noise_w=noise_w,
            accepted=True,
            reason="accepted_change_point",
            edge_source="residual_change_point",
        )
        self.models[entity_id].channel(channel).add(obs, self.max_observations)
        self.last_observation = obs.as_dict()
        self.accepted_count += 1
        self.session_accepted_count += 1

    def _set_last_session_rejection(
        self,
        entity_id: str,
        reason: str | None,
        channel: str | None = None,
    ) -> None:
        """Persist a compact per-device last-session rejection diagnostic."""
        model = self.models.get(entity_id)
        if model is None:
            return
        model.last_session_rejection_reason = reason
        model.last_session_rejection_channel = channel if reason else None
        model.last_session_rejection_at = utc_iso() if reason else None

    def _abort_climate_session(self, entity_id: str, now: float, reason: str) -> None:
        """Discard an orphaned/contradictory climate session conservatively."""
        session = self.isolation_sessions[entity_id]
        if not session.active:
            return
        fallback = session.recent_median_w or session.start_edge_w or 0.0
        if fallback >= self.min_step_w:
            self._shift_active_session_baselines(-fallback, except_entity=entity_id, reason=reason)
        model = self.models[entity_id]
        if session.start_edge_accepted and session.start_edge_w is not None:
            model.rejected_sessions += 1
            vchan = session.validation_channel or self._validation_channel(session.channel)
            if vchan:
                model.rejected_channel_sessions[vchan] = model.rejected_channel_sessions.get(vchan, 0) + 1
            self._set_last_session_rejection(entity_id, reason, vchan or session.channel)
            self.rejected_count += 1
            self.session_rejected_count += 1
            self.rejection_reasons[reason] += 1
            self.session_rejection_reasons[reason] += 1
        if session.started_ts is not None:
            session.last_completed_duration_s = max(0.0, now - session.started_ts)
        session.active = False
        session.channel = None
        session.validation_channel = None
        session.pending_energy_kwh = 0.0
        session.baseline_w = None
        session.reported_power_w = 0.0
        session.raw_power_w = 0.0
        session.status = "off"
        session.quality = f"last_session_{reason}"
        session.pending_direction = None
        session.latent_start_candidate_since = None
        session.latent_stop_candidate_since = None
        session.latent_stop_edge_w = None
        session.climate_off_seen_since = None
        session.start_edge_accepted = False
        session.start_edge_w = None
        session.started_ts = None
        session.last_edge_source = reason
        session.clear_feature_capture()
        self._schedule_save()

    def _finish_latent_climate_session(
        self,
        entity_id: str,
        now: float,
        *,
        record_observation: bool = True,
        edge_source: str = "residual_change_point",
    ) -> None:
        """Close a climate session from electrical inactivity or an OFF cue."""
        session = self.isolation_sessions[entity_id]
        if not session.active or session.baseline_w is None:
            return
        edge_w = session.latent_stop_edge_w or session.recent_median_w or session.start_edge_w or 0.0
        if edge_w >= self.min_step_w:
            self._shift_active_session_baselines(-edge_w, except_entity=entity_id, reason="latent_climate_stop")
        physical_end_ts = session.latent_stop_candidate_since or now
        duration = max(0.0, physical_end_ts - session.started_ts) if session.started_ts is not None else 0.0
        session.last_completed_duration_s = duration
        validated = session.start_edge_accepted and session.start_edge_w is not None and duration >= 60.0
        if validated:
            model = self.models[entity_id]
            model.validated_sessions += 1
            self._set_last_session_rejection(entity_id, None)
            vchan = session.validation_channel or self._validation_channel(session.channel)
            if vchan:
                was_validated = self._channel_is_validated(entity_id, vchan)
                model.validated_channel_sessions[vchan] = model.validated_channel_sessions.get(vchan, 0) + 1
                if not was_validated:
                    self._commit_pending_channel_energy(entity_id, session)
        else:
            model = self.models[entity_id]
            model.rejected_sessions += 1
            vchan = session.validation_channel or self._validation_channel(session.channel)
            if vchan:
                model.rejected_channel_sessions[vchan] = model.rejected_channel_sessions.get(vchan, 0) + 1
            session.pending_energy_kwh = 0.0
            self.rejected_count += 1
            self.session_rejected_count += 1
            self.rejection_reasons["latent_session_too_short"] += 1
            self.session_rejection_reasons["latent_session_too_short"] += 1
            self._set_last_session_rejection(
                entity_id,
                "latent_session_too_short",
                vchan or session.channel,
            )
        channel = session.channel or "climate"
        if record_observation and validated:
            # Deferred evidence commit: latent HVAC edges are persisted only as a
            # complete UP/DOWN pair. Never recreate the pre-0.4.4 one-sided-start
            # failure mode when the stop confirmation occurs after power is near 0.
            floor = self._climate_minimum_edge_w(channel)
            if session.start_edge_w is not None and session.start_edge_w >= floor and edge_w >= floor:
                self._record_latent_climate_observation(
                    entity_id, channel, direction="up", value_w=session.start_edge_w,
                    before_w=session.baseline_w, after_w=(session.baseline_w + session.start_edge_w),
                    noise_w=0.0, event_ts=session.started_ts or now,
                )
                self._record_latent_climate_observation(
                    entity_id, channel, direction="down", value_w=edge_w,
                    before_w=(session.baseline_w + edge_w), after_w=session.baseline_w,
                    noise_w=0.0, event_ts=physical_end_ts,
                )
        if validated:
            self._record_completed_session_fingerprint(
                entity_id,
                session,
                stop_edge_w=edge_w if edge_w > 0 else None,
                end_ts=physical_end_ts,
                quality="validated_latent_climate",
            )
        session.active = False
        session.channel = None
        session.validation_channel = None
        session.pending_energy_kwh = 0.0
        session.baseline_w = None
        session.reported_power_w = 0.0
        session.raw_power_w = 0.0
        session.status = "off"
        session.quality = "off"
        session.last_edge_w = edge_w or None
        session.last_edge_direction = "down"
        session.last_edge_source = edge_source
        session.latent_stop_candidate_since = None
        session.latent_stop_edge_w = None
        session.latent_start_candidate_since = None
        session.climate_off_seen_since = None
        session.start_edge_accepted = False
        session.start_edge_w = None
        session.started_ts = None
        session.clear_feature_capture()
        self._schedule_save()
        if self.auto_export_energy and self.device_energy_dashboard_ready(entity_id):
            self._schedule_energy_dashboard_sync()

    def _update_latent_climate_activity(self, now: float) -> None:
        """Nominate climate activity edges from residual change-points.

        This is an *activity adapter*, not a separate power model. Both central
        HVAC and mini-splits ultimately use the same session baseline, overlap
        compensation, plausibility checks, and isolated-energy integrator. A
        climate entity may use hvac_action edges, residual edges, or both.
        """
        start_candidates: list[tuple[str, str, float, float, float, float]] = []

        for entity_id in self.models:
            if entity_id.split(".", 1)[0] != "climate":
                continue
            if self._manual_fixed_locked(entity_id):
                continue
            state = self.hass.states.get(entity_id)
            session = self.isolation_sessions[entity_id]
            if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                continue
            if session.pending_direction:
                continue

            threshold = self._latent_climate_threshold(entity_id)

            # Thermostat state/HVAC-action OFF is a cue, not proof that the
            # electrical load has vanished. Heat pumps commonly run a compressor
            # or blower briefly after the control state changes. Give the residual
            # a bounded interval to settle before rejecting the session.
            if state.state == STATE_OFF and session.active:
                if session.baseline_w is None or not session.start_edge_accepted:
                    # No validated electrical start exists, so there is nothing
                    # trustworthy to preserve or pair with a later stop.
                    session.active = False
                    session.channel = None
                    session.validation_channel = None
                    session.pending_energy_kwh = 0.0
                    session.baseline_w = None
                    session.reported_power_w = 0.0
                    session.raw_power_w = 0.0
                    session.status = "off"
                    session.quality = "off"
                    session.started_ts = None
                    session.start_edge_w = None
                    session.start_edge_accepted = False
                    session.climate_off_seen_since = None
                    session.latent_start_candidate_since = None
                    session.latent_stop_candidate_since = None
                    session.latent_stop_edge_w = None
                    session.clear_feature_capture()
                    continue
                if session.climate_off_seen_since is None:
                    session.climate_off_seen_since = now
                    session.status = "settling_after_climate_off"
                    session.quality = "awaiting_electrical_stop"
                    session.last_edge_source = "climate_state_off_wait"

            # A climate session that has received an OFF cue remains active only
            # while we wait for the electrical load to return to baseline. Once
            # near baseline, require the normal confirmation interval; if it never
            # settles, terminate after the bounded grace window.
            if session.active and session.climate_off_seen_since is not None and session.baseline_w is not None:
                raw = max(0.0, (self.last_residual_w or session.baseline_w) - session.baseline_w)
                stop_threshold = max(35.0, threshold * 0.25)
                if raw <= stop_threshold:
                    if session.latent_stop_candidate_since is None:
                        session.latent_stop_candidate_since = now
                        pre_drop = session.recent_power_w[:-1][-15:] if len(session.recent_power_w) > 1 else []
                        session.latent_stop_edge_w = robust_median(pre_drop) or session.start_edge_w
                    elif now - session.latent_stop_candidate_since >= self.latent_climate_confirm_seconds:
                        self._finish_latent_climate_session(
                            entity_id,
                            now,
                            record_observation=(session.activity_source == "residual_change_point"),
                            edge_source="climate_off_settled",
                        )
                else:
                    session.latent_stop_candidate_since = None
                    session.latent_stop_edge_w = None
                    if now - session.climate_off_seen_since >= CLIMATE_ELECTRICAL_STOP_GRACE_SECONDS:
                        relative_stop = self._find_climate_relative_stop(entity_id, now)
                        if relative_stop is not None:
                            session.latent_stop_candidate_since = relative_stop["edge_ts"]
                            session.latent_stop_edge_w = relative_stop["edge_w"]
                            self._finish_latent_climate_session(
                                entity_id,
                                now,
                                record_observation=(session.activity_source == "residual_change_point"),
                                edge_source="climate_off_relative_stop",
                            )
                        else:
                            self._abort_climate_session(
                                entity_id,
                                now,
                                "climate_electrical_stop_timeout",
                            )
                continue

            # Any climate session whose isolated electrical draw remains near zero
            # long enough may close from a residual edge. This handles devices
            # whose hvac_action remains "cooling" for an entire thermostat mode
            # even though the compressor itself cycles.
            if session.active and session.baseline_w is not None:
                raw = max(0.0, (self.last_residual_w or session.baseline_w) - session.baseline_w)
                if raw <= max(35.0, threshold * 0.25):
                    if session.latent_stop_candidate_since is None:
                        session.latent_stop_candidate_since = now
                        pre_drop = session.recent_power_w[:-1][-15:] if len(session.recent_power_w) > 1 else []
                        session.latent_stop_edge_w = robust_median(pre_drop) or session.start_edge_w
                    elif now - session.latent_stop_candidate_since >= self.latent_climate_confirm_seconds:
                        self._finish_latent_climate_session(entity_id, now)
                else:
                    session.latent_stop_candidate_since = None
                    session.latent_stop_edge_w = None
                continue

            # Give a just-seen explicit hvac_action edge time to finish its robust
            # pre/post evaluation before trying a residual fallback.
            last_explicit = self._last_explicit_activity_edge.get(entity_id)
            if last_explicit is not None and now - last_explicit < 14.0:
                continue

            # A residual change-point is only useful as a *fallback* activity edge
            # when no known tracked device just changed. Otherwise a perfectly
            # ordinary switch/light transition could be misread as an HVAC start.
            # This does not create a second inference model: it is simply the same
            # concurrency rule applied to the generic climate activity adapter.
            if any(
                other_eid != entity_id and 0.0 <= now - ts <= ISOLATION_CONCURRENCY_SECONDS
                for ts, other_eid in self._transition_log
            ):
                session.latent_start_candidate_since = None
                continue

            if not self._climate_demand_plausible(state):
                session.latent_start_candidate_since = None
                continue

            fast = self._window_values(now - 10.0, now, corrected=True)
            prior = self._window_values(now - 55.0, now - 20.0, corrected=True)
            if len(fast) < 3 or len(prior) < 3:
                continue
            fast_med = robust_median(fast)
            prior_med = robust_median(prior)
            if fast_med is None or prior_med is None:
                continue
            noise = max(mad(fast), mad(prior))
            if noise > max(self.max_noise_w * 1.5, 45.0):
                session.latent_start_candidate_since = None
                continue
            step = fast_med - prior_med
            if step < threshold or step > self._maximum_plausible_w(entity_id):
                session.latent_start_candidate_since = None
                continue

            if session.latent_start_candidate_since is None:
                session.latent_start_candidate_since = now
                continue
            if now - session.latent_start_candidate_since < self.latent_climate_confirm_seconds:
                continue

            explicit_channel = self._activity_channel(entity_id, state)
            channel = explicit_channel or f"inferred_{state.state}"
            start_candidates.append((entity_id, channel, step, prior_med, fast_med, noise))

        if not start_candidates:
            return

        # A whole-house residual edge cannot safely be assigned to two idle HVAC
        # systems at once. Refuse attribution until the candidate set becomes
        # unambiguous rather than double-counting the same compressor step.
        if len(start_candidates) > 1:
            for entity_id, _channel, _step, _prior, _fast, _noise in start_candidates:
                session = self.isolation_sessions[entity_id]
                session.status = "ambiguous_activity"
                session.quality = "ambiguous_climate_change_point"
            return

        entity_id, channel, step, prior_med, fast_med, noise = start_candidates[0]
        session = self.isolation_sessions[entity_id]
        self._shift_active_session_baselines(step, except_entity=entity_id, reason="latent_climate_start")
        session.active = True
        session.channel = channel
        session.validation_channel = self._validation_channel(channel)
        session.pending_energy_kwh = 0.0
        session.baseline_w = prior_med
        session.started_ts = session.latent_start_candidate_since or now
        session.start_edge_w = step
        session.start_edge_accepted = True
        session.climate_off_seen_since = None
        session.baseline_adjustments = 0
        session.edge_symmetry_error_w = None
        session.overlap_seen = any(
            other.active for other_eid, other in self.isolation_sessions.items()
            if other_eid != entity_id
        )
        session.reset_feature_capture(
            session.started_ts,
            started_utc=self._utc_for_monotonic(session.started_ts),
            activity_source="residual_change_point",
            channel=channel,
            old_level=0.0,
            new_level=1.0,
        )
        for sample in self._sample_buffer:
            if session.started_ts <= sample.ts <= now and sample.valid_for_learning:
                session.add_power_sample(max(0.0, sample.residual_w - prior_med), sample.ts)
        session.raw_power_w = max(0.0, (self.last_residual_w or fast_med) - prior_med)
        session.reported_power_w = session.raw_power_w
        session.status = "tracking"
        session.quality = "good_change_point"
        session.sanity_status = "plausible"
        session.last_edge_w = step
        session.last_edge_direction = "up"
        session.last_edge_source = "residual_change_point"
        session.edge_symmetry_status = "awaiting_off_edge"
        session.latent_start_candidate_since = None
        session.latent_stop_candidate_since = None
        session.latent_stop_edge_w = None
        # Do not persist the UP change-point yet. It becomes accepted evidence
        # only if this latent HVAC session later completes and validates.
        self._schedule_save()

    def _climate_demand_plausible(self, state: State) -> bool:
        """Use HVAC mode and temperatures as a permissive start constraint."""
        mode = state.state
        if mode not in ACTIVE_HVAC_MODES:
            return False
        try:
            current = float(state.attributes.get("current_temperature"))
            target = float(state.attributes.get("temperature"))
        except (TypeError, ValueError):
            return True
        if mode == "cool":
            return current >= target - 0.3
        if mode == "heat":
            return current <= target + 0.3
        return True

    def _window_values(self, start: float, end: float, *, corrected: bool) -> list[float]:
        """Extract values from rolling sample buffer."""
        return [
            s.corrected_w if corrected else s.residual_w
            for s in self._sample_buffer
            if start <= s.ts <= end and s.valid_for_learning
        ]

    def _energy_guarded_power(self, entity_id: str, power: float | None, now: float) -> float | None:
        """Protect cumulative energy from isolated upward spikes.

        Legitimate starts/level changes are exempt when they coincide with a tracked
        transition.  Downward changes are accepted immediately.  Only a surprising
        upward point during an otherwise steady run is replaced by the recent robust
        median, and the raw public power sensor remains untouched.
        """
        if power is None or not math.isfinite(float(power)):
            history = self._energy_power_history.get(entity_id)
            if history is not None:
                history.clear()
            self._energy_last_guarded_power_w[entity_id] = None
            return None
        value = max(0.0, float(power))
        if entity_id.split(".", 1)[0] == "climate":
            self._energy_last_filter_reason[entity_id] = None
            self._energy_last_guarded_power_w[entity_id] = value
            return value
        history = self._energy_power_history.setdefault(
            entity_id, deque(maxlen=ENERGY_OUTLIER_HISTORY_POINTS)
        )
        session = self.isolation_sessions.get(entity_id)
        recent_transition = any(
            eid == entity_id and now - ts <= max(12.0, float(self.concurrency_seconds))
            for ts, eid in self._transition_log
        )
        profile = self.device_profiles.get(entity_id)
        if profile and profile.powercalc_active:
            history.append(value)
            self._energy_last_filter_reason[entity_id] = None
            self._energy_last_guarded_power_w[entity_id] = value
            return value
        if self.calibrating_entity == entity_id or recent_transition or (session and session.pending_direction):
            # A real commanded/state transition starts a new operating plateau; do
            # not compare it with the previous OFF/level history.
            history.clear()
            history.append(value)
            self._energy_last_filter_reason[entity_id] = None
            self._energy_last_guarded_power_w[entity_id] = value
            return value
        if len(history) < ENERGY_OUTLIER_MIN_HISTORY:
            history.append(value)
            self._energy_last_filter_reason[entity_id] = None
            self._energy_last_guarded_power_w[entity_id] = value
            return value

        center = robust_median(list(history))
        if center is None:
            history.append(value)
            self._energy_last_guarded_power_w[entity_id] = value
            return value
        spread = 1.4826 * mad(list(history), center)
        threshold = max(
            ENERGY_OUTLIER_MIN_ABSOLUTE_W,
            float(center) * ENERGY_OUTLIER_RELATIVE_RISE,
            ENERGY_OUTLIER_SIGMA * max(1.0, spread),
        )
        # Deliberately asymmetric: immediate downward changes are harmless for
        # cumulative energy, while a one-sample upward spike can overcount forever.
        if value > float(center) + threshold:
            self._energy_outlier_filtered_count[entity_id] += 1
            self._energy_last_filter_reason[entity_id] = (
                f"upward_spike:{value:.1f}W_vs_{float(center):.1f}W"
            )
            guarded = max(0.0, float(center))
            self._energy_last_guarded_power_w[entity_id] = guarded
            return guarded

        history.append(value)
        self._energy_last_filter_reason[entity_id] = None
        self._energy_last_guarded_power_w[entity_id] = value
        return value

    def _integrate_energy(self, now: float, raw_isolated_current: dict[str, float | None] | None = None) -> None:
        """Integrate one unified runtime power signal into canonical kWh.

        ``isolated_energy_kwh`` is the canonical 0.4 total. ``energy_kwh`` is a
        legacy compatibility accumulator: its pre-0.4 value is preserved, but
        from this release forward it receives the *same* trusted unified-energy
        increment. No fixed-wattage runtime estimator remains active.
        """
        if raw_isolated_current is None:
            raw_isolated_current = {eid: self.device_isolated_power_w(eid) for eid in self.models}
        isolated_current = {
            eid: self._energy_guarded_power(eid, power, now)
            for eid, power in raw_isolated_current.items()
        }
        if self._last_energy_ts is None:
            self._last_energy_ts = now
            self._last_isolated_powers = isolated_current
            return
        dt = max(0.0, min(300.0, now - self._last_energy_ts))
        if dt <= 0:
            return
        for eid, power in isolated_current.items():
            profile = self.device_profiles.get(eid)
            if profile and profile.powercalc_active and not self._manual_fixed_locked(eid):
                mirrored = self._powercalc_energy_value(eid)
                if mirrored is not None:
                    self.models[eid].isolated_energy_kwh = max(
                        self.models[eid].isolated_energy_kwh, mirrored
                    )
                    self.models[eid].energy_kwh = max(self.models[eid].energy_kwh, mirrored)
                continue
            prev = self._last_isolated_powers.get(eid)
            if power is not None and prev is not None:
                delta_kwh = ((prev + power) / 2.0) * dt / 3_600_000.0
                session = self.isolation_sessions.get(eid)
                is_climate = eid.split(".", 1)[0] == "climate"
                if is_climate and session and not self._channel_is_validated(eid, session.validation_channel or session.channel):
                    # First encounter with a climate operating channel is escrowed.
                    # The kWh becomes canonical only if the complete session validates.
                    session.pending_energy_kwh += delta_kwh
                else:
                    self.models[eid].isolated_energy_kwh += delta_kwh
                    # Preserve monotonicity of already-existing legacy energy entities
                    # while ensuring they no longer represent a second inference model.
                    self.models[eid].energy_kwh += delta_kwh
        self._last_energy_ts = now
        self._last_isolated_powers = isolated_current
        if now - self._last_periodic_save_ts >= 30.0:
            self._last_periodic_save_ts = now
            self._schedule_save()

    def device_power_w(self, entity_id: str) -> float:
        """Compatibility alias: legacy estimated-power uses unified live power."""
        return self.device_isolated_power_w(entity_id) or 0.0

    def device_energy_kwh(self, entity_id: str) -> float:
        """Compatibility cumulative total continuing from the old legacy offset."""
        model = self.models.get(entity_id)
        return model.energy_kwh if model else 0.0

    def _entity_runtime_level(self, entity_id: str) -> float | None:
        """Return the source entity's normalized 0..1 operating level."""
        state = self.hass.states.get(entity_id)
        if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        domain = entity_id.split(".", 1)[0]
        if state.state == STATE_OFF or (domain == "media_player" and state.state in OFFISH_MEDIA_STATES):
            return 0.0
        if domain == "light":
            brightness = state.attributes.get("brightness")
            if brightness is not None:
                with suppress(TypeError, ValueError):
                    return max(0.0, min(1.0, float(brightness) / 255.0))
            return 1.0 if state.state == STATE_ON else 0.0
        if domain == "fan":
            percentage = state.attributes.get("percentage")
            if percentage is not None:
                with suppress(TypeError, ValueError):
                    return max(0.0, min(1.0, float(percentage) / 100.0))
            return 1.0 if state.state == STATE_ON else 0.0
        return 1.0 if state.state not in (STATE_OFF, STATE_UNKNOWN, STATE_UNAVAILABLE) else 0.0

    def _mature_profile_runtime_spec(self, entity_id: str) -> dict[str, Any] | None:
        """Return a stable fixed/linear model suitable for deterministic fallback."""
        profile = self.device_profiles.get(entity_id)
        if not profile or profile.powercalc_active:
            return None
        # Never mask the electrical edge we are actively trying to measure.
        session = self.isolation_sessions.get(entity_id)
        if self.calibrating_entity == entity_id or (session and session.pending_direction):
            return None

        if profile.powercalc_exported and profile.powercalc_profile_type:
            if profile.powercalc_profile_type == "fixed" and profile.powercalc_power_w is not None:
                return {"profile_type": "fixed", "power_w": float(profile.powercalc_power_w), "calibration_points": []}
            if profile.powercalc_profile_type == "linear" and profile.powercalc_calibration_points:
                return {
                    "profile_type": "linear",
                    "power_w": profile.powercalc_power_w,
                    "calibration_points": [list(point) for point in profile.powercalc_calibration_points],
                }

        if not self.device_energy_dashboard_ready(entity_id):
            return None
        assessment = self.powercalc_profile_assessment(entity_id)
        if not assessment.get("eligible"):
            return None
        return assessment

    @staticmethod
    def _interpolate_profile_points(level: float, points: list[list[float]]) -> float | None:
        """Linearly interpolate normalized Powercalc calibration points."""
        clean = sorted(
            (max(0.0, min(1.0, float(point[0]))), max(0.0, float(point[1])))
            for point in points
            if isinstance(point, (list, tuple)) and len(point) >= 2
        )
        if not clean:
            return None
        if level <= 0:
            return 0.0
        if level <= clean[0][0]:
            first_level, first_w = clean[0]
            return first_w * (level / first_level) if first_level > 0 else first_w
        for (lo_level, lo_w), (hi_level, hi_w) in zip(clean, clean[1:], strict=False):
            if level <= hi_level:
                if hi_level <= lo_level:
                    return hi_w
                frac = (level - lo_level) / (hi_level - lo_level)
                return lo_w + frac * (hi_w - lo_w)
        last_level, last_w = clean[-1]
        if last_level <= 0:
            return last_w
        return last_w * min(1.25, level / last_level)

    def _mature_profile_fallback_w(self, entity_id: str) -> float | None:
        """Estimate runtime watts from a validated model when live isolation is ambiguous."""
        # OFF devices are by far the common case. Avoid constructing/scoring a
        # mature profile only to return zero.
        level = self._entity_runtime_level(entity_id)
        if level is None:
            self._runtime_fallback_last_w[entity_id] = None
            return None
        if level <= 0:
            self._runtime_fallback_last_w[entity_id] = None
            return 0.0
        spec = self._mature_profile_runtime_spec(entity_id)
        if not spec:
            self._runtime_fallback_last_w[entity_id] = None
            return None
        if spec.get("profile_type") == "linear":
            value = self._interpolate_profile_points(level, spec.get("calibration_points") or [])
        else:
            power_w = spec.get("power_w")
            value = float(power_w) if power_w is not None else None
        if value is not None:
            value = max(0.0, float(value))
        self._runtime_fallback_last_w[entity_id] = value
        return value

    def device_isolated_power_w(self, entity_id: str) -> float | None:
        """Public power estimate with explicit manual fixed-power behavior.

        Locked values are authoritative and stop learning. Placeholder values are
        used only when live or mature learned evidence is unavailable, so they
        automatically yield to better data without losing the user's fallback.
        """
        profile = self.device_profiles.get(entity_id)
        manual_mode = self._manual_fixed_mode(entity_id)
        manual_runtime = self._manual_fixed_runtime_w(entity_id)
        if manual_mode == MANUAL_FIXED_MODE_LOCKED and manual_runtime is not None:
            self._runtime_fallback_last_w[entity_id] = None
            return manual_runtime
        if profile and profile.powercalc_active:
            mirrored = self._powercalc_power_value(entity_id)
            if mirrored is not None:
                return mirrored
        session = self.isolation_sessions.get(entity_id)
        if session and session.active and session.reported_power_w is not None:
            self._runtime_fallback_last_w[entity_id] = None
            return session.reported_power_w
        fallback = self._mature_profile_fallback_w(entity_id)
        if fallback is not None:
            return fallback
        if manual_mode == MANUAL_FIXED_MODE_PLACEHOLDER and manual_runtime is not None:
            self._runtime_fallback_last_w[entity_id] = None
            return manual_runtime
        if not session or not session.active:
            return 0.0
        return session.reported_power_w

    def device_isolated_energy_kwh(self, entity_id: str) -> float:
        """Public canonical energy; keep the PF entity stable across Powercalc handoff."""
        profile = self.device_profiles.get(entity_id)
        if profile and profile.powercalc_active:
            mirrored = self._powercalc_energy_value(entity_id)
            if mirrored is not None:
                return mirrored
        model = self.models.get(entity_id)
        return model.isolated_energy_kwh if model else 0.0

    def device_runtime_attributes(self, entity_id: str) -> dict[str, Any]:
        """Return compact recorder-friendly attributes for public device sensors."""
        session = self.isolation_sessions.get(entity_id)
        model = self.models.get(entity_id)
        profile = self.device_profiles.get(entity_id)
        if not session or not model:
            return {}
        legacy = self.device_attributes(entity_id)
        readiness = self.energy_dashboard_readiness_assessment(entity_id)
        return {
            "model_version": VERSION,
            "sample_count": legacy.get("sample_count", 0),
            "sanity_prior_usable": bool(
                int(legacy.get("sample_count", 0) or 0) >= 3
                and legacy.get("learned_full_power_w") is not None
                and float(legacy.get("learned_full_power_w") or 0.0) <= session.prior_high_w
            ),
            "learned_full_power_w": legacy.get("learned_full_power_w"),
            "confidence": legacy.get("confidence", "learning"),
            "confidence_score": legacy.get("confidence_score", 0),
            "status": session.status,
            "quality": session.quality,
            "validation_channel": session.validation_channel or self._validation_channel(session.channel),
            "channel_validation_status": (
                "validated" if self._channel_is_validated(entity_id, session.validation_channel or session.channel)
                else ("provisional" if entity_id.split(".", 1)[0] == "climate" and session.channel else "not_applicable")
            ),
            "expected_range_w": [session.prior_low_w, session.prior_high_w],
            "validated_sessions": model.validated_sessions,
            "historical_validated_cycles": model.historical_validated_cycles,
            "rejected_sessions": model.rejected_sessions,
            # Keep the old field as a compatibility alias, but make its meaning
            # explicit for new cards and diagnostics.
            "readiness_credit": readiness["historical_credit"],
            "historical_readiness_credit": readiness["historical_credit"],
            "readiness_evidence_total": readiness["evidence_total"],
            "readiness_required_evidence": DEFAULT_READY_VALIDATED_SESSIONS,
            "readiness_profile_quality": readiness["profile_quality_ready"],
            "readiness_reasons": readiness["reasons"],
            "learned_profile_ready": readiness["learned_ready"],
            "learning_readiness_reasons": readiness["learning_reasons"],
            "weighted_soft_evidence": readiness.get("weighted_soft_evidence", 0.0),
            "soft_evidence_power_w": readiness.get("soft_evidence_power_w"),
            "soft_evidence_ci_low_w": readiness.get("soft_evidence_ci_low_w"),
            "soft_evidence_ci_high_w": readiness.get("soft_evidence_ci_high_w"),
            "energy_dashboard_ready": readiness["ready"],
            "energy_dashboard_exported": self.device_energy_dashboard_exported(entity_id),
            "profile_lifecycle": profile.lifecycle_state if profile else "learning",
            "manual_fixed_mode": self._manual_fixed_mode(entity_id),
            "manual_fixed_power_w": self._manual_fixed_value_w(entity_id),
            "manual_fixed_suggested_power_w": self.fixed_power_suggestion(entity_id).get("power_w"),
            "manual_fixed_suggestion_source": self.fixed_power_suggestion(entity_id).get("source"),
            "manual_fixed_monitoring_for_improvement": not self._manual_fixed_locked(entity_id),
            "runtime_provider": (
                "manual_fixed_locked"
                if self._manual_fixed_locked(entity_id)
                else (
                    "manual_fixed_placeholder"
                    if self._manual_fixed_mode(entity_id) == MANUAL_FIXED_MODE_PLACEHOLDER
                    and self._manual_fixed_runtime_w(entity_id) is not None
                    and not (session and session.active and session.reported_power_w is not None)
                    and self._mature_profile_fallback_w(entity_id) is None
                    else ("powercalc" if profile and profile.powercalc_active else "power_fingerprint")
                )
            ),
            "powercalc_profile_type": profile.powercalc_profile_type if profile else None,
            "powercalc_handoff_status": profile.powercalc_handoff_status if profile else "none",
            "powercalc_power_sensor": profile.powercalc_power_sensor_id if profile else None,
            "powercalc_energy_sensor": profile.powercalc_energy_sensor_id if profile else None,
            "audit_observations": profile.audit_observations if profile else 0,
            "audit_good": profile.audit_good if profile else 0,
            "audit_bad": profile.audit_bad if profile else 0,
            "audit_last_error_pct": profile.audit_last_error_pct if profile else None,
            "rich_session_count": readiness["rich_session_count"],
            "runtime_profile_fallback_active": self._runtime_fallback_last_w.get(entity_id) is not None,
            "energy_outlier_filtered_count": int(self._energy_outlier_filtered_count.get(entity_id, 0)),
            "active_calibration_allowed": entity_id not in self.calibration_excluded_entities,
        }

    def _sanity_prior_usable(self, entity_id: str, legacy: dict[str, Any] | None = None) -> bool:
        """Share the exact prior test between device attributes and the summary."""
        session = self.isolation_sessions.get(entity_id)
        model = self.models.get(entity_id)
        if not session or not model:
            return False
        if legacy is None:
            if entity_id.split(".", 1)[0] == "climate":
                legacy = self.device_attributes(entity_id)
            else:
                channel = model.channels.get("level")
                legacy = {
                    "sample_count": channel.sample_count if channel else 0,
                    "learned_full_power_w": channel.estimate_w if channel else None,
                }
        return bool(
            int(legacy.get("sample_count", 0) or 0) >= 3
            and legacy.get("learned_full_power_w") is not None
            and float(legacy.get("learned_full_power_w") or 0.0) <= session.prior_high_w
        )

    def _live_isolation_summary(self, ready_devices: list[str]) -> dict[str, Any]:
        """Count only configured devices once; never enumerate HA's sensor domain."""
        active_statuses = {"tracking", "tracking_degraded", "measuring_on_edge", "measuring_level_edge"}
        blockers: Counter[str] = Counter()
        learning_blockers: Counter[str] = Counter()
        unavailable: list[str] = []
        active_count = 0
        prior_count = 0
        exported_count = 0
        learned_ready_count = 0
        placeholder_count = 0
        locked_count = 0
        for eid in self.tracked_entities:
            session = self.isolation_sessions.get(eid)
            active_count += int(bool(session and session.status in active_statuses))
            prior_count += int(self._sanity_prior_usable(eid))
            exported_count += int(self.device_energy_dashboard_exported(eid))
            state = self.hass.states.get(eid)
            if not state or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                unavailable.append(eid)
            readiness = self.energy_dashboard_readiness_assessment(eid)
            blockers.update(readiness["reasons"])
            learning_blockers.update(readiness["learning_reasons"])
            learned_ready_count += int(readiness["learned_ready"])
            fixed_active = self._manual_fixed_active(eid)
            mode = self._manual_fixed_mode(eid)
            placeholder_count += int(fixed_active and mode == MANUAL_FIXED_MODE_PLACEHOLDER)
            locked_count += int(fixed_active and mode == MANUAL_FIXED_MODE_LOCKED)
        return {
            "total_count": len(self.tracked_entities),
            "ready_count": len(ready_devices),
            "learned_ready_count": learned_ready_count,
            "not_yet_learned_count": len(self.tracked_entities) - learned_ready_count,
            "placeholder_count": placeholder_count,
            "locked_count": locked_count,
            "exported_count": exported_count,
            "active_count": active_count,
            "prior_count": prior_count,
            "unavailable_count": len(unavailable),
            "unavailable_entities": unavailable,
            "learning_enabled": self.learning_enabled,
            "source_available": self.last_source_w is not None,
            "blocker_counts": dict(sorted(blockers.items())),
            "learning_blocker_counts": dict(sorted(learning_blockers.items())),
        }

    def device_isolation_attributes(self, entity_id: str) -> dict[str, Any]:
        """Diagnostic attributes for the unified live-isolation engine."""
        session = self.isolation_sessions.get(entity_id)
        model = self.models.get(entity_id)
        if not session or not model:
            return {}
        now = time.monotonic()
        legacy = self.device_attributes(entity_id)
        prior_usable = self._sanity_prior_usable(entity_id, legacy)
        readiness = self.energy_dashboard_readiness_assessment(entity_id)
        canonical_energy = model.isolated_energy_kwh
        migrated_energy = min(model.migration_energy_kwh, canonical_energy)
        return {
            "method": (
                "powercalc_profile_mirror_with_passive_audit"
                if (self.device_profiles.get(entity_id) and self.device_profiles[entity_id].powercalc_active)
                else (
                    "unified_live_session_isolation_with_profile_fallback"
                    if self._runtime_fallback_last_w.get(entity_id) is not None
                    else "unified_live_session_isolation"
                )
            ),
            "model_version": VERSION,
            "sample_count": legacy.get("sample_count", 0),
            "sanity_prior_usable": prior_usable,
            "learned_full_power_w": legacy.get("learned_full_power_w"),
            "learned_channel": legacy.get("learned_channel"),
            "confidence": legacy.get("confidence", "learning"),
            "confidence_score": legacy.get("confidence_score", 0),
            "status": session.status,
            "quality": session.quality,
            "activity_channel": session.channel,
            "validation_channel": session.validation_channel or self._validation_channel(session.channel),
            "channel_validation_status": (
                "validated" if self._channel_is_validated(entity_id, session.validation_channel or session.channel)
                else ("provisional" if entity_id.split(".", 1)[0] == "climate" and session.channel else "not_applicable")
            ),
            "validated_channel_sessions": dict(model.validated_channel_sessions),
            "rejected_channel_sessions": dict(model.rejected_channel_sessions),
            "historical_channel_validations": dict(model.historical_channel_validations),
            "pending_channel_energy_kwh": session.pending_energy_kwh,
            "activity_edge_source": session.last_edge_source,
            "raw_isolated_power_w": session.raw_power_w,
            "runtime_profile_fallback_w": self._runtime_fallback_last_w.get(entity_id),
            "runtime_profile_fallback_active": self._runtime_fallback_last_w.get(entity_id) is not None,
            "energy_integrator_guarded_power_w": self._energy_last_guarded_power_w.get(entity_id),
            "energy_outlier_filtered_count": int(self._energy_outlier_filtered_count.get(entity_id, 0)),
            "energy_outlier_last_filter_reason": self._energy_last_filter_reason.get(entity_id),
            "baseline_w": session.baseline_w,
            "session_age_seconds": (
                max(0.0, now - session.started_ts)
                if session.active and session.started_ts is not None else 0.0
            ),
            "start_edge_w": session.start_edge_w,
            "start_edge_accepted": session.start_edge_accepted,
            "last_edge_w": session.last_edge_w,
            "last_edge_direction": session.last_edge_direction,
            "edge_symmetry_status": session.edge_symmetry_status,
            "edge_symmetry_error_w": session.edge_symmetry_error_w,
            "sanity_status": session.sanity_status,
            "expected_range_w": [session.prior_low_w, session.prior_high_w],
            "prior_source": session.prior_source,
            "baseline_adjustments": session.baseline_adjustments,
            "simultaneously_active_tracked_devices": sum(
                1 for x in self.isolation_sessions.values() if x.active
            ),
            "validated_sessions": model.validated_sessions,
            "rejected_sessions": model.rejected_sessions,
            "last_session_rejection_reason": model.last_session_rejection_reason,
            "last_session_rejection_channel": model.last_session_rejection_channel,
            "last_session_rejection_at": model.last_session_rejection_at,
            "historical_validated_cycles": model.historical_validated_cycles,
            "readiness_credit": readiness["historical_credit"],
            "historical_readiness_credit": readiness["historical_credit"],
            "readiness_evidence_total": readiness["evidence_total"],
            "readiness_required_evidence": DEFAULT_READY_VALIDATED_SESSIONS,
            "readiness_profile_quality": readiness["profile_quality_ready"],
            "readiness_reasons": readiness["reasons"],
            "learned_profile_ready": readiness["learned_ready"],
            "learning_readiness_reasons": readiness["learning_reasons"],
            "energy_dashboard_ready": readiness["ready"],
            "energy_dashboard_exported": self.device_energy_dashboard_exported(entity_id),
            "auto_export_energy_dashboard": self.auto_export_energy,
            "canonical_energy_kwh": canonical_energy,
            "migration_energy_kwh": migrated_energy,
            "unified_energy_since_migration_kwh": max(0.0, canonical_energy - migrated_energy),
            "migration_source": model.migration_source,
            "last_completed_session_seconds": session.last_completed_duration_s,
            "profile_lifecycle": (self.device_profiles.get(entity_id).lifecycle_state if self.device_profiles.get(entity_id) else "learning"),
            "runtime_provider": ("powercalc" if self.device_profiles.get(entity_id) and self.device_profiles[entity_id].powercalc_active else "power_fingerprint"),
            "powercalc_profile_type": (self.device_profiles.get(entity_id).powercalc_profile_type if self.device_profiles.get(entity_id) else None),
            "powercalc_handoff_status": (self.device_profiles.get(entity_id).powercalc_handoff_status if self.device_profiles.get(entity_id) else "none"),
            "powercalc_power_sensor": (self.device_profiles.get(entity_id).powercalc_power_sensor_id if self.device_profiles.get(entity_id) else None),
            "powercalc_energy_sensor": (self.device_profiles.get(entity_id).powercalc_energy_sensor_id if self.device_profiles.get(entity_id) else None),
            "audit_observations": (self.device_profiles.get(entity_id).audit_observations if self.device_profiles.get(entity_id) else 0),
            "audit_good": (self.device_profiles.get(entity_id).audit_good if self.device_profiles.get(entity_id) else 0),
            "audit_bad": (self.device_profiles.get(entity_id).audit_bad if self.device_profiles.get(entity_id) else 0),
            "audit_last_error_pct": (self.device_profiles.get(entity_id).audit_last_error_pct if self.device_profiles.get(entity_id) else None),
            "climate_off_grace_seconds": (
                CLIMATE_ELECTRICAL_STOP_GRACE_SECONDS
                if entity_id.split(".", 1)[0] == "climate"
                else None
            ),
            "climate_off_grace_remaining_seconds": (
                max(
                    0.0,
                    CLIMATE_ELECTRICAL_STOP_GRACE_SECONDS
                    - (now - session.climate_off_seen_since),
                )
                if session.climate_off_seen_since is not None
                else None
            ),
            "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
            "rich_session_count": sum(len(values) for values in model.session_fingerprints.values()),
            "fingerprint_profiles": model.fingerprint_summaries(),
            "latest_session_fingerprint": (
                latest.compact_dict() if (latest := model.latest_session_fingerprint()) else None
            ),
            "legacy_prior": legacy,
        }

    def _climate_channel_summary(self, key: str, channel) -> dict[str, Any]:
        """Summarize climate evidence while ignoring sub-threshold control-board edges."""
        floor = self._climate_minimum_edge_w(key)
        accepted = [
            obs for obs in channel.observations
            if obs.get("accepted") and float(obs.get("value_w", 0.0) or 0.0) >= floor
        ]
        values = [float(obs.get("value_w", 0.0) or 0.0) for obs in accepted]
        directions = {"up": 0, "down": 0}
        for obs in accepted:
            direction = str(obs.get("direction", ""))
            if direction in directions:
                directions[direction] += 1
        n = len(values)
        center = robust_median(values)
        deviation = (1.4826 * mad(values, center)) if center is not None else None
        if n < self.min_samples:
            label, score = "learning", min(20, int(20 * n / max(1, self.min_samples)))
        else:
            rel = (deviation / center) if center and deviation is not None else 1.0
            both = directions["up"] > 0 and directions["down"] > 0
            if n >= 12 and rel <= 0.15 and both:
                label, score = "high", min(100, 80 + min(20, n - 12))
            elif n >= 6 and rel <= 0.30 and both:
                label, score = "medium", min(79, 55 + min(24, n - 6) * 2)
            else:
                label, score = "low", min(54, 25 + n * 3)
        return {
            "sample_count": n,
            "learned_power_w": center,
            "deviation_w": deviation,
            "confidence": label,
            "confidence_score": score,
            "direction_counts": directions,
            "ignored_subthreshold_samples": max(0, channel.sample_count - n),
        }

    def device_attributes(self, entity_id: str) -> dict[str, Any]:
        """Diagnostic attributes for a device model."""
        model = self.models.get(entity_id)
        if not model:
            return {}
        state = self.hass.states.get(entity_id)
        domain = entity_id.split(".", 1)[0]

        if domain == "climate":
            active_channel = str(state.attributes.get("hvac_action", "idle")) if state else "idle"
            channel_details: dict[str, dict[str, Any]] = {
                key: self._climate_channel_summary(key, channel)
                for key, channel in model.channels.items()
            }

            learned_channels = [
                (key, details)
                for key, details in channel_details.items()
                if int(details.get("sample_count", 0) or 0) > 0
            ]
            representative_key: str | None = None
            representative: dict[str, Any] | None = None
            active_details = channel_details.get(active_channel)
            if active_details and int(active_details.get("sample_count", 0) or 0) > 0:
                representative_key = active_channel
                representative = active_details
            elif learned_channels:
                representative_key, representative = max(
                    learned_channels,
                    key=lambda item: (
                        int(item[1].get("sample_count", 0) or 0),
                        int(item[1].get("confidence_score", 0) or 0),
                    ),
                )

            total_samples = sum(int(d.get("sample_count", 0) or 0) for d in channel_details.values())
            if representative is None and model.legacy_cycle_values_w:
                cycles = model.legacy_cycle_values_w
                center = robust_median(cycles)
                deviation = (1.4826 * mad(cycles, center)) if center is not None else None
                n = len(cycles)
                rel = (deviation / center) if center and deviation is not None else 1.0
                if n >= 8 and rel <= 0.35:
                    label, score = "high", min(100, 80 + n)
                elif n >= 4 and rel <= 0.55:
                    label, score = "medium", min(79, 55 + n * 3)
                else:
                    label, score = "low", min(54, 30 + n * 2)
                channel_details["legacy_compressor"] = {
                    "sample_count": n,
                    "learned_power_w": center,
                    "deviation_w": deviation,
                    "confidence": label,
                    "confidence_score": score,
                    "direction_counts": {},
                    "source": "0.3_inverter_change_point",
                    "ignored_subthreshold_samples": 0,
                }
                return {
                    "model": "historical_change_point_prior",
                    "confidence": label,
                    "confidence_score": score,
                    "sample_count": n,
                    "learned_full_power_w": center,
                    "deviation_w": deviation,
                    "active_channel": active_channel,
                    "learned_channel": "legacy_compressor",
                    "channels": channel_details,
                }
            if representative is None:
                return {
                    "model": "hvac_action",
                    "confidence": "learning",
                    "confidence_score": 0,
                    "sample_count": total_samples,
                    "active_channel": active_channel,
                    "learned_channel": None,
                    "channels": channel_details,
                }

            return {
                "model": "hvac_action",
                "confidence": representative.get("confidence", "learning"),
                "confidence_score": representative.get("confidence_score", 0),
                "sample_count": total_samples,
                "learned_full_power_w": representative.get("learned_power_w"),
                "deviation_w": representative.get("deviation_w"),
                "active_channel": active_channel,
                "learned_channel": representative_key,
                "channels": channel_details,
            }

        channel = model.channels.get("level")
        if not channel:
            return {
                "model": "normalized_level",
                "confidence": "learning",
                "confidence_score": 0,
                "sample_count": 0,
                "active_channel": "level",
            }
        label, score = channel.confidence(self.min_samples)
        profile = self.device_profiles.get(entity_id)
        return {
            "model": "normalized_level",
            "confidence": label,
            "confidence_score": score,
            "sample_count": channel.sample_count,
            "learned_full_power_w": channel.estimate_w,
            "deviation_w": channel.deviation_w,
            "active_channel": "level",
            "powercalc_exported": bool(profile.powercalc_exported) if profile else False,
            "powercalc_model_id": profile.powercalc_model_id if profile else None,
            "powercalc_exported_at": profile.powercalc_exported_at if profile else None,
            "powercalc_power_w": profile.powercalc_power_w if profile else None,
            "powercalc_graduation": self.powercalc_graduation_assessment(entity_id),
        }

    def _historical_readiness_credit(self, entity_id: str) -> int:
        """Return limited readiness credit for trustworthy pre-0.4 evidence."""
        model = self.models.get(entity_id)
        if not model:
            return 0
        # Historical Gree cycles were validated by a different algorithm. Give
        # them substantial prior weight but still require at least one successful
        # unified 0.4 session before automatic Energy Dashboard enrollment.
        if model.historical_validated_cycles >= 6:
            return max(0, DEFAULT_READY_VALIDATED_SESSIONS - 1)
        if model.historical_validated_cycles >= 3:
            return 1
        # Ordinary 0.3 isolated energy was accumulated only while live isolation
        # was trustworthy. It earns one evidence credit, but the separate 0.6.3
        # profile-quality gate still requires a mature current fingerprint.
        if model.migration_energy_kwh > 0:
            return 1
        return 0

    def _profile_supports_variable_levels(self, entity_id: str) -> bool:
        """Return whether the source exposes a continuously variable level."""
        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state else {}
        domain = entity_id.split(".", 1)[0]
        return bool(
            (domain == "light" and (
                attrs.get("brightness") is not None
                or any(
                    mode not in {"onoff", "unknown"}
                    for mode in (attrs.get("supported_color_modes") or [])
                )
            ))
            or (domain == "fan" and attrs.get("percentage") is not None)
        )

    def _energy_profile_quality_assessment(self, entity_id: str) -> dict[str, Any]:
        """Assess whether learned evidence is mature enough for energy export.

        Climate entities remain native live-isolation profiles and use the
        historical/unified evidence gate below. Other devices must also have a
        stable fixed/linear fit, or repeatable cycling evidence suitable for the
        native integrator. This prevents a migrated-energy credit from making a
        still-provisional lamp appear Energy Dashboard ready.
        """
        model = self.models.get(entity_id)
        profile = self.device_profiles.get(entity_id)
        domain = entity_id.split(".", 1)[0]
        if not model or not profile:
            return {
                "ready": False,
                "native": False,
                "rich_session_count": 0,
                "reasons": ["missing_model"],
            }

        sessions = self._powercalc_validated_sessions(entity_id)
        rich_count = len(sessions)
        if domain == "climate":
            return {
                "ready": True,
                "native": True,
                "rich_session_count": rich_count,
                "reasons": [],
            }
        if domain not in {"switch", "light", "fan", "media_player"}:
            return {
                "ready": False,
                "native": False,
                "rich_session_count": rich_count,
                "reasons": ["unsupported_domain"],
            }

        # A repeatable cycling load can remain on the native live integrator even
        # though it cannot graduate to one deterministic Powercalc wattage.
        if any(session.cycling_detected for session in sessions):
            fixed = self._fixed_profile_assessment(entity_id, sessions)
            allowed_native_reasons = {
                "cycling_detected",
                "between_session_variability",
                "within_session_variability",
            }
            reasons = [
                reason for reason in fixed["reasons"]
                if reason not in allowed_native_reasons
            ]
            return {
                "ready": not reasons,
                "native": not reasons,
                "rich_session_count": rich_count,
                "reasons": reasons,
            }

        if self._profile_supports_variable_levels(entity_id):
            reasons: list[str] = []
            points = self._linear_profile_points(entity_id, sessions)
            if not points:
                reasons.append("insufficient_multi_level_evidence")
            elif points[-1][1] < max(1.0, self.min_step_w):
                reasons.append("power_too_small")
            if any(session.overlap_seen for session in sessions):
                reasons.append("overlap_seen")
            return {
                "ready": not reasons,
                "native": False,
                "rich_session_count": rich_count,
                "reasons": reasons,
            }

        fixed = self._fixed_profile_assessment(entity_id, sessions)
        return {
            "ready": not fixed["reasons"],
            "native": False,
            "rich_session_count": rich_count,
            "reasons": list(fixed["reasons"]),
        }

    def energy_dashboard_readiness_assessment(self, entity_id: str) -> dict[str, Any]:
        """Return evidence, profile-quality, and final Energy Dashboard readiness."""
        model = self.models.get(entity_id)
        profile = self.device_profiles.get(entity_id)
        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state else {}
        fingerprint_revision = tuple(
            (key, len(values), values[-1].completed_at if values else None)
            for key, values in sorted((model.session_fingerprints if model else {}).items())
        )
        cache_key = (
            bool(model and model.isolated_energy_kwh > 0),
            model.validated_sessions if model else None,
            round(self._soft_evidence_summary(entity_id)["evidence_units"], 3) if model else None,
            model.historical_validated_cycles if model else None,
            bool(model and model.migration_energy_kwh > 0),
            profile.retraining_started_at if profile else None,
            profile.fixed_power_mode if profile else None,
            round(float(profile.fixed_power_w), 3) if profile and profile.fixed_power_w is not None else None,
            attrs.get("brightness") is not None,
            tuple(attrs.get("supported_color_modes") or []),
            attrs.get("percentage") is not None,
            fingerprint_revision,
        )
        cached = self._energy_readiness_cache.get(entity_id)
        if cached and cached[0] == cache_key:
            return dict(cached[1])

        historical_credit = self._historical_readiness_credit(entity_id)
        validated_sessions = model.validated_sessions if model else 0
        soft = self._soft_evidence_summary(entity_id)
        soft_credit = float(soft["evidence_units"]) if self._supports_soft_fixed_evidence(entity_id) else 0.0
        evidence_total = validated_sessions + historical_credit + soft_credit
        quality = self._energy_profile_quality_assessment(entity_id)
        manual_fixed = self._manual_fixed_active(entity_id)
        learning_reasons: list[str] = []
        if not model or model.isolated_energy_kwh <= 0:
            learning_reasons.append("no_isolated_energy")
        if evidence_total < DEFAULT_READY_VALIDATED_SESSIONS:
            learning_reasons.append("insufficient_validated_evidence")
        learning_reasons.extend(
            str(reason) for reason in quality["reasons"]
            if reason not in learning_reasons
        )
        reasons = [] if manual_fixed else list(learning_reasons)
        result = {
            "ready": manual_fixed or not reasons,
            "learned_ready": not learning_reasons,
            "learning_reasons": learning_reasons,
            "historical_credit": historical_credit,
            "validated_sessions": validated_sessions,
            "weighted_soft_evidence": round(soft_credit, 3),
            "paired_evidence_count": int(soft.get("paired_count", 0)),
            "soft_evidence_power_w": soft.get("power_w"),
            "soft_evidence_relative_dispersion_pct": soft.get("relative_dispersion_pct"),
            "soft_evidence_ci_low_w": soft.get("confidence_interval_low_w"),
            "soft_evidence_ci_high_w": soft.get("confidence_interval_high_w"),
            "evidence_total": round(evidence_total, 3),
            "required_evidence": DEFAULT_READY_VALIDATED_SESSIONS,
            "rich_session_count": int(quality["rich_session_count"]),
            "profile_quality_ready": bool(quality["ready"]) or manual_fixed,
            "native_profile": bool(quality["native"]),
            "manual_fixed": manual_fixed,
            "manual_fixed_mode": self._manual_fixed_mode(entity_id),
            "manual_fixed_power_w": self._manual_fixed_value_w(entity_id),
            "reasons": reasons,
        }
        self._energy_readiness_cache[entity_id] = (cache_key, dict(result))
        return result

    def device_energy_dashboard_ready(self, entity_id: str) -> bool:
        """Return whether a canonical isolated-energy sensor is ready to export."""
        return bool(self.energy_dashboard_readiness_assessment(entity_id)["ready"])

    def _sensor_entity_ids(
        self, entity_id: str, power_suffix: str, energy_suffix: str
    ) -> tuple[str | None, str | None]:
        """Resolve registered Power Fingerprint power/energy entity ids."""
        registry = er.async_get(self.hass)
        key = self.source_registry_key(entity_id)
        power_unique = f"{self.entry.entry_id}_{key}_{power_suffix}"
        energy_unique = f"{self.entry.entry_id}_{key}_{energy_suffix}"
        return (
            registry.async_get_entity_id("sensor", DOMAIN, power_unique),
            registry.async_get_entity_id("sensor", DOMAIN, energy_unique),
        )

    def _canonical_sensor_entity_ids(self, entity_id: str) -> tuple[str | None, str | None]:
        """Resolve registered unified power/energy entity ids for one source."""
        return self._sensor_entity_ids(entity_id, "isolated_power", "isolated_energy")

    def _legacy_sensor_entity_ids(self, entity_id: str) -> tuple[str | None, str | None]:
        """Resolve deprecated estimated-power/energy compatibility entity ids."""
        return self._sensor_entity_ids(entity_id, "estimated_power", "estimated_energy")

    def device_energy_dashboard_exported(self, entity_id: str) -> bool:
        """Return whether canonical or compatible energy is already on the dashboard."""
        _power, canonical_energy = self._canonical_sensor_entity_ids(entity_id)
        _legacy_power, legacy_energy = self._legacy_sensor_entity_ids(entity_id)
        return bool(
            (canonical_energy and canonical_energy in self._exported_energy_entities)
            or (legacy_energy and legacy_energy in self._exported_energy_entities)
        )

    async def async_set_auto_export_energy(self, enabled: bool) -> None:
        """Set and persist automatic Energy Dashboard export without reloading."""
        enabled = bool(enabled)
        self.auto_export_energy = enabled

        # Store this as an option so the dashboard switch and the integration
        # Options screen remain in sync across Home Assistant restarts.  Updating
        # the config entry does not require an integration reload because the
        # live coordinator state has already been updated above.
        options = dict(self.entry.options)
        options[CONF_AUTO_EXPORT_ENERGY] = enabled
        self.hass.config_entries.async_update_entry(self.entry, options=options)

        # Turning export ON reconciles anything that became ready while export
        # was disabled.  Turning it OFF never removes existing Energy Dashboard
        # entries; a forced read simply refreshes exported-status bookkeeping.
        await self._async_sync_energy_dashboard(force=True)
        self.async_set_updated_data(self.snapshot())

    def _schedule_energy_dashboard_sync(self) -> None:
        """Debounce an Energy Dashboard preference synchronization."""
        if self._energy_sync_task and not self._energy_sync_task.done():
            return
        task = self.entry.async_create_task(
            self.hass,
            self._async_sync_energy_dashboard(),
            name="Power Fingerprint Energy Dashboard sync",
            eager_start=True,
        )
        self._energy_sync_task = task
        self._pending_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._pending_tasks.discard(done_task)
            if self._energy_sync_task is done_task:
                self._energy_sync_task = None

        task.add_done_callback(_done)

    async def _async_sync_energy_dashboard(self, *, force: bool = False) -> None:
        """Add Energy-ready canonical sensors without editing .storage directly."""
        if not (self.use_energy_meters or self.auto_export_energy or force):
            return
        try:
            manager = self._energy_manager or await async_get_manager(self.hass)
            self._energy_manager = manager
            preferences = manager.data or {}
            devices = [dict(item) for item in preferences.get("device_consumption", [])]
            by_energy: dict[str, int] = {
                item.get("stat_consumption"): idx
                for idx, item in enumerate(devices)
                if isinstance(item.get("stat_consumption"), str)
            }
            exported: set[str] = set(by_energy)
            changed = False

            if self.auto_export_energy:
                for entity_id in self.tracked_entities:
                    if not self.device_energy_dashboard_ready(entity_id):
                        continue
                    power_entity, energy_entity = self._canonical_sensor_entity_ids(entity_id)
                    _legacy_power, legacy_energy = self._legacy_sensor_entity_ids(entity_id)
                    if not power_entity or not energy_entity:
                        # Registry rows may not exist during the first post-setup
                        # tick; the periodic sync will retry without losing data.
                        continue
                    friendly = source_display_name(self.hass, entity_id)
                    # If an older Power Fingerprint energy entity is already on
                    # the Energy Dashboard, keep it in place to preserve its long
                    # statistics history. In 0.4 it receives the same unified
                    # energy increments, so it is a safe compatibility surface.
                    dashboard_energy = (
                        energy_entity if energy_entity in by_energy
                        else legacy_energy if legacy_energy and legacy_energy in by_energy
                        else None
                    )
                    if dashboard_energy:
                        idx = by_energy[dashboard_energy]
                        item = dict(devices[idx])
                        if item.get("stat_rate") != power_entity:
                            item["stat_rate"] = power_entity
                            devices[idx] = item
                            changed = True
                        exported.add(dashboard_energy)
                    else:
                        devices.append({
                            "stat_consumption": energy_entity,
                            "stat_rate": power_entity,
                            "name": friendly,
                        })
                        by_energy[energy_entity] = len(devices) - 1
                        changed = True
                        exported.add(energy_entity)

            if changed:
                await manager.async_update({"device_consumption": devices})

            self._exported_energy_entities = exported
            self._last_energy_sync_error = None
            if self.data is not None:
                self.async_set_updated_data(self.snapshot())
        except Exception as err:  # Energy preferences must never break inference.
            self._last_energy_sync_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("Energy Dashboard sync failed: %s", err)

    def _powercalc_validated_sessions(self, entity_id: str) -> list[Any]:
        """Return validated rich sessions, optionally restricted to a retraining epoch."""
        model = self.models.get(entity_id)
        profile = self.device_profiles.get(entity_id)
        if not model:
            return []
        cutoff: datetime | None = None
        if profile and profile.retraining_started_at:
            with suppress(ValueError, TypeError):
                cutoff = datetime.fromisoformat(profile.retraining_started_at.replace("Z", "+00:00"))
        sessions: list[Any] = []
        for values in model.session_fingerprints.values():
            for session in values:
                if not session.quality.startswith("validated"):
                    continue
                if cutoff is not None:
                    with suppress(ValueError, TypeError):
                        completed = datetime.fromisoformat(str(session.completed_at).replace("Z", "+00:00"))
                        if completed <= cutoff:
                            continue
                sessions.append(session)
        return sessions

    def _powercalc_session_values(self, entity_id: str) -> list[float]:
        """Return validated rich-session steady-state medians for local export."""
        return [
            float(session.steady_median_w)
            for session in self._powercalc_validated_sessions(entity_id)
            if session.steady_median_w is not None
        ]

    def _linear_profile_points(self, entity_id: str, sessions: list[Any]) -> list[list[float]]:
        """Extract conservative normalized level->watts points from single-level sessions."""
        domain = entity_id.split(".", 1)[0]
        if domain not in {"light", "fan"}:
            return []
        buckets: dict[float, list[float]] = {}
        contributing = 0
        for session in sessions:
            if session.steady_median_w is None or session.cycling_detected or session.overlap_seen:
                continue
            levels: list[float] = []
            for event in session.setting_events:
                level = event.get("new_level") if isinstance(event, dict) else None
                if level is None:
                    continue
                with suppress(TypeError, ValueError):
                    value = max(0.0, min(1.0, float(level)))
                    if value > 0.05:
                        levels.append(value)
            unique_levels = sorted({round(level, 2) for level in levels})
            # A session spanning several commanded levels does not have one steady
            # median attributable to a single setting, so exclude it from fitting.
            if len(unique_levels) != 1:
                continue
            level = unique_levels[0]
            bucket = round(level * 20.0) / 20.0  # 5% bins damp tiny slider jitter.
            buckets.setdefault(bucket, []).append(float(session.steady_median_w))
            contributing += 1
        if contributing < PROFILE_LINEAR_MIN_SESSIONS or len(buckets) < PROFILE_LINEAR_MIN_DISTINCT_LEVELS:
            return []
        points = [[level, float(robust_median(values) or 0.0)] for level, values in sorted(buckets.items())]
        if not points or (points[-1][0] - points[0][0]) < PROFILE_LINEAR_MIN_LEVEL_SPAN:
            return []
        # Reject materially non-monotonic curves. Small reversals within the
        # residual meter's noise are tolerated and flattened during export.
        cleaned: list[list[float]] = []
        prior_w = 0.0
        for level, watts in points:
            tolerance = max(PROFILE_AUDIT_ABSOLUTE_TOLERANCE_W, max(prior_w, watts) * 0.10)
            if cleaned and watts < prior_w - tolerance:
                return []
            watts = max(prior_w, watts)
            cleaned.append([float(level), round(float(watts), 3)])
            prior_w = watts
        return cleaned

    def _fixed_profile_assessment(self, entity_id: str, sessions: list[Any]) -> dict[str, Any]:
        steady = [float(s.steady_median_w) for s in sessions if s.steady_median_w is not None]
        soft = self._soft_evidence_summary(entity_id)
        reasons: list[str] = []
        if any(s.cycling_detected for s in sessions):
            reasons.append("cycling_detected")
        # Overlap in a rich session is no longer an automatic disqualifier for a
        # fixed load if independent state-confirmed evidence is strong.
        if any(s.overlap_seen for s in sessions) and float(soft["evidence_units"]) < 5.0:
            reasons.append("overlap_seen")

        center = robust_median(steady) if steady else soft.get("power_w")
        if len(steady) >= DEFAULT_READY_VALIDATED_SESSIONS:
            dispersion = (1.4826 * mad(steady, center)) if center is not None else None
            relative_dispersion = dispersion / center if center and dispersion is not None and center > 0 else None
            median_variability = robust_median([
                float(s.variability_pct) for s in sessions if s.variability_pct is not None
            ])
        else:
            dispersion = soft.get("dispersion_w")
            relative_dispersion = (float(soft["relative_dispersion_pct"]) / 100.0) if soft.get("relative_dispersion_pct") is not None else None
            median_variability = None

        effective_evidence = len(steady) + float(soft["evidence_units"])
        if effective_evidence < 5.0:
            reasons.append("insufficient_rich_sessions")
        if center is None or center < 3.0:
            reasons.append("power_too_small")
        # Small fixed loads use an absolute-error escape hatch; percentage error
        # is otherwise misleading for a 5-10 W lamp.
        dispersion_ok = (
            dispersion is not None and (
                (relative_dispersion is not None and relative_dispersion <= 0.25)
                or float(dispersion) <= 2.5
            )
        )
        if not dispersion_ok:
            reasons.append("between_session_variability")
        if median_variability is not None and median_variability > 25.0 and float(soft["evidence_units"]) < 5.0:
            reasons.append("within_session_variability")
        return {
            "reasons": reasons,
            "power_w": round(float(center), 3) if center is not None else None,
            "session_count": len(steady),
            "effective_evidence": round(effective_evidence, 3),
            "soft_evidence_units": soft["evidence_units"],
            "paired_evidence_count": soft["paired_count"],
            "distinct_evidence_days": soft["distinct_days"],
            "relative_dispersion_pct": round(relative_dispersion * 100.0, 2) if relative_dispersion is not None else None,
            "median_within_session_variability_pct": round(float(median_variability), 2) if median_variability is not None else None,
        }

    def powercalc_profile_assessment(self, entity_id: str) -> dict[str, Any]:
        """Classify a mature device into a private local Powercalc profile family."""
        model = self.models.get(entity_id)
        profile = self.device_profiles.get(entity_id)
        domain = entity_id.split(".", 1)[0]
        reasons: list[str] = []
        if domain not in {"switch", "light", "fan", "media_player"}:
            reasons.append("unsupported_domain")
        if not model or not profile:
            return {"eligible": False, "reasons": reasons + ["missing_model"], "entity_id": entity_id}
        if self._manual_fixed_locked(entity_id):
            reasons.append("manual_fixed_locked")

        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state else {}
        fingerprint_revision = tuple(
            (key, len(values), values[-1].completed_at if values else None)
            for key, values in sorted(model.session_fingerprints.items())
        )
        cache_key = (
            model.validated_sessions,
            round(model.isolated_energy_kwh, 6),
            profile.powercalc_exported,
            profile.powercalc_active,
            profile.lifecycle_state,
            profile.retraining_started_at,
            attrs.get("brightness") is not None,
            tuple(attrs.get("supported_color_modes") or []),
            attrs.get("percentage") is not None,
            fingerprint_revision,
        )
        cached = self._profile_assessment_cache.get(entity_id)
        if cached and cached[0] == cache_key:
            return dict(cached[1])

        sessions = self._powercalc_validated_sessions(entity_id)
        variable = self._profile_supports_variable_levels(entity_id)
        profile_type = "linear" if variable else "fixed"
        calibration_points: list[list[float]] = []
        if profile_type == "linear":
            calibration_points = self._linear_profile_points(entity_id, sessions)
            if not calibration_points:
                reasons.append("insufficient_multi_level_evidence")
            power_w = calibration_points[-1][1] if calibration_points else None
            session_count = len(sessions)
            relative_dispersion_pct = None
            median_variability = robust_median([
                float(s.variability_pct) for s in sessions if s.variability_pct is not None
            ])
            if any(s.cycling_detected for s in sessions):
                reasons.append("cycling_detected")
            if any(s.overlap_seen for s in sessions):
                reasons.append("overlap_seen")
        else:
            fixed = self._fixed_profile_assessment(entity_id, sessions)
            reasons.extend(fixed["reasons"])
            # Graduation is intentionally stricter than native validation: require
            # substantial accumulated evidence over multiple days before handing
            # a fixed profile to Powercalc.
            if float(fixed.get("effective_evidence", 0.0)) < 8.0:
                reasons.append("insufficient_graduation_evidence")
            if int(fixed.get("distinct_evidence_days", 0)) < 3 and len(sessions) < 3:
                reasons.append("insufficient_distinct_days")
            if (
                fixed.get("relative_dispersion_pct") is not None
                and float(fixed["relative_dispersion_pct"]) > 15.0
                and (soft := self._soft_evidence_summary(entity_id)).get("dispersion_w") is not None
                and float(soft["dispersion_w"]) > 2.5
            ):
                reasons.append("graduation_dispersion")
            power_w = fixed["power_w"]
            session_count = fixed["session_count"]
            relative_dispersion_pct = fixed["relative_dispersion_pct"]
            median_variability = fixed["median_within_session_variability_pct"]

        if not self.device_energy_dashboard_ready(entity_id):
            reasons.append("not_energy_dashboard_ready")
        if profile.powercalc_active:
            reasons.append("already_active_in_powercalc")
        elif profile.powercalc_exported and profile.lifecycle_state not in {"retraining", "drift_detected"}:
            reasons.append("handoff_already_pending_or_complete")

        result = {
            "eligible": not reasons,
            "reasons": reasons,
            "entity_id": entity_id,
            "profile_type": profile_type,
            "session_count": session_count,
            "power_w": round(float(power_w), 3) if power_w is not None else None,
            "calibration_points": [list(point) for point in calibration_points],
            "relative_dispersion_pct": relative_dispersion_pct,
            "median_within_session_variability_pct": (
                round(float(median_variability), 2) if median_variability is not None else None
            ),
            "already_exported": bool(profile.powercalc_exported),
            "model_id": profile.powercalc_model_id,
        }
        self._profile_assessment_cache[entity_id] = (cache_key, dict(result))
        return result

    # Backward-compatible name retained for cards/diagnostics written for 0.5.7.
    def powercalc_graduation_assessment(self, entity_id: str) -> dict[str, Any]:
        return self.powercalc_profile_assessment(entity_id)

    def _derived_lifecycle_state(self, entity_id: str) -> str:
        profile = self.device_profiles.get(entity_id)
        if not profile:
            return "learning"
        if profile.lifecycle_state in {"retraining", "drift_detected"}:
            return profile.lifecycle_state
        if profile.powercalc_active:
            return "audit"
        if profile.powercalc_exported:
            return "graduated_to_powercalc"
        assessment = self.powercalc_profile_assessment(entity_id)
        if assessment.get("eligible"):
            return "validated"
        reasons = set(assessment.get("reasons") or [])
        if self.device_energy_dashboard_ready(entity_id) and reasons.intersection({"unsupported_domain", "cycling_detected"}):
            return "validated_native"
        session_count = len(self._powercalc_validated_sessions(entity_id))
        soft_units = float(self._soft_evidence_summary(entity_id)["evidence_units"])
        return "candidate" if (session_count >= 2 or soft_units >= 3.0) else "learning"

    def _refresh_profile_lifecycle(self, entity_id: str) -> None:
        profile = self.device_profiles.get(entity_id)
        if not profile:
            return
        new_state = self._derived_lifecycle_state(entity_id)
        if profile.lifecycle_state != new_state:
            profile.lifecycle_state = new_state
            profile.lifecycle_updated_at = utc_iso()

    def powercalc_graduation_options(self) -> list[tuple[str, str]]:
        """Return mature fixed/linear local-Powercalc candidates."""
        pairs: list[tuple[str, str]] = []
        for entity_id in self.tracked_entities:
            assessment = self.powercalc_profile_assessment(entity_id)
            if not assessment.get("eligible"):
                continue
            kind = str(assessment.get("profile_type") or "profile").title()
            watts = assessment.get("power_w")
            suffix = f"{kind} · {watts:g} W" if watts is not None else kind
            pairs.append((entity_id, f"{source_display_name(self.hass, entity_id)} — {suffix}"))
        pairs.sort(key=lambda pair: pair[1].lower())
        return pairs

    def profile_management_options(self) -> list[tuple[str, str]]:
        """Return every tracked device with its current 0.6 lifecycle state."""
        pairs: list[tuple[str, str]] = []
        for entity_id in self.tracked_entities:
            self._refresh_profile_lifecycle(entity_id)
            profile = self.device_profiles.get(entity_id)
            lifecycle = (profile.lifecycle_state if profile else "learning").replace("_", " ").title()
            pairs.append((entity_id, f"{source_display_name(self.hass, entity_id)} — {lifecycle}"))
        return sorted(pairs, key=lambda item: item[1].casefold())

    def _powercalc_profile_model_id(self, entity_id: str) -> str:
        """Return a per-entity local-only model id to avoid cross-device coupling."""
        return f"pf_{slugify(entity_id)}"

    def _powercalc_generated_name(self, entity_id: str) -> str:
        return f"PFGRAD {self._powercalc_profile_model_id(entity_id)}"

    def _powercalc_predicted_entity_ids(self, entity_id: str) -> tuple[str, str]:
        base = slugify(self._powercalc_generated_name(entity_id))
        return f"sensor.{base}_power", f"sensor.{base}_energy"

    def _write_powercalc_export_sync(
        self,
        entity_id: str,
        assessment: dict[str, Any],
        source_name: str,
        config_root: str,
        export_map: dict[str, str],
    ) -> dict[str, Any]:
        """Write a private Powercalc profile using only executor-safe snapshots."""
        model_id = self._powercalc_profile_model_id(entity_id)
        manufacturer_id = "power-fingerprint-local"
        config_path = Path(config_root)
        root = config_path / "powercalc"
        profile_root = root / "profiles" / manufacturer_id
        model_dir = profile_root / model_id
        model_dir.mkdir(parents=True, exist_ok=True)
        manufacturer_file = profile_root / "manufacturer.json"
        if not manufacturer_file.exists():
            manufacturer_file.write_text(json.dumps({
                "name": "Power Fingerprint Local",
                "description": "Private local profiles inferred by Power Fingerprint; not intended for community publication."
            }, indent=2) + "\n")

        domain = entity_id.split(".", 1)[0]
        # These are synthetic per-entity private profiles inferred from a household
        # residual, not canonical manufacturer device profiles. Use generic_iot to
        # avoid asserting device-type-specific metadata (for example a light profile
        # requires a nonzero standby value in the public-library schema). Runtime
        # linear semantics still follow the actual source entity domain.
        device_type = "generic_iot"
        profile_type = str(assessment.get("profile_type") or "fixed")
        payload: dict[str, Any] = {
            "name": source_name,
            "device_type": device_type,
            "calculation_strategy": profile_type,
            "measure_method": "script",
            "measure_device": "Power Fingerprint whole-house residual inference",
            # Powercalc's model schema requires UTC seconds with a trailing Z.
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Absolute standby cannot be identified from an ordinary on/off residual
            # edge because it is part of both baselines. Omit standby_power entirely
            # rather than inventing a value; Powercalc then uses its zero-standby default.
            "measure_description": (
                f"Private local {profile_type} profile inferred from whole-house residual power across "
                f"{assessment.get('session_count', 0)} validated rich sessions. "
                "Not intended for community publication without direct-meter verification."
            ),
        }
        if profile_type == "linear":
            normalized = assessment.get("calibration_points") or []
            native_points: list[str] = []
            for level, watts in normalized:
                x = max(1, min(255, round(float(level) * 255))) if domain == "light" else max(1, min(100, round(float(level) * 100)))
                native_points.append(f"{x} -> {float(watts):.3f}")
            payload["linear_config"] = {"calibrate": native_points}
        else:
            payload["fixed_config"] = {"power": round(float(assessment["power_w"]), 3)}
        model_file = model_dir / "model.json"
        model_file.write_text(json.dumps(payload, indent=2) + "\n")

        # Rebuild the managed package from persistent metadata. This keeps local
        # Powercalc profiles private while making the runtime handoff reproducible.
        packages_dir = config_path / "packages"
        configuration_file = config_path / "configuration.yaml"
        configuration_text = configuration_file.read_text(errors="ignore") if configuration_file.exists() else ""
        package_active = bool(
            packages_dir.exists()
            and re.search(
                r"(?m)^\s*packages\s*:\s*!include_dir_(?:named|merge_named)\s+packages\s*(?:#.*)?$",
                configuration_text,
            )
        )
        binding_file = packages_dir / "power_fingerprint_powercalc_graduates.yaml"
        exports = dict(export_map)
        exports[entity_id] = model_id
        lines = [
            "# Managed by Power Fingerprint 0.6. Private local Powercalc graduates.",
            "# Re-generated on graduation/revocation. Do not publish as community library data.",
            "powercalc:",
            "  sensors:",
        ]
        for eid in sorted(exports):
            mid = exports[eid]
            generated_name = self._powercalc_generated_name(eid)
            lines += [
                f"    - entity_id: {eid}",
                f"      name: {json.dumps(generated_name)}",
                f"      unique_id: {json.dumps('power_fingerprint_' + mid)}",
                f"      manufacturer: {manufacturer_id}",
                f"      model: {mid}",
                "      create_energy_sensor: true",
                "      energy_integration_method: left",
            ]
        if package_active:
            binding_file.parent.mkdir(parents=True, exist_ok=True)
            binding_file.write_text("\n".join(lines) + "\n")
        power_entity, energy_entity = self._powercalc_predicted_entity_ids(entity_id)
        return {
            "entity_id": entity_id,
            "profile_type": profile_type,
            "power_w": assessment.get("power_w"),
            "calibration_points": assessment.get("calibration_points") or [],
            "manufacturer": manufacturer_id,
            "model_id": model_id,
            "profile_file": str(model_file),
            "binding_file": str(binding_file) if package_active else None,
            "package_active": package_active,
            "power_sensor_id": power_entity,
            "energy_sensor_id": energy_entity,
            "reload_or_restart_required": True,
        }

    def _rewrite_powercalc_bindings_sync(self, config_root: str, exports: list[tuple[str, str]]) -> str:
        """Rewrite the managed YAML package from an executor-safe export snapshot."""
        config_path = Path(config_root)
        packages_dir = config_path / "packages"
        binding_file = packages_dir / "power_fingerprint_powercalc_graduates.yaml"
        if not exports:
            with suppress(FileNotFoundError):
                binding_file.unlink()
            return str(binding_file)
        lines = [
            "# Managed by Power Fingerprint 0.6. Private local Powercalc graduates.",
            "# Re-generated on graduation/revocation. Do not publish as community library data.",
            "powercalc:",
            "  sensors:",
        ]
        for eid, mid in sorted(exports):
            lines += [
                f"    - entity_id: {eid}",
                f"      name: {json.dumps(self._powercalc_generated_name(eid))}",
                f"      unique_id: {json.dumps('power_fingerprint_' + str(mid))}",
                "      manufacturer: power-fingerprint-local",
                f"      model: {mid}",
                "      create_energy_sensor: true",
                "      energy_integration_method: left",
            ]
        binding_file.parent.mkdir(parents=True, exist_ok=True)
        binding_file.write_text("\n".join(lines) + "\n")
        return str(binding_file)


    def _resolve_powercalc_entity_ids(self, entity_id: str) -> tuple[str | None, str | None]:
        """Resolve Powercalc entities from registry/state instead of guessed object ids."""
        profile = self.device_profiles.get(entity_id)
        if not profile or not profile.powercalc_model_id:
            return None, None
        registry = er.async_get(self.hass)
        unique_id = f"power_fingerprint_{profile.powercalc_model_id}"
        power_entity = registry.async_get_entity_id("sensor", "powercalc", unique_id)
        if power_entity is None and profile.powercalc_power_sensor_id:
            if registry.async_get(profile.powercalc_power_sensor_id) or self.hass.states.get(profile.powercalc_power_sensor_id):
                power_entity = profile.powercalc_power_sensor_id
        if power_entity is None:
            predicted_power, _predicted_energy = self._powercalc_predicted_entity_ids(entity_id)
            if registry.async_get(predicted_power) or self.hass.states.get(predicted_power):
                power_entity = predicted_power

        energy_entity = None
        if profile.powercalc_energy_sensor_id:
            if registry.async_get(profile.powercalc_energy_sensor_id) or self.hass.states.get(profile.powercalc_energy_sensor_id):
                energy_entity = profile.powercalc_energy_sensor_id
        if power_entity and energy_entity is None:
            for entry in registry.entities.values():
                if entry.platform != "powercalc" or not entry.entity_id.startswith("sensor."):
                    continue
                state = self.hass.states.get(entry.entity_id)
                if not state:
                    continue
                source = (
                    state.attributes.get("source_entity")
                    or state.attributes.get("source_power_entity")
                    or state.attributes.get("power_sensor")
                )
                if source == power_entity and entry.entity_id != power_entity:
                    energy_entity = entry.entity_id
                    break
        if energy_entity is None:
            _predicted_power, predicted_energy = self._powercalc_predicted_entity_ids(entity_id)
            if registry.async_get(predicted_energy) or self.hass.states.get(predicted_energy):
                energy_entity = predicted_energy

        if power_entity:
            profile.powercalc_power_sensor_id = power_entity
        if energy_entity:
            profile.powercalc_energy_sensor_id = energy_entity
        return power_entity, energy_entity

    def _powercalc_power_value(self, entity_id: str) -> float | None:
        profile = self.device_profiles.get(entity_id)
        if not profile or not profile.powercalc_power_sensor_id:
            return None
        state = self.hass.states.get(profile.powercalc_power_sensor_id)
        if not state or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None
        with suppress(TypeError, ValueError):
            return max(0.0, float(state.state))
        return None

    def _powercalc_energy_value(self, entity_id: str) -> float | None:
        profile = self.device_profiles.get(entity_id)
        if not profile or not profile.powercalc_energy_sensor_id:
            return None
        state = self.hass.states.get(profile.powercalc_energy_sensor_id)
        if not state or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None
        with suppress(TypeError, ValueError):
            return max(0.0, float(state.state))
        return None

    async def _async_finalize_powercalc_handoff(self, entity_id: str, *, request_reload: bool = True) -> bool:
        """Seed Powercalc energy, then activate the low-overhead mirror/audit runtime."""
        profile = self.device_profiles.get(entity_id)
        model = self.models.get(entity_id)
        if not profile or not model or not profile.powercalc_exported:
            return False
        if request_reload and self.hass.services.has_service("powercalc", "reload"):
            with suppress(Exception):
                await self.hass.services.async_call("powercalc", "reload", {}, blocking=True)
        if not self.hass.services.has_service("powercalc", "calibrate_energy"):
            profile.powercalc_handoff_status = "waiting_for_powercalc"
            profile.lifecycle_state = "graduated_to_powercalc"
            profile.lifecycle_updated_at = utc_iso()
            self._schedule_save()
            return False

        power_entity, energy_entity = self._resolve_powercalc_entity_ids(entity_id)
        for _ in range(15):
            if power_entity and energy_entity and self.hass.states.get(power_entity) and self.hass.states.get(energy_entity):
                break
            await asyncio.sleep(2)
            power_entity, energy_entity = self._resolve_powercalc_entity_ids(entity_id)
        if not power_entity or not energy_entity or not self.hass.states.get(energy_entity):
            profile.powercalc_handoff_status = "pending_restart"
            profile.lifecycle_state = "graduated_to_powercalc"
            profile.lifecycle_updated_at = utc_iso()
            self._schedule_save()
            return False

        seed = float(model.isolated_energy_kwh)
        try:
            await self.hass.services.async_call(
                "powercalc",
                "calibrate_energy",
                {"value": str(seed)},
                target={"entity_id": energy_entity},
                blocking=True,
            )
        except Exception as err:
            profile.powercalc_handoff_status = f"energy_seed_failed:{type(err).__name__}"
            self._schedule_save()
            _LOGGER.warning("Powercalc energy seed failed for %s: %s", entity_id, err)
            return False

        profile.powercalc_energy_seed_kwh = seed
        profile.powercalc_energy_seeded_at = utc_iso()
        profile.powercalc_active = True
        profile.powercalc_handoff_status = "audit"
        profile.lifecycle_state = "audit"
        profile.lifecycle_updated_at = utc_iso()
        profile.retraining_started_at = None
        self._schedule_save()
        self.async_set_updated_data(self.snapshot())
        return True

    async def _async_reconcile_powercalc_handoffs(self) -> None:
        """Complete exported handoffs after startup when Powercalc entities exist."""
        for entity_id, profile in self.device_profiles.items():
            if profile.powercalc_exported and not profile.powercalc_active and profile.lifecycle_state not in {"retraining", "drift_detected"}:
                with suppress(Exception):
                    await self._async_finalize_powercalc_handoff(entity_id, request_reload=False)

    async def async_graduate_to_powercalc(self, entity_id: str) -> dict[str, Any]:
        """Export one validated device and begin the Powercalc handoff."""
        assessment = self.powercalc_profile_assessment(entity_id)
        if not assessment.get("eligible"):
            raise ValueError(
                f"{entity_id} is not eligible for Powercalc graduation: "
                + ", ".join(assessment.get("reasons") or ["unknown_reason"])
            )
        export_map = {
            eid: str(p.powercalc_model_id)
            for eid, p in self.device_profiles.items()
            if p.powercalc_exported and p.powercalc_model_id
        }
        result = await self.hass.async_add_executor_job(
            self._write_powercalc_export_sync,
            entity_id,
            assessment,
            source_display_name(self.hass, entity_id),
            self.hass.config.config_dir,
            export_map,
        )
        if not result.get("package_active"):
            raise RuntimeError(
                "Powercalc graduation requires homeassistant: packages: !include_dir_named packages "
                "in configuration.yaml; no binding was activated."
            )
        profile = self.device_profiles[entity_id]
        profile.powercalc_exported = True
        profile.powercalc_model_id = str(result["model_id"])
        profile.powercalc_exported_at = utc_iso()
        profile.powercalc_power_w = float(result["power_w"]) if result.get("power_w") is not None else None
        profile.powercalc_profile_type = str(result.get("profile_type") or "fixed")
        profile.powercalc_calibration_points = [list(point) for point in (result.get("calibration_points") or [])]
        profile.powercalc_power_sensor_id = str(result["power_sensor_id"])
        profile.powercalc_energy_sensor_id = str(result["energy_sensor_id"])
        profile.powercalc_handoff_status = "pending_reload"
        profile.powercalc_active = False
        profile.lifecycle_state = "graduated_to_powercalc"
        profile.lifecycle_updated_at = utc_iso()
        profile.audit_observations = 0
        profile.audit_good = 0
        profile.audit_bad = 0
        profile.audit_consecutive_bad = 0
        self.powercalc_last_export = dict(result)
        self._schedule_save()
        self.async_set_updated_data(self.snapshot())
        self.entry.async_create_background_task(
            self.hass,
            self._async_finalize_powercalc_handoff(entity_id),
            name=f"Power Fingerprint Powercalc handoff {entity_id}",
            eager_start=True,
        )
        return result

    async def async_graduate_selected_to_powercalc(self) -> dict[str, Any]:
        options = {eid for eid, _label in self.powercalc_graduation_options()}
        if not self.powercalc_graduation_target or self.powercalc_graduation_target not in options:
            raise ValueError("No eligible Powercalc graduation target selected")
        return await self.async_graduate_to_powercalc(self.powercalc_graduation_target)

    async def async_graduate_all_to_powercalc(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for entity_id, _label in list(self.powercalc_graduation_options()):
            results.append(await self.async_graduate_to_powercalc(entity_id))
        return results

    def _sync_powercalc_energy_back_to_model(self, entity_id: str) -> None:
        mirrored = self._powercalc_energy_value(entity_id)
        if mirrored is None or entity_id not in self.models:
            return
        self.models[entity_id].isolated_energy_kwh = max(self.models[entity_id].isolated_energy_kwh, mirrored)
        self.models[entity_id].energy_kwh = max(self.models[entity_id].energy_kwh, mirrored)

    async def async_retrain_profile(self, entity_id: str) -> None:
        """Suspend Powercalc mirroring and require fresh post-request evidence."""
        profile = self.device_profiles.get(entity_id)
        if not profile:
            raise ValueError(f"Unknown tracked entity: {entity_id}")
        self._sync_powercalc_energy_back_to_model(entity_id)
        profile.powercalc_active = False
        profile.powercalc_handoff_status = "suspended_for_retraining"
        profile.lifecycle_state = "retraining"
        profile.lifecycle_updated_at = utc_iso()
        profile.retraining_started_at = utc_iso()
        profile.audit_consecutive_bad = 0
        self._schedule_save()
        self.async_set_updated_data(self.snapshot())

    async def async_retrain_selected_profile(self) -> None:
        if not self.profile_management_target:
            raise ValueError("No profile management target selected")
        await self.async_retrain_profile(self.profile_management_target)

    async def async_revoke_powercalc_graduation(self, entity_id: str) -> None:
        """Remove a managed Powercalc export while preserving PF evidence/energy."""
        profile = self.device_profiles.get(entity_id)
        if not profile or not profile.powercalc_exported:
            raise ValueError(f"{entity_id} is not a Power Fingerprint-managed Powercalc graduate")
        self._sync_powercalc_energy_back_to_model(entity_id)
        model_id = profile.powercalc_model_id
        profile.powercalc_exported = False
        profile.powercalc_active = False
        profile.powercalc_handoff_status = "revoked"
        profile.powercalc_model_id = None
        profile.powercalc_power_sensor_id = None
        profile.powercalc_energy_sensor_id = None
        profile.powercalc_profile_type = None
        profile.powercalc_calibration_points = []
        profile.retraining_started_at = None
        profile.lifecycle_state = "candidate"
        profile.lifecycle_updated_at = utc_iso()
        exports = [
            (eid, str(p.powercalc_model_id))
            for eid, p in self.device_profiles.items()
            if p.powercalc_exported and p.powercalc_model_id
        ]
        await self.hass.async_add_executor_job(
            self._rewrite_powercalc_bindings_sync, self.hass.config.config_dir, exports
        )
        if model_id:
            model_dir = Path(self.hass.config.path("powercalc", "profiles", "power-fingerprint-local", str(model_id)))
            def _remove_dir() -> None:
                import shutil
                with suppress(FileNotFoundError):
                    shutil.rmtree(model_dir)
            await self.hass.async_add_executor_job(_remove_dir)
        if self.hass.services.has_service("powercalc", "reload"):
            with suppress(Exception):
                await self.hass.services.async_call("powercalc", "reload", {}, blocking=True)
        self._refresh_profile_lifecycle(entity_id)
        self._schedule_save()
        self.async_set_updated_data(self.snapshot())

    async def async_revoke_selected_graduation(self) -> None:
        if not self.profile_management_target:
            raise ValueError("No profile management target selected")
        await self.async_revoke_powercalc_graduation(self.profile_management_target)

    def _profile_expected_power(self, profile: DeviceProfile, level: float) -> float:
        level = max(0.0, min(1.0, float(level)))
        if level <= 0.0:
            return 0.0
        if profile.powercalc_profile_type == "linear" and profile.powercalc_calibration_points:
            points = sorted((float(x), float(y)) for x, y in profile.powercalc_calibration_points)
            if level <= points[0][0]:
                return points[0][1]
            if level >= points[-1][0]:
                return points[-1][1]
            for (x0, y0), (x1, y1) in zip(points, points[1:]):
                if x0 <= level <= x1:
                    frac = (level - x0) / max(1e-9, x1 - x0)
                    return y0 + frac * (y1 - y0)
        return float(profile.powercalc_power_w or 0.0)

    def _process_profile_audit_observation(self, pending: PendingTransition, obs: Observation) -> None:
        """Compare a clean residual edge with the graduated Powercalc model."""
        profile = self.device_profiles.get(pending.entity_id)
        if not profile or not profile.powercalc_active or not obs.accepted:
            return
        expected_delta = self._profile_expected_power(profile, pending.new_level) - self._profile_expected_power(profile, pending.old_level)
        if abs(expected_delta) < max(1.0, self.min_step_w):
            return
        error_w = abs(float(obs.delta_w) - expected_delta)
        tolerance = max(
            PROFILE_AUDIT_ABSOLUTE_TOLERANCE_W,
            abs(expected_delta) * PROFILE_AUDIT_RELATIVE_TOLERANCE,
            float(obs.noise_w) * 2.0,
        )
        good = error_w <= tolerance
        profile.audit_observations += 1
        profile.audit_last_error_w = round(error_w, 3)
        profile.audit_last_error_pct = round(error_w / max(abs(expected_delta), 1.0) * 100.0, 2)
        profile.audit_last_at = utc_iso()
        if good:
            profile.audit_good += 1
            profile.audit_consecutive_bad = 0
        else:
            profile.audit_bad += 1
            profile.audit_consecutive_bad += 1
        if profile.audit_consecutive_bad >= PROFILE_AUDIT_BAD_STREAK_FOR_DRIFT:
            self._sync_powercalc_energy_back_to_model(pending.entity_id)
            profile.powercalc_active = False
            profile.powercalc_handoff_status = "drift_detected"
            profile.lifecycle_state = "drift_detected"
            profile.lifecycle_updated_at = utc_iso()
        self._schedule_save()

    def calibration_options(self) -> list[tuple[str, str]]:
        """Return safe entity_id/display name pairs."""
        result: list[tuple[str, str]] = []
        used: set[str] = set()
        for eid in self.tracked_entities:
            if eid.split(".", 1)[0] not in SAFE_CALIBRATION_DOMAINS:
                continue
            if eid in self.calibration_excluded_entities:
                continue
            if self._manual_fixed_locked(eid):
                continue
            name = source_display_name(self.hass, eid)
            label = name
            if label in used:
                label = f"{name} ({eid})"
            used.add(label)
            result.append((eid, label))
        return sorted(result, key=lambda item: item[1].lower())

    def _calibration_rich_session_count(self, entity_id: str) -> int:
        """Return the number of validated rich fingerprints for one device."""
        model = self.models.get(entity_id)
        if not model:
            return 0
        return sum(len(values) for values in model.session_fingerprints.values())

    def calibration_training_candidates(self) -> list[str]:
        """Return undertrained safe devices in useful training priority order.

        A native batch trains safe devices that still fail actual readiness,
        including devices needing more levels or less variable evidence even
        after three rich sessions. Devices with no rich fingerprint come first; among those,
        previously resolvable devices (accepted edges) are attempted before
        devices dominated by rejected/sub-threshold observations. Heavy loads
        then win ties because their edges are generally easier to isolate.
        """
        rows: list[tuple[int, int, int, int, int, int, str, str]] = []
        for entity_id, label in self.calibration_options():
            model = self.models.get(entity_id)
            profile = self.device_profiles.get(entity_id)
            if not model:
                continue
            if profile and profile.powercalc_exported and profile.lifecycle_state not in {"retraining", "drift_detected"}:
                continue
            rich_count = self._calibration_rich_session_count(entity_id)
            evidence = (
                model.validated_sessions
                + self._historical_readiness_credit(entity_id)
                + self._soft_evidence_summary(entity_id)["evidence_units"]
            )
            force_retrain = bool(profile and profile.lifecycle_state in {"retraining", "drift_detected"})
            training_ready = self.device_energy_dashboard_ready(entity_id)
            if self._manual_fixed_mode(entity_id) == MANUAL_FIXED_MODE_PLACEHOLDER:
                # A placeholder permits provisional energy accounting while
                # learning continues. It is not proof that training is complete.
                training_ready = self.energy_dashboard_readiness_assessment(entity_id)["learned_ready"]
            if not force_retrain and training_ready:
                continue
            accepted_edges = sum(
                channel.sample_count for channel in model.channels.values()
            )
            step_too_small = sum(
                1
                for channel in model.channels.values()
                for obs in channel.observations
                if not obs.get("accepted") and obs.get("reason") == "step_too_small"
            )
            # A device that has already produced clean accepted edges is more
            # likely to turn a quiet deliberate cycle into a complete fingerprint.
            # Devices dominated by sub-threshold edges are not discarded; they
            # simply move later in the queue so a finite overnight window spends
            # its best time on the most resolvable missing profiles first.
            resolvability_rank = 0 if accepted_edges else 1
            profile = self.device_profiles.get(entity_id)
            heavy_rank = 0 if profile and profile.heavy_load else 1
            rows.append(
                (
                    rich_count,
                    resolvability_rank,
                    -min(accepted_edges, 100),
                    step_too_small,
                    evidence,
                    heavy_rank,
                    label.casefold(),
                    entity_id,
                )
            )
        rows.sort()
        return [row[-1] for row in rows]

    def _calibration_evidence_snapshot(self, entity_id: str) -> dict[str, Any]:
        """Capture compact evidence counters around one deliberate calibration."""
        model = self.models.get(entity_id)
        if not model:
            return {
                "rich_sessions": 0,
                "validated_sessions": 0,
                "rejected_sessions": 0,
                "accepted_edges": 0,
                "rejected_edges": 0,
                "last_edge_reason": None,
                "last_edge_ts": None,
                "last_session_rejection_reason": None,
                "weighted_evidence": 0.0,
            }
        observations = [obs for channel in model.channels.values() for obs in channel.observations]
        latest = max(observations, key=lambda obs: str(obs.get("ts", "")), default=None)
        return {
            "rich_sessions": self._calibration_rich_session_count(entity_id),
            "validated_sessions": model.validated_sessions,
            "rejected_sessions": model.rejected_sessions,
            "accepted_edges": sum(1 for obs in observations if obs.get("accepted")),
            "rejected_edges": sum(1 for obs in observations if not obs.get("accepted")),
            "last_edge_reason": (str(latest.get("reason")) if latest and latest.get("reason") else None),
            "last_edge_ts": (str(latest.get("ts")) if latest and latest.get("ts") else None),
            "last_session_rejection_reason": model.last_session_rejection_reason,
            "weighted_evidence": self._soft_evidence_summary(entity_id)["evidence_units"],
        }

    def _classify_calibration_attempt(
        self, before: dict[str, Any], after: dict[str, Any]
    ) -> str:
        """Classify whether one calibration attempt created useful new evidence."""
        if after["rich_sessions"] > before["rich_sessions"]:
            return "validated_fingerprint"
        if after["validated_sessions"] > before["validated_sessions"]:
            return "validated_session_without_fingerprint"
        if float(after.get("weighted_evidence", 0.0)) > float(before.get("weighted_evidence", 0.0)) + 0.05:
            return "evidence_added"
        if after["rejected_sessions"] > before["rejected_sessions"]:
            reason = after.get("last_session_rejection_reason") or "session_rejected"
            return f"rejected:{reason}"
        edge_changed = after.get("last_edge_ts") != before.get("last_edge_ts")
        if after["accepted_edges"] > before["accepted_edges"]:
            return "partial_edges_only"
        if after["rejected_edges"] > before["rejected_edges"] or edge_changed:
            reason = after.get("last_edge_reason") or "edge_rejected"
            if reason in {"accepted", "accepted_change_point"}:
                return "partial_edges_only"
            return f"rejected:{reason}"
        return "no_new_evidence"

    @staticmethod
    def _calibration_result_retryable(result: str) -> bool:
        """Return whether another attempt is sensible after a transient failure."""
        if result in {"partial_edges_only", "no_new_evidence", "evidence_added"}:
            return True
        if not result.startswith("rejected:"):
            return False
        reason = result.split(":", 1)[1]
        return reason in {
            "insufficient_samples",
            "source_not_refreshed",
            "concurrent_tracked_transition",
            "meter_transition_nearby",
            "meter_transition_in_measurement_window",
            "meter_unavailable",
            "wrong_step_direction",
            "edge_symmetry_mismatch",
        }

    def _calibration_quiet_baseline(self, entity_id: str) -> tuple[bool, str]:
        """Judge whether residual power is quiet enough to start calibration."""
        now = time.monotonic()
        window_seconds = max(12.0, min(25.0, float(self.pre_window_seconds)))
        samples = [
            sample for sample in self._sample_buffer
            if now - sample.ts <= window_seconds and sample.valid_for_learning
        ]
        required_samples = max(
            self.min_samples + 2,
            int(window_seconds / max(1, self.sample_interval_seconds) * 0.6),
        )
        if len(samples) < required_samples:
            return False, "insufficient_clean_samples"

        values = [sample.residual_w for sample in samples]
        noise = mad(values) if values else None
        spread = (max(values) - min(values)) if values else None

        model = self.models.get(entity_id)
        estimates = [
            channel.estimate_w
            for channel in (model.channels.values() if model else [])
            if channel.estimate_w is not None
        ]
        estimated_w = robust_median([float(value) for value in estimates if value is not None])
        if estimated_w is not None and estimated_w < 20.0:
            max_mad = 1.5
            max_spread = 8.0
        elif estimated_w is not None and estimated_w < 50.0:
            max_mad = 3.0
            max_spread = 15.0
        else:
            max_mad = max(2.0, min(8.0, self.max_noise_w * 0.25))
            max_spread = max(10.0, min(35.0, self.max_noise_w * 1.1))

        if noise is None or noise > max_mad:
            return False, f"residual_noise_{noise:.1f}W" if noise is not None else "no_residual_noise_measure"
        if spread is None or spread > max_spread:
            return False, f"residual_spread_{spread:.1f}W" if spread is not None else "no_residual_spread_measure"

        transition_guard = max(10.0, min(20.0, float(self.concurrency_seconds)))
        if any(
            other_entity != entity_id and now - ts <= transition_guard
            for ts, other_entity in self._transition_log
        ):
            return False, "recent_tracked_transition"

        meter_guard = max(6.0, self.sample_interval_seconds * 3.0)
        if any(now - ts <= meter_guard for ts, _meter, _delta in self._meter_change_log):
            return False, "recent_meter_transition"
        if self.recovering_meters or self.stale_meters:
            return False, "meter_data_recovering"
        return True, "quiet"

    async def _async_wait_for_calibration_quiet(
        self, entity_id: str, timeout: float = 180.0
    ) -> tuple[bool, str]:
        """Wait for a stable baseline before a native batch calibration."""
        started = time.monotonic()
        last_reason = "waiting_for_samples"
        while time.monotonic() - started < timeout:
            if self.calibration_batch_stop_requested:
                return False, "stop_requested"
            quiet, reason = self._calibration_quiet_baseline(entity_id)
            last_reason = reason
            if self.calibration_batch_running:
                self.calibration_batch_last_result = f"waiting:{reason}"
            else:
                self.selected_calibration_last_result = f"waiting:{reason}"
            self.calibration_phase = "waiting_for_quiet_baseline"
            self.async_set_updated_data(self.snapshot())
            if quiet:
                return True, reason
            await asyncio.sleep(max(1.0, float(self.sample_interval_seconds)))
        return False, f"quiet_timeout:{last_reason}"

    def _supports_multilevel_calibration(self, entity_id: str) -> bool:
        """Return whether active calibration can safely sweep several commanded levels."""
        state = self.hass.states.get(entity_id)
        if not state:
            return False
        domain = entity_id.split(".", 1)[0]
        if domain == "light":
            modes = {str(mode).lower() for mode in (state.attributes.get("supported_color_modes") or [])}
            return bool(modes - {"onoff", "unknown"})
        if domain == "fan":
            return (
                state.attributes.get("percentage") is not None
                or state.attributes.get("percentage_step") is not None
            )
        return False

    async def _async_turn_on_calibration_level(self, entity_id: str, level_pct: int) -> None:
        """Turn a multilevel light/fan on at a deliberate calibration setpoint."""
        domain = entity_id.split(".", 1)[0]
        pct = max(1, min(100, int(level_pct)))
        if domain == "light":
            await self.hass.services.async_call(
                "light", "turn_on", {ATTR_ENTITY_ID: entity_id, "brightness_pct": pct}, blocking=True
            )
            return
        if domain == "fan":
            # set_percentage is the canonical HA level control and normally turns
            # the fan on as needed.  Fall back to turn_on for unusual fan entities.
            if self.hass.services.has_service("fan", "set_percentage"):
                await self.hass.services.async_call(
                    "fan", "set_percentage", {ATTR_ENTITY_ID: entity_id, "percentage": pct}, blocking=True
                )
            else:
                await self.hass.services.async_call(
                    "fan", "turn_on", {ATTR_ENTITY_ID: entity_id, "percentage": pct}, blocking=True
                )
            return
        await self.hass.services.async_call(
            "homeassistant", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )

    def _calibration_dwell_seconds(self) -> float:
        """Leave enough time for a bounded edge evaluation before the next command."""
        return max(
            self.settle_seconds + self.post_window_seconds + 3.0,
            ISOLATION_SETTLE_SECONDS + self._edge_sample_timeout_seconds()
            + self.sample_interval_seconds + 1.0,
        )

    async def _async_calibrate_multilevel_entity(self, entity_id: str) -> None:
        """Run separate OFF→level→OFF sessions to populate a linear profile."""
        for level_pct in CALIBRATION_SWEEP_LEVELS_PCT:
            if self.calibration_batch_stop_requested:
                break
            # Require the household residual to settle between levels.  The quiet
            # check ignores this entity's own recent transition but blocks unrelated
            # tracked/meter activity from contaminating the next measurement.
            quiet, _reason = await self._async_wait_for_calibration_quiet(entity_id, timeout=180.0)
            if not quiet:
                if self.calibration_batch_stop_requested:
                    break
                raise CalibrationDeferred(_reason)
            self.calibration_campaign_level_pct = int(level_pct)
            self.calibration_phase = f"measuring_level_{level_pct}"
            self.async_set_updated_data(self.snapshot())
            await self._async_turn_on_calibration_level(entity_id, level_pct)
            await asyncio.sleep(self._calibration_dwell_seconds())

            self.calibration_phase = f"measuring_off_after_{level_pct}"
            self.async_set_updated_data(self.snapshot())
            await self.hass.services.async_call(
                "homeassistant", "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True
            )
            await asyncio.sleep(self._calibration_dwell_seconds())

    async def _async_calibrate_entity(self, entity_id: str) -> None:
        """Run an adaptive calibration campaign and let the normal learner observe it."""
        if entity_id.split(".", 1)[0] not in SAFE_CALIBRATION_DOMAINS:
            raise ValueError(f"Calibration is not supported for {entity_id}")
        async with self._calibration_lock:
            original = self.hass.states.get(entity_id)
            if not original or original.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                raise ValueError(f"Entity {entity_id} is unavailable")
            self.calibrating_entity = entity_id
            self.calibration_campaign_level_pct = None
            changed_state = False
            try:
                # The batch has already waited for a quiet baseline. Apply the
                # same guard to a manually selected calibration before touching
                # the device; the lock prevents two manual presses overlapping.
                if not self.calibration_batch_running:
                    quiet, reason = await self._async_wait_for_calibration_quiet(entity_id)
                    if not quiet:
                        raise CalibrationDeferred(reason)
                self.calibration_phase = "establishing_off_baseline"
                self.async_set_updated_data(self.snapshot())
                changed_state = True
                await self.hass.services.async_call(
                    "homeassistant", "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True
                )
                await asyncio.sleep(self._calibration_dwell_seconds())

                if self._supports_multilevel_calibration(entity_id):
                    await self._async_calibrate_multilevel_entity(entity_id)
                else:
                    # A tracked or direct-meter change during the OFF baseline
                    # must settle before the deliberate ON edge is measured.
                    quiet, reason = await self._async_wait_for_calibration_quiet(entity_id)
                    if not quiet:
                        raise CalibrationDeferred(reason)
                    self.calibration_phase = "measuring_on"
                    self.async_set_updated_data(self.snapshot())
                    # Reproduce the original ON state when possible because that restores
                    # dimmer brightness / fan percentage. If originally OFF, a normal
                    # turn_on uses the device's most recent level.
                    if original.state == STATE_ON:
                        await async_reproduce_state(self.hass, [original], reproduce_options={})
                    else:
                        await self.hass.services.async_call(
                            "homeassistant", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
                        )
                    await asyncio.sleep(self._calibration_dwell_seconds())

                    self.calibration_phase = "measuring_off"
                    self.async_set_updated_data(self.snapshot())
                    await self.hass.services.async_call(
                        "homeassistant", "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True
                    )
                    await asyncio.sleep(self._calibration_dwell_seconds())
            finally:
                self.calibration_campaign_level_pct = None
                if changed_state:
                    self.calibration_phase = "restoring"
                    self.async_set_updated_data(self.snapshot())
                    with suppress(Exception):
                        await async_reproduce_state(self.hass, [original], reproduce_options={})
                self.calibration_phase = "idle"
                self.calibrating_entity = None
                self.async_set_updated_data(self.snapshot())

    async def async_calibrate_selected(self) -> None:
        """Calibrate the selected device without disturbing a running native batch."""
        if self.calibration_batch_running:
            raise ValueError("A calibration batch is already running")
        if self.calibrating_entity:
            raise ValueError(f"Calibration already running for {self.calibrating_entity}")
        if not self.calibration_target:
            raise ValueError("No calibration target selected")
        entity_id = self.calibration_target
        before = self._calibration_evidence_snapshot(entity_id)
        try:
            await self._async_calibrate_entity(entity_id)
        except CalibrationDeferred as err:
            self.selected_calibration_last_result = str(err)
        else:
            after = self._calibration_evidence_snapshot(entity_id)
            self.selected_calibration_last_result = self._classify_calibration_attempt(before, after)
        self._schedule_save()
        self.async_set_updated_data(self.snapshot())

    def request_calibration_batch_stop(self) -> None:
        """Gracefully stop a native batch after the current device finishes."""
        if not self.calibration_batch_running:
            return
        self.calibration_batch_stop_requested = True
        self.calibration_batch_status = "stopping"
        self.calibration_batch_last_result = "stop_requested"
        self.async_set_updated_data(self.snapshot())

    async def async_calibrate_training_batch(self) -> None:
        """Train devices failing readiness, with quiet gating and one retry."""
        if self.calibration_batch_running:
            return
        if self.calibrating_entity:
            raise ValueError(f"Calibration already running for {self.calibrating_entity}")

        async with self._calibration_batch_lock:
            if self.calibration_batch_running:
                return
            all_safe = [entity_id for entity_id, _label in self.calibration_options()]
            targets = self.calibration_training_candidates()
            self.calibration_batch_running = True
            self.calibration_batch_stop_requested = False
            self.calibration_batch_total = len(targets)
            self.calibration_batch_candidates = list(targets)
            self.calibration_batch_completed = 0
            self.calibration_batch_validated = 0
            self.calibration_batch_failed = 0
            self.calibration_batch_skipped = max(0, len(all_safe) - len(targets))
            self.calibration_batch_retried = 0
            self.calibration_batch_current_entity = None
            self.calibration_batch_last_result = None
            self.calibration_batch_status = "running" if targets else "nothing_to_train"
            self.calibration_batch_started_at = utc_iso()
            self.calibration_batch_results.clear()
            self.async_set_updated_data(self.snapshot())

            try:
                for entity_id in targets:
                    if self.calibration_batch_stop_requested:
                        break
                    self.calibration_batch_current_entity = entity_id
                    final_result = "not_started"
                    attempts = 0
                    validated = False

                    state = self.hass.states.get(entity_id)
                    if not state or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                        final_result = "skipped:unavailable"
                        self.calibration_batch_skipped += 1
                    else:
                        while attempts < 2 and not self.calibration_batch_stop_requested:
                            quiet, quiet_reason = await self._async_wait_for_calibration_quiet(entity_id)
                            if not quiet:
                                final_result = quiet_reason
                                break

                            attempts += 1
                            before = self._calibration_evidence_snapshot(entity_id)
                            try:
                                await self._async_calibrate_entity(entity_id)
                            except CalibrationDeferred as err:
                                final_result = str(err)
                                break
                            except Exception as err:  # one device must not abort the batch
                                _LOGGER.warning("Calibration failed for %s: %s", entity_id, err)
                                final_result = f"error:{type(err).__name__}"
                                break
                            after = self._calibration_evidence_snapshot(entity_id)
                            final_result = self._classify_calibration_attempt(before, after)
                            self.calibration_batch_last_result = final_result
                            self.async_set_updated_data(self.snapshot())

                            if final_result in {
                                "validated_fingerprint",
                                "validated_session_without_fingerprint",
                            }:
                                validated = True
                                break
                            if attempts < 2 and self._calibration_result_retryable(final_result):
                                self.calibration_batch_retried += 1
                                self.calibration_batch_last_result = f"retrying:{final_result}"
                                self.async_set_updated_data(self.snapshot())
                                await asyncio.sleep(max(4.0, float(self.sample_interval_seconds) * 2.0))
                                continue
                            break

                    if final_result == "stop_requested" and attempts == 0:
                        break

                    self.calibration_batch_completed += 1
                    if validated:
                        self.calibration_batch_validated += 1
                    elif not final_result.startswith("skipped:"):
                        self.calibration_batch_failed += 1
                    self.calibration_batch_last_result = final_result
                    self.calibration_batch_results.append(
                        {
                            "entity_id": entity_id,
                            "name": source_display_name(self.hass, entity_id),
                            "attempts": attempts,
                            "result": final_result,
                            "rich_sessions": self._calibration_rich_session_count(entity_id),
                            "validated_sessions": self.models[entity_id].validated_sessions,
                        }
                    )
                    self.async_set_updated_data(self.snapshot())
                    if not self.calibration_batch_stop_requested:
                        await asyncio.sleep(max(2.0, float(self.sample_interval_seconds)))

                self.calibration_batch_status = (
                    "stopped" if self.calibration_batch_stop_requested else
                    ("nothing_to_train" if not targets else "completed")
                )
            except asyncio.CancelledError:
                self.calibration_batch_status = "interrupted"
                raise
            except Exception as err:  # protect the integration from batch-only failures
                self.calibration_batch_status = f"error:{type(err).__name__}"
                self.calibration_batch_last_result = str(err)
                _LOGGER.exception("Unexpected intelligent calibration batch failure")
            finally:
                self.calibration_batch_running = False
                self.calibration_batch_current_entity = None
                self.calibration_batch_stop_requested = False
                self.calibration_phase = "idle"
                self._schedule_save()
                self.async_set_updated_data(self.snapshot())

    async def async_reset_model(self, entity_id: str | None = None, *, reset_energy: bool = False) -> None:
        """Reset learned observations for one/all devices."""
        targets = [entity_id] if entity_id else list(self.models)
        for eid in targets:
            if eid in self.models:
                previous = self.models[eid]
                energy = 0.0 if reset_energy else previous.energy_kwh
                isolated_energy = 0.0 if reset_energy else previous.isolated_energy_kwh
                migration_energy = 0.0 if reset_energy else min(
                    previous.migration_energy_kwh, isolated_energy
                )
                self.models[eid] = DeviceModel(
                    eid,
                    energy_kwh=energy,
                    isolated_energy_kwh=isolated_energy,
                    migration_energy_kwh=migration_energy,
                    migration_source=(previous.migration_source if migration_energy > 0 else None),
                )
                self.isolation_sessions[eid] = IsolationSession(eid)
        if entity_id is None:
            self._last_isolated_powers = {eid: 0.0 for eid in self.models}
            self.accepted_count = 0
            self.rejected_count = 0
            self.rejection_reasons.clear()
            self.session_accepted_count = 0
            self.session_rejected_count = 0
            self.session_rejection_reasons.clear()
            self.last_observation = None
            self._sample_buffer.clear()
        elif entity_id in self.models:
            self._last_isolated_powers[entity_id] = 0.0
        self._schedule_save()
        self.async_set_updated_data(self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        """Compact coordinator state consumed by entities."""
        residual = self.last_residual_w
        coverage = 0.0
        if residual and residual > 1:
            coverage = max(0.0, min(100.0, self.last_fingerprinted_w / residual * 100.0))
        now = time.monotonic()
        recent = [s for s in self._sample_buffer if now - s.ts <= 60.0]
        recent_valid = [s for s in recent if s.valid_for_learning]
        recent_values = [s.residual_w for s in recent_valid]
        for eid in self.tracked_entities:
            self._refresh_profile_lifecycle(eid)
        ready_devices = [eid for eid in self.tracked_entities if self.device_energy_dashboard_ready(eid)]
        exported_devices = [eid for eid in ready_devices if self.device_energy_dashboard_exported(eid)]
        isolation_summary = self._live_isolation_summary(ready_devices)
        source_state = self.hass.states.get(self.source_entity)
        source_report = source_state.last_reported if source_state else None
        wall_now = datetime.now(timezone.utc).timestamp()
        graduation_options = self.powercalc_graduation_options()
        validated_states = {"validated", "validated_native", "audit", "graduated_to_powercalc"}
        validated_runtime_power = 0.0
        candidate_runtime_power = 0.0
        candidate_profile_power = 0.0
        for eid, profile in self.device_profiles.items():
            runtime_w = self.device_isolated_power_w(eid) or 0.0
            if profile.lifecycle_state in validated_states:
                validated_runtime_power += max(0.0, runtime_w)
            elif profile.lifecycle_state == "candidate":
                candidate_runtime_power += max(0.0, runtime_w)
                if self._supports_soft_fixed_evidence(eid):
                    level = self._entity_runtime_level(eid) or 0.0
                    soft = self._soft_evidence_summary(eid)
                    if level > 0 and soft.get("power_w") is not None:
                        candidate_profile_power += max(0.0, float(soft["power_w"]) * level)
        validated_coverage = 0.0
        potential_coverage = coverage
        if residual and residual > 1:
            validated_coverage = max(0.0, min(100.0, validated_runtime_power / residual * 100.0))
            candidate_increment = max(0.0, candidate_profile_power - candidate_runtime_power)
            potential_coverage = max(0.0, min(100.0, (self.last_fingerprinted_w + candidate_increment) / residual * 100.0))
        return {
            "source_entity": self.source_entity,
            "source_power_w": self.last_source_w,
            "source_report_age_seconds": round(max(0.0, wall_now - source_report.timestamp()), 1) if source_report else None,
            "source_report_interval_seconds": self._source_report_interval_seconds(),
            "source_reports_last_60s": len({
                s.source_reported_at for s in recent
                if s.source_reported_at is not None and s.source_reported_at >= wall_now - 60.0
            }),
            "ordinary_edge_timeout_seconds": self._edge_sample_timeout_seconds(),
            "live_isolation_summary": isolation_summary,
            "learning_blocker_counts": isolation_summary["learning_blocker_counts"],
            "meter_entities": self.meter_entities,
            "metered_power_w": self.last_metered_w,
            "residual_power_w": residual,
            "fingerprinted_power_w": self.last_fingerprinted_w,
            "unexplained_power_w": self.last_unexplained_w,
            "coverage_percent": coverage,
            "validated_coverage_percent": validated_coverage,
            "potential_coverage_percent": potential_coverage,
            "validated_runtime_power_w": validated_runtime_power,
            "candidate_runtime_power_w": candidate_runtime_power,
            "candidate_profile_power_w": candidate_profile_power,
            "missing_meters": self.missing_meters,
            "stale_meters": self.stale_meters,
            "quarantined_meters": self.quarantined_meters,
            "recovering_meters": self.recovering_meters,
            "active_meter_count": len(self.meter_entities) - len(self._quarantined_meters),
            "total_meter_count": len(self.meter_entities),
            "quarantined_meter_risk_w": self._recent_quarantined_risk_w(),
            "samples_last_60s": len(recent),
            "valid_samples_last_60s": len(recent_valid),
            "residual_median_60s": robust_median(recent_values),
            "residual_mad_60s": mad(recent_values) if recent_values else None,
            "sample_interval_seconds": self.sample_interval_seconds,
            "meter_grace_seconds": self.meter_grace_seconds,
            "meter_rejoin_seconds": self.meter_rejoin_seconds,
            "meter_transition_threshold_w": max(15.0, self.min_step_w * 3.0),
            "meter_transition_guard_seconds": max(4.0, self.sample_interval_seconds * 2.0),
            "meter_transients_last_60s": sum(1 for ts, _, _ in self._meter_change_log if now - ts <= 60.0),
            "accepted_observations": self.accepted_count,
            "rejected_observations": self.rejected_count,
            "rejection_reasons": dict(self.rejection_reasons),
            "session_accepted_observations": self.session_accepted_count,
            "session_rejected_observations": self.session_rejected_count,
            "session_rejection_reasons": dict(self.session_rejection_reasons),
            "concurrent_decomposed_observations": self._concurrent_decomposed_count,
            "rich_fingerprint_session_count": sum(
                len(sessions)
                for model in self.models.values()
                for sessions in model.session_fingerprints.values()
            ),
            "fingerprint_schema_version": FINGERPRINT_SCHEMA_VERSION,
            "last_observation": self.last_observation,
            "learning_enabled": self.learning_enabled,
            "calibrating_entity": self.calibrating_entity,
            "calibration_phase": self.calibration_phase,
            "calibration_campaign_level_pct": self.calibration_campaign_level_pct,
            "calibration_batch_running": self.calibration_batch_running,
            "calibration_batch_stop_requested": self.calibration_batch_stop_requested,
            "calibration_batch_status": self.calibration_batch_status,
            "calibration_batch_started_at": self.calibration_batch_started_at,
            "calibration_batch_total": self.calibration_batch_total,
            "calibration_batch_completed": self.calibration_batch_completed,
            "calibration_batch_validated": self.calibration_batch_validated,
            "calibration_batch_failed": self.calibration_batch_failed,
            "calibration_batch_skipped": self.calibration_batch_skipped,
            "calibration_batch_retried": self.calibration_batch_retried,
            "calibration_batch_current_entity": self.calibration_batch_current_entity,
            "calibration_batch_last_result": self.calibration_batch_last_result,
            "selected_calibration_last_result": self.selected_calibration_last_result,
            "calibration_batch_results": list(self.calibration_batch_results)[-12:],
            "calibration_batch_outcome_counts": dict(Counter(
                str(result.get("result") or "unknown")
                for result in self.calibration_batch_results
            )),
            "calibration_batch_candidate_count": len(self.calibration_batch_candidates),
            "calibration_batch_candidates": list(self.calibration_batch_candidates)[:25],
            "inverter_running": False,
            "unified_model_version": VERSION,
            "auto_export_energy_dashboard": self.auto_export_energy,
            "energy_dashboard_ready_count": len(ready_devices),
            "energy_dashboard_exported_count": len(exported_devices),
            "energy_dashboard_ready_entities": ready_devices,
            "energy_dashboard_exported_entities": exported_devices,
            "energy_dashboard_sync_error": self._last_energy_sync_error,
            "powercalc_graduation_eligible_entities": [eid for eid, _label in graduation_options],
            "powercalc_graduation_eligible_count": len(graduation_options),
            "powercalc_exported_entities": [eid for eid, profile in self.device_profiles.items() if profile.powercalc_exported],
            "powercalc_exported_count": sum(1 for profile in self.device_profiles.values() if profile.powercalc_exported),
            "profile_lifecycle_counts": dict(Counter(profile.lifecycle_state for profile in self.device_profiles.values())),
            "profile_learning_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "learning"],
            "profile_candidate_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "candidate"],
            "profile_validated_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "validated"],
            "profile_native_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "validated_native"],
            "profile_audit_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "audit"],
            "profile_drift_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "drift_detected"],
            "profile_retraining_entities": [eid for eid, profile in self.device_profiles.items() if profile.lifecycle_state == "retraining"],
            "powercalc_active_entities": [eid for eid, profile in self.device_profiles.items() if profile.powercalc_active],
            "powercalc_active_count": sum(1 for profile in self.device_profiles.values() if profile.powercalc_active),
            "powercalc_pending_handoff_entities": [eid for eid, profile in self.device_profiles.items() if profile.powercalc_exported and not profile.powercalc_active and profile.lifecycle_state not in {"retraining", "drift_detected"}],
            "powercalc_last_export": self.powercalc_last_export,
            "runtime_profile_fallback_active_count": sum(
                1 for value in self._runtime_fallback_last_w.values() if value is not None and value > 0
            ),
            "energy_outlier_filtered_count": int(sum(self._energy_outlier_filtered_count.values())),
            "energy_outlier_filtered_entities": [
                eid for eid, count in self._energy_outlier_filtered_count.items() if count > 0
            ],
            "fixed_power_target": self.fixed_power_target,
            "fixed_power_target_attributes": self.fixed_power_target_attributes(),
            "manual_fixed_placeholder_entities": [
                eid for eid in self.tracked_entities
                if self._manual_fixed_mode(eid) == MANUAL_FIXED_MODE_PLACEHOLDER
                and self._manual_fixed_value_w(eid) is not None
            ],
            "manual_fixed_locked_entities": [
                eid for eid in self.tracked_entities
                if self._manual_fixed_locked(eid)
            ],
            "manual_fixed_count": sum(
                1 for eid in self.tracked_entities if self._manual_fixed_active(eid)
            ),
            "outdoor_block_entities": list(self.outdoor_block_entities),
            "outdoor_block_active": self._outdoor_block_active,
            "outdoor_block_power_w": self._outdoor_block_power_w,
            "outdoor_block_energy_kwh": self._outdoor_block_energy_kwh,
            "outdoor_block_legacy_energy_kwh": self._outdoor_block_legacy_energy_kwh,
            "outdoor_block_sample_count": self._outdoor_block_sample_count,
            "outdoor_block_mode": self._outdoor_block_mode,
            "outdoor_ensemble_mature": self._outdoor_ensemble_mature,
            "outdoor_ensemble_confidence": self._outdoor_ensemble_confidence,
            "outdoor_ensemble_equation_count": len(self._outdoor_ensemble_equations),
            "outdoor_ensemble_unique_signature_count": self._outdoor_ensemble_unique_signature_count,
            "outdoor_ensemble_rejected_equations": dict(self._outdoor_ensemble_rejected_equations),
            "outdoor_ensemble_rank": self._outdoor_ensemble_rank,
            "outdoor_ensemble_required_rank": len(self.outdoor_block_entities),
            "outdoor_ensemble_rmse_w": self._outdoor_ensemble_rmse_w,
            "outdoor_ensemble_member_power_w": dict(self._outdoor_ensemble_member_power_w),
            "outdoor_ensemble_member_evidence": dict(self._outdoor_ensemble_member_evidence),
            "outdoor_ensemble_predicted_total_w": self._outdoor_block_predicted_total_w,
            "outdoor_ensemble_individual_identified_w": self._outdoor_block_individual_identified_w,
        }

    def source_registry_key(self, entity_id: str) -> str:
        """Stable-ish source key based on entity registry row id."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(entity_id)
        return entry.id if entry else entity_id
