"""Constants for Power Fingerprint."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "power_fingerprint"
NAME = "Power Fingerprint"
VERSION = "0.7.4"
PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
]

CONF_SOURCE_POWER = "source_power"
CONF_TRACKED_ENTITIES = "tracked_entities"
CONF_INVERTER_CLIMATE = "inverter_climate"
CONF_USE_ENERGY_METERS = "use_energy_dashboard_meters"
CONF_AUTO_EXPORT_ENERGY = "auto_export_energy_dashboard"
CONF_ADDITIONAL_METERS = "additional_meters"
CONF_EXCLUDED_METERS = "excluded_meters"
CONF_SETTLE_SECONDS = "settle_seconds"
CONF_PRE_WINDOW_SECONDS = "pre_window_seconds"
CONF_POST_WINDOW_SECONDS = "post_window_seconds"
CONF_MAX_NOISE_W = "max_noise_w"
CONF_MIN_STEP_W = "min_step_w"
CONF_CONCURRENCY_SECONDS = "concurrency_seconds"
CONF_MIN_SAMPLES = "min_samples"
CONF_INVERTER_MIN_STEP_W = "inverter_min_step_w"
CONF_INVERTER_CONFIRM_SECONDS = "inverter_confirm_seconds"
CONF_MAX_OBSERVATIONS = "max_observations"
CONF_SAMPLE_INTERVAL_SECONDS = "sample_interval_seconds"
CONF_METER_GRACE_SECONDS = "meter_grace_seconds"
CONF_METER_REJOIN_SECONDS = "meter_rejoin_seconds"
CONF_CALIBRATION_EXCLUDED_ENTITIES = "calibration_excluded_entities"
CONF_OUTDOOR_BLOCK_ENTITIES = "outdoor_block_entities"

DEFAULT_SETTLE_SECONDS = 35
DEFAULT_PRE_WINDOW_SECONDS = 25
DEFAULT_POST_WINDOW_SECONDS = 20
DEFAULT_MAX_NOISE_W = 30.0
DEFAULT_MIN_STEP_W = 4.0
DEFAULT_CONCURRENCY_SECONDS = 20
DEFAULT_MIN_SAMPLES = 3
DEFAULT_INVERTER_MIN_STEP_W = 150.0
DEFAULT_INVERTER_CONFIRM_SECONDS = 20
DEFAULT_MAX_OBSERVATIONS = 100
DEFAULT_SAMPLE_INTERVAL_SECONDS = 2
DEFAULT_METER_GRACE_SECONDS = 20
DEFAULT_METER_REJOIN_SECONDS = 60
DEFAULT_AUTO_EXPORT_ENERGY = True

# New installs select their outdoor group explicitly. Existing installations
# recover their group from model storage when no explicit option is present.
DEFAULT_OUTDOOR_BLOCK_SETTLE_SECONDS = 90
OUTDOOR_ENSEMBLE_MAX_PER_SIGNATURE = 12
OUTDOOR_ENSEMBLE_SINGLE_MAX_PRIOR_MULTIPLIER = 3.0
OUTDOOR_ENSEMBLE_SINGLE_ABSOLUTE_MARGIN_W = 20.0
OUTDOOR_ENSEMBLE_MULTI_ABSOLUTE_MARGIN_W = 35.0

MANUAL_FIXED_MODE_AUTO = "automatic"
MANUAL_FIXED_MODE_PLACEHOLDER = "placeholder"
MANUAL_FIXED_MODE_LOCKED = "locked"
MANUAL_FIXED_MODES = {
    MANUAL_FIXED_MODE_AUTO,
    MANUAL_FIXED_MODE_PLACEHOLDER,
    MANUAL_FIXED_MODE_LOCKED,
}
MANUAL_FIXED_SUGGESTED_W = {
    "lighting": 10.0,
    "fan": 35.0,
    "electronics": 80.0,
    "refrigerator": 120.0,
    "washer": 500.0,
    "dryer": 3000.0,
    "dishwasher": 1200.0,
    "microwave": 1200.0,
    "toaster": 1000.0,
    "oven_stove": 2500.0,
    "water_heater": 4500.0,
    "ev_charger": 7200.0,
    "hvac": 1500.0,
    "generic_load": 50.0,
}

# Unified live-isolation readiness / latent climate-edge defaults.
DEFAULT_READY_VALIDATED_SESSIONS = 3
DEFAULT_LATENT_CLIMATE_CONFIRM_SECONDS = 20
DEFAULT_LATENT_CLIMATE_MIN_STEP_W = 150.0
MODEL_SCHEMA_VERSION = 11
DEVICE_PROFILE_SCHEMA_VERSION = 4
FINGERPRINT_SCHEMA_VERSION = 1


STORAGE_VERSION = 1
STORAGE_SAVE_DELAY = 15.0

SUPPORTED_TRACKED_DOMAINS = {"light", "switch", "fan", "media_player", "climate"}
SAFE_CALIBRATION_DOMAINS = {"light", "switch", "fan"}

# New-install candidate discovery is generic and device-registry based.
# Existing config entries retain their explicitly configured tracked entities.

# Optional friendly-name overrides; empty in the distributable build.
SOURCE_DISPLAY_NAME_OVERRIDES: dict[str, str] = {}
