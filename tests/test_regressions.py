"""Offline regression checks for 0.7.4; run with Python's unittest runner.

HA boundaries are stubbed; the coordinator, models, sensors, and learning
methods under test are the actual distributable code. These checks do not
replace installation testing in a running Home Assistant instance.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import importlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    sys.modules[name] = result
    return result


class GenericStub:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, hass=None, *_args, **_kwargs):
        self.hass = hass
        self.data = {}
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.data = data

    def async_delay_save(self, *_args):
        pass


class CoordinatorEntityStub(GenericStub):
    def __init__(self, coordinator):
        self.coordinator = coordinator


class Platform(StrEnum):
    SENSOR = "sensor"
    SELECT = "select"
    BUTTON = "button"
    SWITCH = "switch"
    NUMBER = "number"


class DeviceClass(StrEnum):
    POWER = "power"
    ENERGY = "energy"


class StateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


def effective_area(hass, entry):
    """Represent HA's entity/device/child-device area lookup boundary."""
    if entry.area_id is not None:
        return entry.area_id
    device = hass.devices.async_get(entry.device_id) if entry.device_id else None
    if device is None:
        return None
    if device.area_id is not None:
        return device.area_id
    parent = hass.devices.async_get(getattr(device, "parent_device_id", None))
    return parent.area_id if parent else None


module("homeassistant")
module("homeassistant.components")
module("homeassistant.components.energy")
module("homeassistant.components.energy.data", async_get_manager=None)
module("homeassistant.components.sensor", SensorEntity=type("SensorEntity", (), {}),
       SensorDeviceClass=DeviceClass, SensorStateClass=StateClass)
module("homeassistant.config_entries", ConfigEntry=object)
module("homeassistant.const", Platform=Platform, ATTR_ENTITY_ID="entity_id",
       ATTR_FRIENDLY_NAME="friendly_name", STATE_OFF="off", STATE_ON="on",
       STATE_UNAVAILABLE="unavailable", STATE_UNKNOWN="unknown", PERCENTAGE="%",
       UnitOfPower=SimpleNamespace(WATT="W", KILO_WATT="kW"),
       UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR="kWh"))
module("homeassistant.core", Event=object, EventStateChangedData=dict,
       HomeAssistant=object, State=object, callback=lambda f: f)
module("homeassistant.helpers")
module("homeassistant.helpers.entity_registry", async_get=lambda hass: hass.registry,
       EVENT_ENTITY_REGISTRY_UPDATED="entity_registry_updated",
       async_entries_for_config_entry=lambda registry, entry_id: registry.for_config_entry(entry_id),
       async_get_effective_area_id=effective_area)
module("homeassistant.helpers.event", async_track_state_change_event=None,
       async_track_time_interval=None)
module("homeassistant.helpers.state", async_reproduce_state=None)
module("homeassistant.helpers.storage", Store=GenericStub)
module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=GenericStub,
       CoordinatorEntity=CoordinatorEntityStub)
module("homeassistant.helpers.device_registry", DeviceInfo=dict,
       async_get=lambda hass: hass.devices,
       EVENT_DEVICE_REGISTRY_UPDATED="device_registry_updated")
module("homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object)
module("homeassistant.util", slugify=lambda x: x.lower().replace(" ", "_"))
module("homeassistant.util.unit_conversion", PowerConverter=SimpleNamespace(
    convert=lambda value, unit, _target: value * (1000 if unit == "kW" else 1)))
module("custom_components").__path__ = [str(ROOT / "custom_components")]
module("custom_components.power_fingerprint").__path__ = [str(ROOT / "custom_components" / "power_fingerprint")]

co = importlib.import_module("custom_components.power_fingerprint.coordinator")
models = importlib.import_module("custom_components.power_fingerprint.models")
sensors = importlib.import_module("custom_components.power_fingerprint.sensor")
areas = importlib.import_module("custom_components.power_fingerprint.areas")


class State:
    def __init__(self, entity_id, value="off", **attrs):
        self.entity_id = entity_id
        self.state = value
        self.attributes = {"friendly_name": entity_id, **attrs}
        self.name = attrs.get("friendly_name", entity_id)
        self.last_reported = datetime.now(timezone.utc)
        self.last_updated = self.last_reported


class States:
    def __init__(self, entities):
        self.entities = {state.entity_id: state for state in entities}

    def get(self, eid):
        return self.entities.get(eid)

    def async_all(self):
        raise AssertionError("Summary must never enumerate all HA states")


class Registry:
    entities = {}

    def async_get(self, _eid):
        return None

    def async_get_entity_id(self, _domain, _platform, uid):
        return f"sensor.{uid.replace('.', '_')}"


def engine(eids=("switch.test_light",), outdoor=()):
    hass = SimpleNamespace(
        states=States([State(eid) for eid in eids] + [State("sensor.source", "110", unit_of_measurement="W"), State("sun.sun", "below_horizon")]),
        registry=Registry(),
    )
    entry = SimpleNamespace(entry_id="test", data={"source_power": "sensor.source",
        "tracked_entities": list(eids), "outdoor_block_entities": list(outdoor),
        "auto_export_energy_dashboard": False}, options={})
    result = co.PowerFingerprintCoordinator(hass, entry)
    result.last_source_w = 110.0
    result.last_residual_w = 110.0
    result._initialize_isolation_sessions()
    return result


class Clock:
    def __init__(self, now=100.0):
        self.now = now
        self.wall = datetime.now(timezone.utc)
        self.origin = now
        self.sleep_hook = lambda: None

    async def sleep(self, duration):
        self.now += float(duration)
        self.sleep_hook()

    def datetime_class(self):
        clock = self

        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return (clock.wall + timedelta(seconds=clock.now - clock.origin)).astimezone(tz)

        return ClockDateTime


def evaluate(engine, *, after_times=(106.3, 109.3, 114.3), invalid=(), source_delay=None,
             concurrency=None, old_level=0.0, new_level=1.0, before_w=100.0, after_w=110.0,
             channel="level"):
    clock = Clock()
    eid = engine.tracked_entities[0]
    engine.hass.states.get(eid).state = "on" if new_level else "off"
    engine._sample_buffer = deque(models.Sample(t, before_w) for t in (88, 90, 92, 94, 96, 98))
    added = set()

    def add_samples():
        for t in after_times:
            if t in added or t > clock.now:
                continue
            report = None if source_delay is None else clock.wall.timestamp() + (
                source_delay if t >= 100 + source_delay else -5)
            engine._sample_buffer.append(models.Sample(t, after_w, valid_for_learning=t not in invalid,
                                                      source_reported_at=report))
            added.add(t)

    clock.sleep_hook = add_samples
    if concurrency is not None:
        engine._transition_log.append((concurrency, "switch.other"))
    pending = models.PendingTransition(eid, channel, 100.0, old_level, new_level,
                                      "up" if new_level > old_level else "down")
    with patch.object(co, "time", SimpleNamespace(monotonic=lambda: clock.now)), \
         patch.object(co, "datetime", clock.datetime_class()), \
         patch.object(co, "asyncio", SimpleNamespace(sleep=clock.sleep)):
        asyncio.run(engine._async_evaluate_transition(pending))
    return engine.last_observation, clock


class EdgeTests(unittest.TestCase):
    def test_ordinary_jitter_extends_window_until_three_samples(self):
        obs, clock = evaluate(engine())
        self.assertTrue(obs["accepted"])
        self.assertEqual(obs["after_sample_count"], 3)
        self.assertEqual(obs["value_w"], 10.0)
        self.assertGreater(clock.now, 113)

    def test_guarded_sample_does_not_count_and_later_clean_sample_recovers(self):
        obs, _ = evaluate(engine(), after_times=(106.3, 109.3, 114.3, 117.3), invalid=(106.3,))
        self.assertTrue(obs["accepted"])
        self.assertEqual(obs["after_sample_count"], 3)

    def test_timeout_keeps_insufficient_samples_rejected(self):
        obs, clock = evaluate(engine(), after_times=(106.3, 109.3))
        self.assertFalse(obs["accepted"])
        self.assertEqual(obs["reason"], "insufficient_samples")
        self.assertLessEqual(clock.now, 125)

    def test_concurrent_change_in_extended_window_is_rejected(self):
        obs, _ = evaluate(engine(), concurrency=114)
        self.assertEqual(obs["reason"], "concurrent_tracked_transition")

    def test_concurrent_change_in_pre_window_is_rejected(self):
        obs, _ = evaluate(engine(), concurrency=89)
        self.assertEqual(obs["reason"], "concurrent_tracked_transition")

    def test_polling_source_waits_for_post_event_report(self):
        obs, clock = evaluate(engine(), after_times=tuple(range(106, 123, 2)), source_delay=15)
        self.assertTrue(obs["accepted"])
        self.assertGreaterEqual(clock.now, 120)
        self.assertEqual(obs["after_sample_count"], 3)

    def test_source_that_never_reports_again_is_rejected(self):
        obs, _ = evaluate(engine(), after_times=tuple(range(106, 125, 2)), source_delay=60)
        self.assertEqual(obs["reason"], "source_not_refreshed")
        self.assertEqual(obs["after_sample_count"], 0)

    def test_wrong_sign_not_rescued_by_extra_time(self):
        obs, _ = evaluate(engine(), after_w=90)
        self.assertEqual(obs["reason"], "wrong_step_direction")

    def test_small_edge_not_automatically_promoted(self):
        obs, _ = evaluate(engine(), after_w=102)
        self.assertEqual(obs["reason"], "step_too_small")

    def test_symmetric_cycle_produces_a_rich_session(self):
        eng = engine()
        obs, _ = evaluate(eng)
        self.assertTrue(obs["accepted"])
        eng.isolation_sessions["switch.test_light"].started_ts = 0.0
        obs, _ = evaluate(eng, old_level=1, new_level=0, before_w=110, after_w=100)
        self.assertTrue(obs["accepted"])
        model = eng.models["switch.test_light"]
        self.assertEqual(model.validated_sessions, 1)
        self.assertEqual(sum(map(len, model.session_fingerprints.values())), 1)

    def test_calibration_dwell_covers_adaptive_edge_timeout(self):
        eng = engine()
        eng.settle_seconds = eng.post_window_seconds = 0
        self.assertGreater(eng._calibration_dwell_seconds(), 6 + eng._edge_sample_timeout_seconds())

    def test_climate_edge_still_accepts_valid_compressor_step(self):
        obs, _ = evaluate(engine(("climate.thermostat",)), after_w=500, channel="heating")
        self.assertTrue(obs["accepted"])
        self.assertEqual(obs["value_w"], 400)

    def test_climate_edge_keeps_small_control_board_step_rejected(self):
        obs, _ = evaluate(engine(("climate.thermostat",)), after_w=110, channel="heating")
        self.assertFalse(obs["accepted"])
        self.assertEqual(obs["reason"], "climate_step_too_small")


class CalibrationTests(unittest.TestCase):
    def test_untrained_placeholder_is_export_ready_and_remains_a_training_candidate(self):
        eng = engine()
        profile = eng.device_profiles["switch.test_light"]
        profile.fixed_power_mode = "placeholder"
        profile.fixed_power_w = 10.0
        self.assertTrue(eng.device_energy_dashboard_ready("switch.test_light"))
        self.assertEqual(eng.calibration_training_candidates(), ["switch.test_light"])
        self.assertEqual(profile.fixed_power_w, 10.0)
        self.assertEqual(profile.fixed_power_mode, "placeholder")

    def test_placeholder_exposes_actual_learning_blockers(self):
        eng = engine()
        profile = eng.device_profiles["switch.test_light"]
        profile.fixed_power_mode = "placeholder"
        profile.fixed_power_w = 10.0
        readiness = eng.energy_dashboard_readiness_assessment("switch.test_light")
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["reasons"], [])
        self.assertFalse(readiness["learned_ready"])
        self.assertIn("insufficient_validated_evidence", readiness["learning_reasons"])
        self.assertIn("insufficient_rich_sessions", readiness["learning_reasons"])

    def test_placeholder_with_mature_learned_profile_leaves_training_queue(self):
        eng = engine()
        profile = eng.device_profiles["switch.test_light"]
        profile.fixed_power_mode = "placeholder"
        profile.fixed_power_w = 10.0
        eng.models["switch.test_light"].isolated_energy_kwh = 1.0
        eng.models["switch.test_light"].validated_sessions = 3
        with patch.object(eng, "_energy_profile_quality_assessment", return_value={
            "ready": True, "native": False, "rich_session_count": 3, "reasons": []
        }):
            self.assertEqual(eng.calibration_training_candidates(), [])
            self.assertTrue(eng.energy_dashboard_readiness_assessment("switch.test_light")["learned_ready"])

    def test_locked_fixed_device_remains_outside_training_even_if_unlearned(self):
        eng = engine()
        profile = eng.device_profiles["switch.test_light"]
        profile.fixed_power_mode = "locked"
        profile.fixed_power_w = 10.0
        self.assertTrue(eng.device_energy_dashboard_ready("switch.test_light"))
        self.assertFalse(eng.energy_dashboard_readiness_assessment("switch.test_light")["learned_ready"])
        self.assertEqual(eng.calibration_training_candidates(), [])

    def test_three_sessions_do_not_skip_a_failed_multilevel_profile(self):
        eng = engine(("light.dimmer", "switch.ready"))
        eng._calibration_rich_session_count = lambda _eid: 3
        eng.device_energy_dashboard_ready = lambda eid: eid == "switch.ready"
        self.assertEqual(eng.calibration_training_candidates(), ["light.dimmer"])

    def test_fixed_locked_device_is_not_calibrated(self):
        eng = engine()
        eng.device_profiles["switch.test_light"].fixed_power_mode = "locked"
        eng.device_profiles["switch.test_light"].fixed_power_w = 10.0
        self.assertEqual(eng.calibration_training_candidates(), [])

    def test_ready_retraining_device_is_still_a_candidate(self):
        eng = engine()
        eng.device_profiles["switch.test_light"].lifecycle_state = "retraining"
        eng.device_energy_dashboard_ready = lambda _eid: True
        self.assertEqual(eng.calibration_training_candidates(), ["switch.test_light"])


class OutdoorTests(unittest.TestCase):
    def setUp(self):
        self.eids = ("switch.outdoor_a", "switch.outdoor_b")
        self.eng = engine(self.eids, outdoor=self.eids)

    def test_unchanged_pattern_refreshes_baseline_after_hours(self):
        self.eng._record_outdoor_plateau(100, [1, 1], 300, 0.5)
        self.eng._record_outdoor_plateau(10000, [1, 1], 220, 0.5)
        self.eng._record_outdoor_plateau(10050, [0, 0], 200, 0.5)
        eq = self.eng._outdoor_ensemble_equations[-1]
        self.assertEqual(eq["dy_w"], -20)

    def test_old_plateau_is_not_used_for_a_new_equation(self):
        self.eng._record_outdoor_plateau(100, [1, 1], 220, 0.5)
        self.eng._record_outdoor_plateau(400, [0, 0], 200, 0.5)
        self.assertEqual(self.eng._outdoor_ensemble_equations, [])

    def test_unrelated_device_transition_rejects_equation(self):
        self.eng._record_outdoor_plateau(100, [1, 1], 220, 0.5)
        self.eng._transition_log.append((115, "switch.indoor"))
        self.eng._record_outdoor_plateau(140, [0, 0], 200, 0.5)
        self.assertEqual(self.eng._outdoor_ensemble_equations, [])
        self.assertEqual(self.eng._outdoor_ensemble_rejected_equations["concurrent_nonblock_transition"], 1)

    def test_meter_transition_rejects_equation(self):
        self.eng._record_outdoor_plateau(100, [1, 1], 220, 0.5)
        self.eng._meter_change_log.append((115, "sensor.appliance", 500))
        self.eng._record_outdoor_plateau(140, [0, 0], 200, 0.5)
        self.assertEqual(self.eng._outdoor_ensemble_equations, [])

    def test_missing_member_is_not_treated_as_off(self):
        self.eng.hass.states.get(self.eids[0]).state = "unavailable"
        self.eng._update_outdoor_block(100, 200, {})
        self.assertEqual(self.eng.outdoor_block_power_w, 0)
        self.assertEqual(self.eng._outdoor_block_mode, "member_unavailable")
        self.assertEqual(self.eng._outdoor_ensemble_equations, [])

    def test_plateau_refresh_uses_only_recent_samples(self):
        for eid in self.eids:
            self.eng.hass.states.get(eid).state = "on"
        for now in range(100, 130, 2):
            self.eng._update_outdoor_block(now, 300, {})
        for now in range(10000, 10030, 2):
            self.eng._update_outdoor_block(now, 220, {})
        self.assertEqual(self.eng._outdoor_ensemble_last_plateau["power_w"], 220)
        self.assertTrue(all(t >= 10016 for t, _w in self.eng._outdoor_block_samples))

    def test_together_switching_does_not_invent_individual_readiness(self):
        self.eng._outdoor_ensemble_equations = [
            {"dx": [1, 1], "dy_w": 20, "noise_w": 0.5, "ts": f"test-{i}"}
            for i in range(20)]
        self.eng._refit_outdoor_ensemble()
        self.assertEqual(self.eng._outdoor_ensemble_rank, 1)
        self.assertFalse(self.eng._outdoor_ensemble_mature)


class OutdoorUpgradeTests(unittest.TestCase):
    def restore(self, members, *, explicit=None):
        initial = engine(("switch.outdoor_a", "switch.outdoor_b"))
        initial.entry.data.pop("outdoor_block_entities")
        if explicit is not None:
            initial.entry.options["outdoor_block_entities"] = explicit
        eng = co.PowerFingerprintCoordinator(initial.hass, initial.entry)
        eng.store.async_load = AsyncMock(return_value={
            "schema_version": 11,
            "outdoor_block": {"entities": members, "energy_kwh": 1.25},
        })
        eng.store.async_save = AsyncMock()
        asyncio.run(eng._async_load())
        return eng

    def test_public_upgrade_recovers_saved_outdoor_group_and_energy(self):
        eng = self.restore(["switch.outdoor_a", "switch.outdoor_b"])
        self.assertEqual(eng.outdoor_block_entities, ["switch.outdoor_a", "switch.outdoor_b"])
        self.assertEqual(eng._outdoor_block_entity_set, set(eng.outdoor_block_entities))
        self.assertEqual(eng._outdoor_block_energy_kwh, 1.25)

    def test_explicit_group_wins_over_previously_saved_members(self):
        eng = self.restore(["switch.outdoor_a"], explicit=["switch.outdoor_b"])
        self.assertEqual(eng.outdoor_block_entities, ["switch.outdoor_b"])

    def test_explicit_empty_group_stays_disabled_after_upgrade(self):
        eng = self.restore(["switch.outdoor_a"], explicit=[])
        self.assertEqual(eng.outdoor_block_entities, [])

    def test_recovered_group_deduplicates_and_ignores_removed_sources(self):
        eng = self.restore(["switch.outdoor_a", "switch.removed", "switch.outdoor_a"])
        self.assertEqual(eng.outdoor_block_entities, ["switch.outdoor_a"])

    def test_new_install_has_no_household_specific_outdoor_defaults(self):
        eng = self.restore([])
        self.assertEqual(eng.outdoor_block_entities, [])


class SummaryTests(unittest.TestCase):
    def test_summary_distinguishes_fixed_export_from_learned_profiles(self):
        eng = engine(("switch.learning", "switch.placeholder", "switch.locked"))
        for eid, mode in (("switch.placeholder", "placeholder"), ("switch.locked", "locked")):
            eng.device_profiles[eid].fixed_power_mode = mode
            eng.device_profiles[eid].fixed_power_w = 10.0
        summary = eng._live_isolation_summary(["switch.placeholder", "switch.locked"])
        self.assertEqual(summary["ready_count"], 2)
        self.assertEqual(summary["learned_ready_count"], 0)
        self.assertEqual(summary["not_yet_learned_count"], 3)
        self.assertEqual(summary["placeholder_count"], 1)
        self.assertEqual(summary["locked_count"], 1)
        self.assertEqual(summary["blocker_counts"]["insufficient_validated_evidence"], 1)
        self.assertEqual(summary["learning_blocker_counts"]["insufficient_validated_evidence"], 3)
        eng.data = eng.snapshot()
        attrs = sensors.LearningStatusSensor(eng).extra_state_attributes
        self.assertEqual(attrs["fixed_placeholder_count"], 1)
        self.assertEqual(attrs["learning_blocker_counts"]["insufficient_validated_evidence"], 3)
        attrs = eng.device_isolation_attributes("switch.placeholder")
        self.assertTrue(attrs["energy_dashboard_ready"])
        self.assertFalse(attrs["learned_profile_ready"])
        self.assertIn("insufficient_validated_evidence", attrs["learning_readiness_reasons"])

    def test_sampler_records_report_timestamps_without_counting_duplicates(self):
        eng = engine()
        now = co.time.monotonic()
        eng._append_source_sample(now)
        eng._append_source_sample(now + 2)
        self.assertEqual(len(eng._source_report_times), 1)
        self.assertEqual(eng._sample_buffer[-1].source_reported_at,
                         eng.hass.states.get("sensor.source").last_reported.timestamp())
        eng.hass.states.get("sensor.source").last_reported += timedelta(seconds=60)
        eng._append_source_sample(now + 60)
        self.assertEqual(eng._source_report_interval_seconds(), 60)

    def test_sensor_platform_registers_one_summary(self):
        eng = engine(("switch.a", "switch.b"))
        eng.entry.runtime_data = eng
        entities = []
        asyncio.run(sensors.async_setup_entry(eng.hass, eng.entry, entities.extend))
        self.assertEqual(sum(isinstance(entity, sensors.LiveIsolationSummarySensor) for entity in entities), 1)
        self.assertEqual(sum(isinstance(entity, sensors.IsolatedPowerSensor) for entity in entities), 2)

    def test_summary_is_stable_when_only_watts_and_samples_change(self):
        eng = engine(("switch.a", "switch.b"))
        first = eng.snapshot()["live_isolation_summary"]
        eng.last_source_w = eng.last_residual_w = 900
        eng._sample_buffer.append(models.Sample(co.time.monotonic(), 900))
        second = eng.snapshot()["live_isolation_summary"]
        self.assertEqual(first, second)
        sensor = sensors.LiveIsolationSummarySensor(eng)
        eng.async_set_updated_data(eng.snapshot())
        self.assertEqual(sensor.extra_state_attributes, second)

    def test_only_configured_sources_are_counted_and_missing_source_is_visible(self):
        eng = engine(("switch.a", "switch.b"))
        eng.hass.states.entities["sensor.unrelated"] = State("sensor.unrelated", "999")
        eng.hass.states.get("switch.a").state = "unavailable"
        eng.isolation_sessions["switch.b"].status = "tracking"
        result = eng._live_isolation_summary([])
        self.assertEqual(result["total_count"], 2)
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["unavailable_count"], 1)

    def test_source_report_count_does_not_count_timer_duplicates(self):
        eng = engine()
        now = co.time.monotonic()
        wall = datetime.now(timezone.utc).timestamp()
        eng._sample_buffer = deque(models.Sample(now - i, 100, source_reported_at=wall - 5)
                                   for i in range(20))
        self.assertEqual(eng.snapshot()["source_reports_last_60s"], 1)

    def test_slow_source_extends_edge_and_calibration_waits(self):
        eng = engine()
        eng._source_report_times.extend((1000, 1060, 1120))
        self.assertEqual(eng._source_report_interval_seconds(), 60)
        self.assertEqual(eng._edge_sample_timeout_seconds(), 68)
        self.assertGreaterEqual(eng._calibration_dwell_seconds(), 77)
        obs, clock = evaluate(eng, after_times=tuple(range(106, 173, 2)), source_delay=55)
        self.assertTrue(obs["accepted"])
        self.assertGreaterEqual(clock.now, 160)

    def test_source_wait_remains_bounded(self):
        eng = engine()
        eng._source_report_times.extend((1000, 1300, 1600))
        self.assertEqual(eng._edge_sample_timeout_seconds(), 90)


class RegistryBus:
    def __init__(self):
        self.listeners = {}

    def async_listen(self, name, handler):
        self.listeners.setdefault(name, []).append(handler)
        return lambda: self.listeners[name].remove(handler)

    def fire(self, name, **data):
        for handler in tuple(self.listeners.get(name, ())):
            handler(SimpleNamespace(data=data))


class AreaRegistry:
    def __init__(self, bus):
        self.bus = bus
        self.entities = {}
        self.updates = []
        self.scans = 0

    def async_get(self, reference):
        return self.entities.get(reference) or next(
            (row for row in self.entities.values() if row.id == reference), None)

    def for_config_entry(self, entry_id):
        self.scans += 1
        return [row for row in self.entities.values() if row.config_entry_id == entry_id]

    def add(self, entity_id, *, key=None, platform="test_source", config_entry_id="source",
            unique_id=None, area_id=None, device_id=None, disabled_by=None):
        row = SimpleNamespace(entity_id=entity_id, id=key or f"row_{entity_id}",
            platform=platform, config_entry_id=config_entry_id,
            unique_id=unique_id or entity_id, area_id=area_id,
            device_id=device_id, disabled_by=disabled_by)
        self.entities[entity_id] = row
        self.bus.fire("entity_registry_updated", action="create", entity_id=entity_id)
        return row

    def async_update_entity(self, entity_id, **changes):
        row = self.entities[entity_id]
        old_values = {}
        for key, value in changes.items():
            attribute = "entity_id" if key == "new_entity_id" else key
            old_values[attribute] = getattr(row, attribute)
            setattr(row, attribute, value)
        if row.entity_id != entity_id:
            self.entities.pop(entity_id)
            self.entities[row.entity_id] = row
        self.updates.append((row.entity_id, changes))
        event = dict(action="update", entity_id=row.entity_id, changes=old_values)
        if row.entity_id != entity_id:
            event["old_entity_id"] = entity_id
        self.bus.fire("entity_registry_updated", **event)
        return row


class DeviceRegistry:
    def __init__(self, bus):
        self.bus = bus
        self.devices = {}

    def async_get(self, device_id, **_kwargs):
        return self.devices.get(device_id)

    def add(self, device_id, area_id=None, **attrs):
        device = SimpleNamespace(id=device_id, area_id=area_id, **attrs)
        self.devices[device_id] = device
        self.bus.fire("device_registry_updated", action="create", device_id=device_id)
        return device

    def update(self, device_id, **changes):
        device = self.devices[device_id]
        old_values = {key: getattr(device, key) for key in changes}
        for key, value in changes.items():
            setattr(device, key, value)
        self.bus.fire("device_registry_updated", action="update", device_id=device_id,
                      changes=old_values)


class AreaInheritanceTests(unittest.TestCase):
    def setUp(self):
        bus = RegistryBus()
        self.hass = SimpleNamespace(bus=bus, registry=AreaRegistry(bus), devices=DeviceRegistry(bus))
        self.registry = self.hass.registry
        self.hass.devices.add("lamp", "living_room")
        self.source = self.registry.add("switch.lamp", key="source_uuid", device_id="lamp")
        self.targets = [self.add_sensor(suffix) for suffix in (
            "isolated_power", "isolated_energy", "estimated_power", "estimated_energy")]

    def add_sensor(self, suffix, source_key="source_uuid", **attrs):
        attrs.setdefault("area_id", "old_area")
        attrs.setdefault("disabled_by", "integration" if suffix.startswith("estimated") else None)
        return self.registry.add(f"sensor.custom_{source_key}_{suffix}", platform="power_fingerprint",
            config_entry_id="test", unique_id=f"test_{source_key}_{suffix}", **attrs)

    def start(self):
        return areas.async_setup_area_inheritance(self.hass, "test")

    def assert_areas(self, expected):
        self.assertEqual([target.area_id for target in self.targets], [expected] * 4)

    def test_backfills_existing_and_disabled_sensors_with_only_area_updates(self):
        originals = [vars(target).copy() for target in self.targets]
        self.start()
        self.assert_areas("living_room")
        for original, target in zip(originals, self.targets):
            original["area_id"] = "living_room"
            self.assertEqual(vars(target), original)
        self.assertTrue(all(set(change) == {"area_id"} for _eid, change in self.registry.updates))
        self.assertEqual(len(self.registry.updates), 4)

    def test_follows_parent_device_area_moves_and_removal(self):
        self.start()
        self.hass.devices.update("lamp", area_id="bedroom")
        self.assert_areas("bedroom")
        self.hass.devices.update("lamp", area_id=None)
        self.assert_areas(None)

    def test_source_area_override_and_follow_device_again(self):
        self.source.area_id = "office"
        self.start()
        self.assert_areas("office")
        self.hass.devices.update("lamp", area_id="bedroom")
        self.assert_areas("office")
        self.registry.async_update_entity("switch.lamp", area_id=None)
        self.assert_areas("bedroom")

    def test_entity_without_device_uses_its_own_area(self):
        self.source.device_id = None
        self.source.area_id = "office"
        self.start()
        self.assert_areas("office")

    def test_child_device_inherits_parent_and_follows_child_override(self):
        self.hass.devices.add("strip", "kitchen")
        self.hass.devices.add("socket", parent_device_id="strip")
        self.source.device_id = "socket"
        self.start()
        self.assert_areas("kitchen")
        self.hass.devices.update("strip", area_id="garage")
        self.assert_areas("garage")
        self.hass.devices.update("socket", area_id="office")
        self.assert_areas("office")
        self.hass.devices.update("strip", area_id="kitchen")
        self.assert_areas("office")
        self.hass.devices.update("socket", area_id=None)
        self.assert_areas("kitchen")

    def test_source_device_reassignment_switches_watched_devices(self):
        self.hass.devices.add("other", "garage")
        self.start()
        self.registry.async_update_entity("switch.lamp", device_id="other")
        self.assert_areas("garage")
        scans = self.registry.scans
        self.hass.devices.update("lamp", area_id="office")
        self.assertEqual(self.registry.scans, scans)
        self.hass.devices.update("other", area_id="bedroom")
        self.assert_areas("bedroom")

    def test_new_sensor_and_source_renames_keep_registry_identity(self):
        self.start()
        target = self.add_sensor("isolated_energy", area_id=None)
        self.assertEqual(target.area_id, "living_room")
        self.registry.async_update_entity(target.entity_id, new_entity_id="sensor.renamed_energy")
        self.registry.async_update_entity("switch.lamp", new_entity_id="switch.renamed_lamp")
        self.hass.devices.update("lamp", area_id="garage")
        self.assertEqual(target.entity_id, "sensor.renamed_energy")
        self.assertEqual(target.area_id, "garage")

    def test_late_registry_source_and_device_get_inherited_area(self):
        late = self.add_sensor("isolated_power", source_key="switch.late", area_id=None)
        self.start()
        self.assertIsNone(late.area_id)
        self.registry.add("switch.late", key="late_uuid", device_id="late_device")
        self.assertIsNone(late.area_id)
        self.hass.devices.add("late_device", "office")
        self.assertEqual(late.area_id, "office")

    def test_historical_untracked_sensor_is_backfilled(self):
        self.registry.add("light.previous_selection", key="previous_uuid", area_id="hall")
        historical = self.add_sensor("estimated_energy", source_key="previous_uuid")
        self.start()
        self.assertEqual(historical.area_id, "hall")

    def test_removed_source_clears_stale_area(self):
        self.start()
        self.registry.entities.pop("switch.lamp")
        self.hass.bus.fire("entity_registry_updated", action="remove", entity_id="switch.lamp")
        self.assert_areas(None)

    def test_global_other_entry_and_other_platform_sensors_are_untouched(self):
        global_sensor = self.registry.add("sensor.pf_total", platform="power_fingerprint",
            config_entry_id="test", unique_id="test_fingerprinted_power", area_id="hub_area")
        other_entry = self.registry.add("sensor.other_entry", platform="power_fingerprint",
            config_entry_id="other", unique_id="test_source_uuid_isolated_power", area_id="custom")
        other_platform = self.registry.add("sensor.other_platform", platform="powercalc",
            config_entry_id="test", unique_id="test_source_uuid_isolated_power", area_id="custom")
        self.start()
        self.assertEqual(global_sensor.area_id, "hub_area")
        self.assertEqual(other_entry.area_id, "custom")
        self.assertEqual(other_platform.area_id, "custom")

    def test_unrelated_registry_and_state_events_do_no_work(self):
        self.start()
        scans = self.registry.scans
        self.hass.devices.add("unrelated", "elsewhere")
        self.registry.add("sensor.unrelated", area_id="elsewhere")
        self.hass.devices.update("unrelated", area_id="office")
        self.registry.async_update_entity("sensor.unrelated", area_id="office")
        self.source.name = "Old name"
        self.registry.async_update_entity("switch.lamp", name="New name")
        self.hass.bus.fire("state_changed", entity_id="sensor.house_power")
        self.assertEqual(self.registry.scans, scans)

    def test_repeat_setup_is_idempotent_and_listeners_unsubscribe(self):
        unsubscribe = self.start()
        updates = len(self.registry.updates)
        unsubscribe()
        unsubscribe = self.start()
        self.assertEqual(len(self.registry.updates), updates)
        unsubscribe()
        self.assertTrue(all(not listeners for listeners in self.hass.bus.listeners.values()))
        self.hass.devices.update("lamp", area_id="office")
        self.assert_areas("living_room")

    def test_post_setup_and_stop_manage_area_listener_lifecycle(self):
        eng = engine()
        eng.use_energy_meters = eng.auto_export_energy = False
        eng._async_sync_energy_dashboard = AsyncMock()
        eng._async_reconcile_powercalc_handoffs = AsyncMock()
        eng.store.async_save = AsyncMock()
        unsubscribe = Mock()
        with patch.object(co, "async_setup_area_inheritance", return_value=unsubscribe) as setup:
            asyncio.run(eng.async_post_setup())
        setup.assert_called_once_with(eng.hass, eng.entry.entry_id)
        asyncio.run(eng.async_stop())
        unsubscribe.assert_called_once_with()


@unittest.skipUnless(os.environ.get("PF_DIAGNOSTICS"), "Set PF_DIAGNOSTICS to replay a diagnostics file")
class DiagnosticsReplayTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path(os.environ["PF_DIAGNOSTICS"]).read_text())["data"]
        self.eng = engine(self.data["configuration"]["tracked_entities"],
                          outdoor=self.data["state"]["outdoor_block_entities"])
        self.eng.models = {eid: models.DeviceModel.from_dict(eid, value)
                           for eid, value in self.data["models"].items()}
        self.eng.device_profiles = {eid: models.DeviceProfile.from_dict(eid, value)
                                   for eid, value in self.data["device_profiles"].items()}
        for eid, detail in self.data["tracked_details"].items():
            state = self.eng.hass.states.get(eid)
            state.state = detail["state"]
            state.attributes["friendly_name"] = detail["friendly_name"]
            if "insufficient_multi_level_evidence" in detail["isolation_attributes"]["readiness_reasons"]:
                state.attributes["supported_color_modes"] = ["brightness"]
        self.eng._initialize_isolation_sessions()

    def test_model_roundtrip_preserves_energy_and_evidence(self):
        for eid, old in self.data["models"].items():
            new = self.eng.models[eid].as_dict()
            for key in ("isolated_energy_kwh", "energy_kwh", "validated_sessions", "session_fingerprints"):
                self.assertEqual(new[key], old[key], (eid, key))

    def test_startup_upgrade_preserves_fixed_settings_energy_and_outdoor_members(self):
        initial = engine(self.data["configuration"]["tracked_entities"])
        initial.entry.data.pop("outdoor_block_entities")
        eng = co.PowerFingerprintCoordinator(initial.hass, initial.entry)
        outdoor = self.data["state"]["outdoor_block_entities"]
        stored = {
            "schema_version": 11,
            "models": self.data["models"],
            "device_profiles": self.data["device_profiles"],
            "outdoor_block": {
                "entities": outdoor,
                "energy_kwh": self.data["state"].get("outdoor_block_energy_kwh", 0.0),
            },
        }
        eng.store.async_load = AsyncMock(return_value=stored)
        eng.store.async_save = AsyncMock()
        asyncio.run(eng._async_load())
        self.assertEqual(eng.outdoor_block_entities, outdoor)
        for eid, original in self.data["models"].items():
            restored = eng.models[eid].as_dict()
            for key in ("energy_kwh", "isolated_energy_kwh", "session_fingerprints", "validated_sessions"):
                self.assertEqual(restored[key], original[key], (eid, key))
        for eid, original in self.data["device_profiles"].items():
            restored = eng.device_profiles[eid].as_dict()
            for key in ("fixed_power_mode", "fixed_power_w"):
                self.assertEqual(restored.get(key), original.get(key), (eid, key))

    def test_counts_and_priors_match_uploaded_isolation_attributes(self):
        ready = self.data["state"]["energy_dashboard_ready_entities"]
        for eid in self.data["state"]["energy_dashboard_exported_entities"]:
            _power, energy = self.eng._canonical_sensor_entity_ids(eid)
            self.eng._exported_energy_entities.add(energy)
        result = self.eng._live_isolation_summary(ready)
        expected_priors = sum(d["isolation_attributes"]["sanity_prior_usable"]
                              for d in self.data["tracked_details"].values())
        self.assertEqual(result["total_count"], len(self.data["configuration"]["tracked_entities"]))
        self.assertEqual(result["ready_count"], len(ready))
        self.assertEqual(result["exported_count"], len(self.data["state"]["energy_dashboard_exported_entities"]))
        self.assertEqual(result["prior_count"], expected_priors)
        unavailable = sum(detail["state"] in {"unknown", "unavailable"}
                          for detail in self.data["tracked_details"].values())
        self.assertEqual(result["unavailable_count"], unavailable)

    def test_untrained_safe_placeholders_from_diagnostics_remain_trainable(self):
        candidates = set(self.eng.calibration_training_candidates())
        safe = {eid for eid, _name in self.eng.calibration_options()}
        for eid, profile in self.eng.device_profiles.items():
            if (eid in safe and profile.fixed_power_mode == "placeholder"
                    and not profile.powercalc_exported
                    and self.eng._calibration_rich_session_count(eid) == 0
                    and self.eng.models[eid].validated_sessions == 0
                    and self.eng._soft_evidence_summary(eid)["evidence_units"] < 3):
                self.assertIn(eid, candidates)


if __name__ == "__main__":
    unittest.main()
