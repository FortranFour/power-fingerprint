"""Statistical and session models used by Power Fingerprint."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, median
from typing import Any
import math


def utc_iso() -> str:
    """Return current UTC time as ISO text."""
    return datetime.now(timezone.utc).isoformat()


def robust_median(values: list[float]) -> float | None:
    """Return median or None."""
    return float(median(values)) if values else None


def mad(values: list[float], center: float | None = None) -> float:
    """Median absolute deviation."""
    if not values:
        return 0.0
    if center is None:
        center = float(median(values))
    return float(median([abs(v - center) for v in values]))


def normalize_fingerprint_channel_alias(channel: str | None) -> str | None:
    """Normalize equivalent HVAC activity labels without merging real stages.

    Residual-change-point sessions can begin with labels such as ``inferred_cool``
    and later be refined by ``hvac_action`` to ``cooling``. Those labels describe
    the same operating mode and must not make a rich fingerprint look mixed.
    Distinct stages such as heating versus defrosting intentionally remain distinct.
    """
    if not channel:
        return None
    key = str(channel).lower()
    if key.startswith("inferred_"):
        key = key.removeprefix("inferred_")
    aliases = {
        "cool": "cooling",
        "cooling": "cooling",
        "heat": "heating",
        "heating": "heating",
        "fan": "fan",
        "fan_only": "fan",
        "dry": "drying",
        "drying": "drying",
        "preheat": "preheating",
        "preheating": "preheating",
        "defrost": "defrosting",
        "defrosting": "defrosting",
    }
    return aliases.get(key, key)


@dataclass(slots=True)
class Sample:
    """One residual power sample."""

    ts: float
    residual_w: float
    corrected_w: float = 0.0
    valid_for_learning: bool = True
    source_reported_at: float | None = None


@dataclass(slots=True)
class Observation:
    """One accepted or rejected fingerprint observation."""

    ts: str
    entity_id: str
    channel: str
    value_w: float
    delta_w: float
    level_delta: float
    direction: str
    before_w: float
    after_w: float
    noise_w: float
    accepted: bool
    reason: str
    culprit_meter: str | None = None
    edge_source: str = "entity_state"
    before_sample_count: int | None = None
    after_sample_count: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize observation."""
        return {
            "ts": self.ts,
            "entity_id": self.entity_id,
            "channel": self.channel,
            "value_w": round(self.value_w, 3),
            "delta_w": round(self.delta_w, 3),
            "level_delta": round(self.level_delta, 4),
            "direction": self.direction,
            "before_w": round(self.before_w, 3),
            "after_w": round(self.after_w, 3),
            "noise_w": round(self.noise_w, 3),
            "accepted": self.accepted,
            "reason": self.reason,
            "edge_source": self.edge_source,
            **({"before_sample_count": self.before_sample_count} if self.before_sample_count is not None else {}),
            **({"after_sample_count": self.after_sample_count} if self.after_sample_count is not None else {}),
            **({"culprit_meter": self.culprit_meter} if self.culprit_meter else {}),
        }


def percentile(values: list[float], q: float) -> float | None:
    """Return a linearly interpolated percentile for 0 <= q <= 1."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    q = max(0.0, min(1.0, float(q)))
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _downsample_waveform(samples: list[tuple[float, float]], max_points: int = 180) -> list[list[float]]:
    """Return an evenly sampled, compact [seconds, watts] waveform."""
    if not samples:
        return []
    if len(samples) <= max_points:
        chosen = samples
    else:
        last = len(samples) - 1
        indexes = sorted({round(i * last / (max_points - 1)) for i in range(max_points)})
        chosen = [samples[i] for i in indexes]
    return [[round(float(t), 2), round(float(w), 2)] for t, w in chosen]


@dataclass(slots=True)
class SessionFingerprint:
    """Feature-rich description of one validated isolated device session.

    The compact electrical waveform is deliberately retained so future versions
    can extract additional features without requiring the user to retrain the
    appliance. Runtime recognition still uses the unified isolation engine; this
    record is evidence, not a second fixed-wattage model.
    """

    completed_at: str
    started_at: str | None
    channel: str
    validation_channel: str | None
    duration_s: float
    activity_source: str
    device_type: str
    sample_count: int
    start_step_w: float | None = None
    stop_step_w: float | None = None
    edge_symmetry_error_w: float | None = None
    steady_median_w: float | None = None
    steady_mean_w: float | None = None
    steady_min_w: float | None = None
    steady_max_w: float | None = None
    steady_p10_w: float | None = None
    steady_p90_w: float | None = None
    steady_range_w: float | None = None
    steady_mad_w: float | None = None
    variability_pct: float | None = None
    peak_w: float | None = None
    overshoot_w: float | None = None
    ramp_time_s: float | None = None
    ramp_slope_w_per_s: float | None = None
    cycling_detected: bool = False
    cycle_count: int = 0
    median_cycle_period_s: float | None = None
    duty_cycle_pct: float | None = None
    cycling_amplitude_w: float | None = None
    baseline_adjustments: int = 0
    overlap_seen: bool = False
    quality: str = "validated"
    setting_events: list[dict[str, Any]] = field(default_factory=list)
    annotation: dict[str, Any] = field(default_factory=dict)
    waveform: list[list[float]] = field(default_factory=list)

    def as_dict(self, *, include_waveform: bool = True) -> dict[str, Any]:
        """Serialize the session fingerprint."""
        data = {
            "completed_at": self.completed_at,
            "started_at": self.started_at,
            "channel": self.channel,
            "validation_channel": self.validation_channel,
            "duration_s": round(self.duration_s, 3),
            "activity_source": self.activity_source,
            "device_type": self.device_type,
            "sample_count": self.sample_count,
            "start_step_w": self.start_step_w,
            "stop_step_w": self.stop_step_w,
            "edge_symmetry_error_w": self.edge_symmetry_error_w,
            "steady_median_w": self.steady_median_w,
            "steady_mean_w": self.steady_mean_w,
            "steady_min_w": self.steady_min_w,
            "steady_max_w": self.steady_max_w,
            "steady_p10_w": self.steady_p10_w,
            "steady_p90_w": self.steady_p90_w,
            "steady_range_w": self.steady_range_w,
            "steady_mad_w": self.steady_mad_w,
            "variability_pct": self.variability_pct,
            "peak_w": self.peak_w,
            "overshoot_w": self.overshoot_w,
            "ramp_time_s": self.ramp_time_s,
            "ramp_slope_w_per_s": self.ramp_slope_w_per_s,
            "cycling_detected": self.cycling_detected,
            "cycle_count": self.cycle_count,
            "median_cycle_period_s": self.median_cycle_period_s,
            "duty_cycle_pct": self.duty_cycle_pct,
            "cycling_amplitude_w": self.cycling_amplitude_w,
            "baseline_adjustments": self.baseline_adjustments,
            "overlap_seen": self.overlap_seen,
            "quality": self.quality,
            "setting_events": list(self.setting_events),
            "annotation": dict(self.annotation),
        }
        if include_waveform:
            data["waveform"] = list(self.waveform)
        return data

    def compact_dict(self) -> dict[str, Any]:
        """Serialize a bounded summary suitable for HA state attributes."""
        data = self.as_dict(include_waveform=False)
        events = data.pop("setting_events", [])
        sequence: list[str] = []
        for event in events:
            channel = event.get("channel") if isinstance(event, dict) else None
            if channel and (not sequence or sequence[-1] != str(channel)):
                sequence.append(str(channel))
        data["setting_event_count"] = len(events)
        data["mode_sequence"] = sequence[:12]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionFingerprint | None":
        """Deserialize a persisted session fingerprint."""
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                completed_at=str(data.get("completed_at") or ""),
                started_at=(str(data["started_at"]) if data.get("started_at") else None),
                channel=str(data.get("channel") or "level"),
                validation_channel=(str(data["validation_channel"]) if data.get("validation_channel") else None),
                duration_s=float(data.get("duration_s", 0.0) or 0.0),
                activity_source=str(data.get("activity_source") or "unknown"),
                device_type=str(data.get("device_type") or "generic_load"),
                sample_count=int(data.get("sample_count", 0) or 0),
                start_step_w=_optional_float(data.get("start_step_w")),
                stop_step_w=_optional_float(data.get("stop_step_w")),
                edge_symmetry_error_w=_optional_float(data.get("edge_symmetry_error_w")),
                steady_median_w=_optional_float(data.get("steady_median_w")),
                steady_mean_w=_optional_float(data.get("steady_mean_w")),
                steady_min_w=_optional_float(data.get("steady_min_w")),
                steady_max_w=_optional_float(data.get("steady_max_w")),
                steady_p10_w=_optional_float(data.get("steady_p10_w")),
                steady_p90_w=_optional_float(data.get("steady_p90_w")),
                steady_range_w=_optional_float(data.get("steady_range_w")),
                steady_mad_w=_optional_float(data.get("steady_mad_w")),
                variability_pct=_optional_float(data.get("variability_pct")),
                peak_w=_optional_float(data.get("peak_w")),
                overshoot_w=_optional_float(data.get("overshoot_w")),
                ramp_time_s=_optional_float(data.get("ramp_time_s")),
                ramp_slope_w_per_s=_optional_float(data.get("ramp_slope_w_per_s")),
                cycling_detected=bool(data.get("cycling_detected", False)),
                cycle_count=int(data.get("cycle_count", 0) or 0),
                median_cycle_period_s=_optional_float(data.get("median_cycle_period_s")),
                duty_cycle_pct=_optional_float(data.get("duty_cycle_pct")),
                cycling_amplitude_w=_optional_float(data.get("cycling_amplitude_w")),
                baseline_adjustments=int(data.get("baseline_adjustments", 0) or 0),
                overlap_seen=bool(data.get("overlap_seen", False)),
                quality=str(data.get("quality") or "validated"),
                setting_events=list(data.get("setting_events") or []),
                annotation=dict(data.get("annotation") or {}),
                waveform=[list(v) for v in (data.get("waveform") or []) if isinstance(v, (list, tuple)) and len(v) == 2],
            )
        except (TypeError, ValueError):
            return None


def _optional_float(value: Any) -> float | None:
    """Convert a persisted optional numeric field."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def build_session_fingerprint(
    *,
    completed_at: str,
    started_at: str | None,
    channel: str,
    validation_channel: str | None,
    duration_s: float,
    activity_source: str,
    device_type: str,
    samples: list[tuple[float, float]],
    start_step_w: float | None,
    stop_step_w: float | None,
    edge_symmetry_error_w: float | None,
    baseline_adjustments: int,
    overlap_seen: bool,
    quality: str,
    setting_events: list[dict[str, Any]] | None = None,
    annotation: dict[str, Any] | None = None,
) -> SessionFingerprint:
    """Extract reusable electrical features from one validated isolated session."""
    cleaned = sorted(
        (max(0.0, float(t)), max(0.0, float(w)))
        for t, w in samples
        if math.isfinite(float(t)) and math.isfinite(float(w))
    )
    values = [w for _, w in cleaned]

    if len(values) >= 5:
        trim = max(1, len(values) // 10)
        core = values[trim:-trim] or values
    else:
        core = values

    steady_median = robust_median(core)
    steady_mean = float(mean(core)) if core else None
    steady_min = min(core) if core else None
    steady_max = max(core) if core else None
    p10 = percentile(core, 0.10)
    p90 = percentile(core, 0.90)
    steady_range = (steady_max - steady_min) if steady_min is not None and steady_max is not None else None
    steady_mad = mad(core, steady_median) if core and steady_median is not None else None
    variability = (
        (steady_mad / steady_median * 100.0)
        if steady_mad is not None and steady_median is not None and steady_median > 0.5
        else None
    )
    peak = max(values) if values else None
    overshoot = max(0.0, peak - steady_median) if peak is not None and steady_median is not None else None

    ramp_time: float | None = None
    ramp_slope: float | None = None
    if cleaned and steady_median is not None and steady_median > 0:
        tolerance = max(10.0, steady_median * 0.10)
        required = min(3, len(cleaned))
        for idx in range(0, max(1, len(cleaned) - required + 1)):
            window = cleaned[idx : idx + required]
            if len(window) == required and all(abs(w - steady_median) <= tolerance for _, w in window):
                ramp_time = max(0.0, window[0][0])
                break
        if ramp_time and ramp_time > 0.0:
            first_w = cleaned[0][1]
            ramp_slope = (steady_median - first_w) / ramp_time

    cycling_detected = False
    cycle_count = 0
    median_cycle_period: float | None = None
    duty_cycle: float | None = None
    cycling_amplitude: float | None = None
    if len(cleaned) >= 8:
        smooth: list[tuple[float, float]] = []
        for idx, (t, _w) in enumerate(cleaned):
            lo = max(0, idx - 1)
            hi = min(len(cleaned), idx + 2)
            smooth.append((t, float(median([w for _, w in cleaned[lo:hi]]))))
        smooth_values = [w for _, w in smooth]
        low_band = percentile(smooth_values, 0.15)
        high_band = percentile(smooth_values, 0.85)
        if low_band is not None and high_band is not None and steady_median is not None:
            cycling_amplitude = max(0.0, high_band - low_band)
            if cycling_amplitude >= max(25.0, steady_median * 0.18):
                threshold = (low_band + high_band) / 2.0
                states = [w >= threshold for _, w in smooth]
                starts: list[float] = []
                prior = False
                for (t, _), is_high in zip(smooth, states, strict=False):
                    if is_high and not prior:
                        starts.append(t)
                    prior = is_high
                if len(starts) >= 2:
                    cycling_detected = True
                    cycle_count = len(starts)
                    periods = [b - a for a, b in zip(starts, starts[1:], strict=False) if b > a]
                    median_cycle_period = robust_median(periods)
                    duty_cycle = 100.0 * sum(1 for state in states if state) / len(states)

    symmetry_error = edge_symmetry_error_w
    if symmetry_error is None and start_step_w is not None and stop_step_w is not None:
        symmetry_error = abs(float(start_step_w) - float(stop_step_w))

    def r(value: float | None, digits: int = 3) -> float | None:
        return round(float(value), digits) if value is not None and math.isfinite(float(value)) else None

    return SessionFingerprint(
        completed_at=completed_at,
        started_at=started_at,
        channel=channel,
        validation_channel=validation_channel,
        duration_s=max(0.0, float(duration_s)),
        activity_source=activity_source,
        device_type=device_type,
        sample_count=len(cleaned),
        start_step_w=r(start_step_w),
        stop_step_w=r(stop_step_w),
        edge_symmetry_error_w=r(symmetry_error),
        steady_median_w=r(steady_median),
        steady_mean_w=r(steady_mean),
        steady_min_w=r(steady_min),
        steady_max_w=r(steady_max),
        steady_p10_w=r(p10),
        steady_p90_w=r(p90),
        steady_range_w=r(steady_range),
        steady_mad_w=r(steady_mad),
        variability_pct=r(variability, 2),
        peak_w=r(peak),
        overshoot_w=r(overshoot),
        ramp_time_s=r(ramp_time, 2),
        ramp_slope_w_per_s=r(ramp_slope),
        cycling_detected=cycling_detected,
        cycle_count=cycle_count,
        median_cycle_period_s=r(median_cycle_period, 2),
        duty_cycle_pct=r(duty_cycle, 2),
        cycling_amplitude_w=r(cycling_amplitude),
        baseline_adjustments=int(baseline_adjustments),
        overlap_seen=bool(overlap_seen),
        quality=quality,
        setting_events=list(setting_events or []),
        annotation=dict(annotation or {}),
        waveform=_downsample_waveform(cleaned),
    )


def summarize_session_fingerprints(sessions: list[SessionFingerprint]) -> dict[str, Any]:
    """Return a robust per-channel summary of validated rich sessions."""
    if not sessions:
        return {"session_count": 0}

    def med(field_name: str) -> float | None:
        vals = [float(v) for s in sessions if (v := getattr(s, field_name)) is not None]
        value = robust_median(vals)
        return round(value, 3) if value is not None else None

    cycling_sessions = [s for s in sessions if s.cycling_detected]
    return {
        "session_count": len(sessions),
        "latest_completed_at": sessions[-1].completed_at,
        "median_duration_s": med("duration_s"),
        "median_start_step_w": med("start_step_w"),
        "median_stop_step_w": med("stop_step_w"),
        "median_steady_power_w": med("steady_median_w"),
        "median_steady_p10_w": med("steady_p10_w"),
        "median_steady_p90_w": med("steady_p90_w"),
        "median_steady_range_w": med("steady_range_w"),
        "median_steady_mad_w": med("steady_mad_w"),
        "median_variability_pct": med("variability_pct"),
        "median_peak_w": med("peak_w"),
        "median_overshoot_w": med("overshoot_w"),
        "median_ramp_time_s": med("ramp_time_s"),
        "median_ramp_slope_w_per_s": med("ramp_slope_w_per_s"),
        "cycling_session_count": len(cycling_sessions),
        "median_cycling_amplitude_w": (
            robust_median([s.cycling_amplitude_w for s in cycling_sessions if s.cycling_amplitude_w is not None])
            if cycling_sessions else None
        ),
        "median_cycle_period_s": (
            robust_median([s.median_cycle_period_s for s in cycling_sessions if s.median_cycle_period_s is not None])
            if cycling_sessions else None
        ),
        "median_duty_cycle_pct": (
            robust_median([s.duty_cycle_pct for s in cycling_sessions if s.duty_cycle_pct is not None])
            if cycling_sessions else None
        ),
    }


@dataclass(slots=True)
class ChannelModel:
    """Robust statistical fingerprint for one operating channel."""

    observations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def accepted_values(self) -> list[float]:
        """Accepted positive values."""
        return [
            float(o["value_w"])
            for o in self.observations
            if o.get("accepted") and float(o.get("value_w", 0)) > 0
        ]

    @property
    def estimate_w(self) -> float | None:
        """Robust central estimate."""
        return robust_median(self.accepted_values)

    @property
    def deviation_w(self) -> float | None:
        """Robust spread converted to a normal-like sigma."""
        values = self.accepted_values
        if not values:
            return None
        center = float(median(values))
        return 1.4826 * mad(values, center)

    @property
    def sample_count(self) -> int:
        """Accepted sample count."""
        return len(self.accepted_values)

    @property
    def direction_counts(self) -> dict[str, int]:
        """Accepted transition counts by direction."""
        counts = {"up": 0, "down": 0}
        for obs in self.observations:
            if obs.get("accepted") and obs.get("direction") in counts:
                counts[str(obs["direction"])] += 1
        return counts

    def confidence(self, min_samples: int) -> tuple[str, int]:
        """Return confidence label and approximate 0-100 score."""
        n = self.sample_count
        directions = self.direction_counts
        if n < min_samples:
            return "learning", min(20, int(20 * n / max(1, min_samples)))
        est = self.estimate_w or 0.0
        dev = self.deviation_w or 0.0
        rel = dev / est if est > 1 else 1.0
        both_directions = directions["up"] > 0 and directions["down"] > 0
        if n >= 12 and rel <= 0.15 and both_directions:
            return "high", min(100, 80 + min(20, n - 12))
        if n >= 6 and rel <= 0.30 and both_directions:
            return "medium", min(79, 55 + min(24, n - 6) * 2)
        return "low", min(54, 25 + n * 3)

    def plausible(self, value_w: float) -> bool:
        """Reject a gross outlier once a model is established."""
        values = self.accepted_values
        if len(values) < 4:
            return True
        center = float(median(values))
        spread = max(1.4826 * mad(values, center), center * 0.08, 3.0)
        return abs(value_w - center) <= max(4.0 * spread, center * 0.45, 15.0)

    def add(self, observation: Observation, max_observations: int) -> None:
        """Append observation and bound history."""
        self.observations.append(observation.as_dict())
        if len(self.observations) > max_observations:
            del self.observations[: len(self.observations) - max_observations]

    def as_dict(self) -> dict[str, Any]:
        """Serialize."""
        return {"observations": self.observations}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ChannelModel":
        """Deserialize."""
        if not data:
            return cls()
        obs = data.get("observations", [])
        return cls(observations=list(obs) if isinstance(obs, list) else [])


@dataclass(slots=True)
class DeviceModel:
    """Persistent evidence and energy state for one tracked HA entity.

    ``energy_kwh`` remains the old fixed-model accumulator for provenance only.
    ``isolated_energy_kwh`` is the canonical 0.4 cumulative energy total.
    """

    entity_id: str
    model_type: str = "auto"
    channels: dict[str, ChannelModel] = field(default_factory=dict)
    energy_kwh: float = 0.0
    isolated_energy_kwh: float = 0.0
    migration_energy_kwh: float = 0.0
    migration_source: str | None = None
    validated_sessions: int = 0
    rejected_sessions: int = 0
    historical_validated_cycles: int = 0
    legacy_cycle_values_w: list[float] = field(default_factory=list)
    validated_channel_sessions: dict[str, int] = field(default_factory=dict)
    rejected_channel_sessions: dict[str, int] = field(default_factory=dict)
    historical_channel_validations: dict[str, int] = field(default_factory=dict)
    session_fingerprints: dict[str, list[SessionFingerprint]] = field(default_factory=dict)
    last_session_rejection_reason: str | None = None
    last_session_rejection_channel: str | None = None
    last_session_rejection_at: str | None = None
    # 0.6.6: quality-weighted low-power evidence. These observations preserve
    # useful state-confirmed edges that are too weak or mildly confounded for
    # the strict live-isolation path, without promoting them to accepted edges.
    soft_evidence_observations: list[dict[str, Any]] = field(default_factory=list)
    paired_evidence_count: int = 0
    pending_soft_edge: dict[str, Any] | None = None

    def channel(self, key: str) -> ChannelModel:
        """Get/create channel."""
        return self.channels.setdefault(key, ChannelModel())

    def add_session_fingerprint(self, key: str, fingerprint: SessionFingerprint, limit: int = 40) -> None:
        """Append one validated rich session while bounding persistent history."""
        sessions = self.session_fingerprints.setdefault(key, [])
        sessions.append(fingerprint)
        if len(sessions) > limit:
            del sessions[: len(sessions) - limit]

    def fingerprint_summaries(self) -> dict[str, dict[str, Any]]:
        """Return compact summaries for every learned operating channel."""
        return {key: summarize_session_fingerprints(value) for key, value in self.session_fingerprints.items()}

    def latest_session_fingerprint(self) -> SessionFingerprint | None:
        """Return the most recently completed rich session across channels."""
        sessions = [session for values in self.session_fingerprints.values() for session in values]
        return max(sessions, key=lambda item: item.completed_at, default=None)

    def as_dict(self) -> dict[str, Any]:
        """Serialize."""
        return {
            "entity_id": self.entity_id,
            "model_type": self.model_type,
            "channels": {key: value.as_dict() for key, value in self.channels.items()},
            "energy_kwh": self.energy_kwh,
            "isolated_energy_kwh": self.isolated_energy_kwh,
            "migration_energy_kwh": self.migration_energy_kwh,
            "migration_source": self.migration_source,
            "validated_sessions": self.validated_sessions,
            "rejected_sessions": self.rejected_sessions,
            "historical_validated_cycles": self.historical_validated_cycles,
            "legacy_cycle_values_w": self.legacy_cycle_values_w[-100:],
            "validated_channel_sessions": self.validated_channel_sessions,
            "rejected_channel_sessions": self.rejected_channel_sessions,
            "historical_channel_validations": self.historical_channel_validations,
            # Keep rich summaries for up to 40 sessions/channel, but retain full
            # 180-point waveforms only for the newest five. Older sessions still
            # preserve every derived feature needed for profile fitting/auditing.
            "session_fingerprints": {
                key: [
                    session.as_dict(include_waveform=index >= max(0, len(recent) - 5))
                    for index, session in enumerate(recent)
                ]
                for key, sessions in self.session_fingerprints.items()
                for recent in [sessions[-40:]]
            },
            "last_session_rejection_reason": self.last_session_rejection_reason,
            "last_session_rejection_channel": self.last_session_rejection_channel,
            "last_session_rejection_at": self.last_session_rejection_at,
            "soft_evidence_observations": self.soft_evidence_observations[-80:],
            "paired_evidence_count": self.paired_evidence_count,
            "pending_soft_edge": self.pending_soft_edge,
        }

    @classmethod
    def from_dict(cls, entity_id: str, data: dict[str, Any] | None) -> "DeviceModel":
        """Deserialize both 0.3 and 0.4 model records."""
        if not data:
            return cls(entity_id=entity_id)
        return cls(
            entity_id=entity_id,
            model_type=str(data.get("model_type", "auto")),
            channels={
                key: ChannelModel.from_dict(value)
                for key, value in (data.get("channels") or {}).items()
            },
            energy_kwh=float(data.get("energy_kwh", 0.0) or 0.0),
            isolated_energy_kwh=float(data.get("isolated_energy_kwh", 0.0) or 0.0),
            migration_energy_kwh=float(data.get("migration_energy_kwh", 0.0) or 0.0),
            migration_source=(str(data["migration_source"]) if data.get("migration_source") else None),
            validated_sessions=int(data.get("validated_sessions", 0) or 0),
            rejected_sessions=int(data.get("rejected_sessions", 0) or 0),
            historical_validated_cycles=int(data.get("historical_validated_cycles", 0) or 0),
            legacy_cycle_values_w=[
                float(v) for v in (data.get("legacy_cycle_values_w") or []) if v is not None
            ],
            validated_channel_sessions={str(k): int(v or 0) for k, v in (data.get("validated_channel_sessions") or {}).items()},
            rejected_channel_sessions={str(k): int(v or 0) for k, v in (data.get("rejected_channel_sessions") or {}).items()},
            historical_channel_validations={str(k): int(v or 0) for k, v in (data.get("historical_channel_validations") or {}).items()},
            session_fingerprints={
                str(key): [
                    session
                    for item in (values or [])
                    if (session := SessionFingerprint.from_dict(item)) is not None
                ]
                for key, values in (data.get("session_fingerprints") or {}).items()
                if isinstance(values, list)
            },
            last_session_rejection_reason=(
                str(data["last_session_rejection_reason"])
                if data.get("last_session_rejection_reason")
                else None
            ),
            last_session_rejection_channel=(
                str(data["last_session_rejection_channel"])
                if data.get("last_session_rejection_channel")
                else None
            ),
            last_session_rejection_at=(
                str(data["last_session_rejection_at"])
                if data.get("last_session_rejection_at")
                else None
            ),
            soft_evidence_observations=[
                dict(item) for item in (data.get("soft_evidence_observations") or [])
                if isinstance(item, dict)
            ][-80:],
            paired_evidence_count=int(data.get("paired_evidence_count", 0) or 0),
            pending_soft_edge=(
                dict(data["pending_soft_edge"])
                if isinstance(data.get("pending_soft_edge"), dict)
                else None
            ),
        )


@dataclass(slots=True)
class DeviceProfile:
    """Persistent user-facing classification metadata for one tracked device.

    Profiles do not select a different inference engine. They are deliberately
    metadata-only in 0.4.x so future discovery/reclassification UI can evolve
    without forcing existing users to rebuild their curated tracked list.
    """

    entity_id: str
    device_type: str = "generic_load"
    classification_source: str = "domain_default"
    selection_source: str = "configured"
    user_confirmed: bool = False
    heavy_load: bool = False
    source_kind: str = "ha_entity"
    source_ref: str | None = None
    powercalc_exported: bool = False
    powercalc_model_id: str | None = None
    powercalc_exported_at: str | None = None
    powercalc_power_w: float | None = None
    lifecycle_state: str = "learning"
    lifecycle_updated_at: str | None = None
    powercalc_profile_type: str | None = None
    powercalc_calibration_points: list[list[float]] = field(default_factory=list)
    powercalc_power_sensor_id: str | None = None
    powercalc_energy_sensor_id: str | None = None
    powercalc_handoff_status: str = "none"
    powercalc_active: bool = False
    powercalc_energy_seed_kwh: float | None = None
    powercalc_energy_seeded_at: str | None = None
    retraining_started_at: str | None = None
    audit_observations: int = 0
    audit_good: int = 0
    audit_bad: int = 0
    audit_consecutive_bad: int = 0
    audit_last_error_pct: float | None = None
    audit_last_error_w: float | None = None
    audit_last_at: str | None = None
    # 0.7.1 explicit fixed-power override. ``placeholder`` continues learning
    # and yields to better validated/live evidence; ``locked`` is authoritative
    # and suppresses further learning for this device.
    fixed_power_w: float | None = None
    fixed_power_mode: str = "automatic"
    fixed_power_updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "device_type": self.device_type,
            "classification_source": self.classification_source,
            "selection_source": self.selection_source,
            "user_confirmed": self.user_confirmed,
            "heavy_load": self.heavy_load,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref or self.entity_id,
            "powercalc_exported": self.powercalc_exported,
            "powercalc_model_id": self.powercalc_model_id,
            "powercalc_exported_at": self.powercalc_exported_at,
            "powercalc_power_w": self.powercalc_power_w,
            "lifecycle_state": self.lifecycle_state,
            "lifecycle_updated_at": self.lifecycle_updated_at,
            "powercalc_profile_type": self.powercalc_profile_type,
            "powercalc_calibration_points": [list(point) for point in self.powercalc_calibration_points],
            "powercalc_power_sensor_id": self.powercalc_power_sensor_id,
            "powercalc_energy_sensor_id": self.powercalc_energy_sensor_id,
            "powercalc_handoff_status": self.powercalc_handoff_status,
            "powercalc_active": self.powercalc_active,
            "powercalc_energy_seed_kwh": self.powercalc_energy_seed_kwh,
            "powercalc_energy_seeded_at": self.powercalc_energy_seeded_at,
            "retraining_started_at": self.retraining_started_at,
            "audit_observations": self.audit_observations,
            "audit_good": self.audit_good,
            "audit_bad": self.audit_bad,
            "audit_consecutive_bad": self.audit_consecutive_bad,
            "audit_last_error_pct": self.audit_last_error_pct,
            "audit_last_error_w": self.audit_last_error_w,
            "audit_last_at": self.audit_last_at,
            "fixed_power_w": self.fixed_power_w,
            "fixed_power_mode": self.fixed_power_mode,
            "fixed_power_updated_at": self.fixed_power_updated_at,
        }

    @classmethod
    def from_dict(cls, entity_id: str, data: dict[str, Any] | None) -> "DeviceProfile":
        data = data or {}
        return cls(
            entity_id=entity_id,
            device_type=str(data.get("device_type", "generic_load")),
            classification_source=str(data.get("classification_source", "domain_default")),
            selection_source=str(data.get("selection_source", "configured")),
            user_confirmed=bool(data.get("user_confirmed", False)),
            heavy_load=bool(data.get("heavy_load", False)),
            source_kind=str(data.get("source_kind", "ha_entity")),
            source_ref=(str(data["source_ref"]) if data.get("source_ref") else entity_id),
            powercalc_exported=bool(data.get("powercalc_exported", False)),
            powercalc_model_id=(str(data["powercalc_model_id"]) if data.get("powercalc_model_id") else None),
            powercalc_exported_at=(str(data["powercalc_exported_at"]) if data.get("powercalc_exported_at") else None),
            powercalc_power_w=(float(data["powercalc_power_w"]) if data.get("powercalc_power_w") is not None else None),
            lifecycle_state=str(data.get("lifecycle_state") or ("graduated_to_powercalc" if data.get("powercalc_exported") else "learning")),
            lifecycle_updated_at=(str(data["lifecycle_updated_at"]) if data.get("lifecycle_updated_at") else None),
            powercalc_profile_type=(str(data["powercalc_profile_type"]) if data.get("powercalc_profile_type") else ("fixed" if data.get("powercalc_exported") else None)),
            powercalc_calibration_points=[
                [float(point[0]), float(point[1])]
                for point in (data.get("powercalc_calibration_points") or [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ],
            powercalc_power_sensor_id=(str(data["powercalc_power_sensor_id"]) if data.get("powercalc_power_sensor_id") else None),
            powercalc_energy_sensor_id=(str(data["powercalc_energy_sensor_id"]) if data.get("powercalc_energy_sensor_id") else None),
            powercalc_handoff_status=str(data.get("powercalc_handoff_status") or ("pending_restart" if data.get("powercalc_exported") else "none")),
            powercalc_active=bool(data.get("powercalc_active", False)),
            powercalc_energy_seed_kwh=(float(data["powercalc_energy_seed_kwh"]) if data.get("powercalc_energy_seed_kwh") is not None else None),
            powercalc_energy_seeded_at=(str(data["powercalc_energy_seeded_at"]) if data.get("powercalc_energy_seeded_at") else None),
            retraining_started_at=(str(data["retraining_started_at"]) if data.get("retraining_started_at") else None),
            audit_observations=int(data.get("audit_observations", 0) or 0),
            audit_good=int(data.get("audit_good", 0) or 0),
            audit_bad=int(data.get("audit_bad", 0) or 0),
            audit_consecutive_bad=int(data.get("audit_consecutive_bad", 0) or 0),
            audit_last_error_pct=(float(data["audit_last_error_pct"]) if data.get("audit_last_error_pct") is not None else None),
            audit_last_error_w=(float(data["audit_last_error_w"]) if data.get("audit_last_error_w") is not None else None),
            audit_last_at=(str(data["audit_last_at"]) if data.get("audit_last_at") else None),
            fixed_power_w=(float(data["fixed_power_w"]) if data.get("fixed_power_w") is not None else None),
            fixed_power_mode=str(data.get("fixed_power_mode") or "automatic"),
            fixed_power_updated_at=(str(data["fixed_power_updated_at"]) if data.get("fixed_power_updated_at") else None),
        )


def prune_unpaired_latent_start_observations(model: DeviceModel) -> list[dict[str, Any]]:
    """Remove pre-0.4.4 latent HVAC starts that never acquired a closing edge.

    0.4.0-0.4.3 persisted a residual-change-point UP observation as soon as a
    latent climate session started. If HA restarted or the session became
    orphaned before a validated close, that provisional edge incorrectly
    remained as accepted training evidence. A completed latent cycle has a
    matching accepted DOWN edge, so pair UP/DOWN observations chronologically
    and remove only unmatched UP edges.
    """
    removed: list[dict[str, Any]] = []
    empty_channels: list[str] = []
    for channel_name, channel in list(model.channels.items()):
        if not channel_name.startswith("inferred_"):
            continue
        unmatched_up: list[int] = []
        for idx, obs in enumerate(channel.observations):
            if not (
                obs.get("accepted")
                and obs.get("edge_source") == "residual_change_point"
                and obs.get("reason") == "accepted_change_point"
            ):
                continue
            direction = str(obs.get("direction", "")).lower()
            if direction == "up":
                unmatched_up.append(idx)
            elif direction == "down" and unmatched_up:
                unmatched_up.pop(0)
        if unmatched_up:
            doomed = set(unmatched_up)
            kept: list[dict[str, Any]] = []
            for idx, obs in enumerate(channel.observations):
                if idx in doomed:
                    removed.append(dict(obs))
                else:
                    kept.append(obs)
            channel.observations = kept
        if not channel.observations:
            empty_channels.append(channel_name)
    for channel_name in empty_channels:
        model.channels.pop(channel_name, None)
    return removed


def prune_malformed_schema5_session_fingerprints(
    model: DeviceModel,
) -> list[tuple[str, SessionFingerprint]]:
    """Remove rich sessions produced by the 0.5.0/0.5.1 stale-start pairing bug.

    A valid 0.5 rich session always receives ``started_at`` when its accepted
    start edge is committed. 0.5.0/0.5.1 could retain a previous accepted
    ``start_edge_w`` while a later start edge was rejected, then pair that stale
    start with a subsequent accepted stop. Those hybrid fingerprints have a
    validated-looking start/stop pair but no ``started_at`` timestamp.

    Only rich-session evidence is removed here. Energy accumulators and ordinary
    edge observations are deliberately left untouched because their trustworthy
    portions cannot be reconstructed safely from the compact stored record.
    """
    removed: list[tuple[str, SessionFingerprint]] = []
    empty_channels: list[str] = []
    for channel_name, sessions in list(model.session_fingerprints.items()):
        kept: list[SessionFingerprint] = []
        for session in sessions:
            malformed = (
                session.started_at is None
                and session.start_step_w is not None
                and session.stop_step_w is not None
                and session.quality.startswith("validated")
            )
            if malformed:
                removed.append((channel_name, session))
            else:
                kept.append(session)
        if kept:
            model.session_fingerprints[channel_name] = kept
        else:
            empty_channels.append(channel_name)
    for channel_name in empty_channels:
        model.session_fingerprints.pop(channel_name, None)

    if removed:
        model.validated_sessions = max(0, model.validated_sessions - len(removed))
        for channel_name, session in removed:
            vchan = session.validation_channel or channel_name
            if vchan in model.validated_channel_sessions:
                model.validated_channel_sessions[vchan] = max(
                    0, model.validated_channel_sessions[vchan] - 1
                )
                if model.validated_channel_sessions[vchan] == 0:
                    model.validated_channel_sessions.pop(vchan, None)
    return removed


@dataclass(slots=True)
class PendingTransition:
    """Tracked entity transition awaiting a post-event window."""

    entity_id: str
    channel: str
    event_ts: float
    old_level: float
    new_level: float
    direction: str
    edge_source: str = "entity_state"
    generation: int = 0


@dataclass(slots=True)
class IsolationSession:
    """Live counterfactual-baseline isolation state for one tracked device."""

    entity_id: str
    active: bool = False
    channel: str | None = None
    baseline_w: float | None = None
    started_ts: float | None = None
    start_edge_w: float | None = None
    raw_power_w: float | None = None
    reported_power_w: float | None = None
    status: str = "off"
    quality: str = "unknown"
    sanity_status: str = "unknown"
    prior_source: str = "device_metadata"
    prior_low_w: float = 0.0
    prior_high_w: float = 5000.0
    pending_direction: str | None = None
    recent_power_w: list[float] = field(default_factory=list)
    last_edge_w: float | None = None
    last_edge_direction: str | None = None
    last_edge_source: str | None = None
    edge_symmetry_status: str = "unknown"
    edge_symmetry_error_w: float | None = None
    baseline_adjustments: int = 0
    overlap_seen: bool = False
    latent_start_candidate_since: float | None = None
    latent_stop_candidate_since: float | None = None
    latent_stop_edge_w: float | None = None
    last_completed_duration_s: float | None = None
    validation_channel: str | None = None
    pending_energy_kwh: float = 0.0
    started_utc: str | None = None
    activity_source: str = "unknown"
    start_edge_accepted: bool = False
    climate_off_seen_since: float | None = None
    feature_samples: list[tuple[float, float]] = field(default_factory=list)
    setting_events: list[dict[str, Any]] = field(default_factory=list)

    def reset_feature_capture(
        self,
        now: float,
        *,
        started_utc: str | None,
        activity_source: str,
        channel: str | None,
        old_level: float | None = None,
        new_level: float | None = None,
    ) -> None:
        """Start a fresh feature waveform for a newly isolated session."""
        self.started_utc = started_utc
        self.activity_source = activity_source
        self.latent_stop_edge_w = None
        self.feature_samples.clear()
        self.setting_events.clear()
        if channel is not None:
            self.note_setting_event(
                now, channel=channel, old_level=old_level, new_level=new_level, source=activity_source, kind="start"
            )

    def note_setting_event(
        self,
        now: float,
        *,
        channel: str | None,
        old_level: float | None = None,
        new_level: float | None = None,
        source: str = "entity_state",
        kind: str = "change",
    ) -> None:
        """Retain a compact operating-mode/setting timeline for this session."""
        rel = max(0.0, now - self.started_ts) if self.started_ts is not None else 0.0
        self.setting_events.append({
            "t_s": round(rel, 2),
            "kind": kind,
            "channel": channel,
            "old_level": old_level,
            "new_level": new_level,
            "source": source,
        })
        if len(self.setting_events) > 100:
            del self.setting_events[: len(self.setting_events) - 100]

    def add_power_sample(self, value_w: float, now: float | None = None, limit: int = 120) -> None:
        """Append recent live power plus a longer bounded feature waveform."""
        value = float(value_w)
        self.recent_power_w.append(value)
        if len(self.recent_power_w) > limit:
            del self.recent_power_w[: len(self.recent_power_w) - limit]
        if now is not None and self.started_ts is not None:
            self.feature_samples.append((max(0.0, now - self.started_ts), value))
            # Bound runtime memory while preserving the entire session span. Each
            # compaction halves temporal resolution instead of discarding the start.
            if len(self.feature_samples) > 3600:
                self.feature_samples[:] = self.feature_samples[::2]

    def clear_feature_capture(self) -> None:
        """Drop runtime-only rich-session capture state."""
        self.feature_samples.clear()
        self.setting_events.clear()
        self.started_utc = None
        self.activity_source = "unknown"
        self.latent_stop_edge_w = None

    @property
    def recent_median_w(self) -> float | None:
        """Return recent live-power median."""
        return robust_median(self.recent_power_w[-15:])


@dataclass(slots=True)
class InverterModel:
    """Legacy 0.3 inverter record retained only for migration compatibility."""

    entity_id: str
    running: bool = False
    candidate_since: float | None = None
    stop_candidate_since: float | None = None
    baseline_w: float | None = None
    started_ts: float | None = None
    current_power_w: float = 0.0
    energy_kwh: float = 0.0
    cycle_values_w: list[float] = field(default_factory=list)
    cycle_samples_w: list[float] = field(default_factory=list)

    @property
    def estimate_w(self) -> float | None:
        return robust_median(self.cycle_values_w)

    @property
    def deviation_w(self) -> float | None:
        if not self.cycle_values_w:
            return None
        center = float(median(self.cycle_values_w))
        return 1.4826 * mad(self.cycle_values_w, center)

    def confidence(self) -> tuple[str, int]:
        n = len(self.cycle_values_w)
        if n < 2:
            return "learning", n * 10
        est = self.estimate_w or 0
        dev = self.deviation_w or 0
        rel = dev / est if est else 1
        if n >= 8 and rel <= 0.25:
            return "high", min(100, 80 + n)
        if n >= 4 and rel <= 0.4:
            return "medium", min(79, 55 + n * 3)
        return "low", min(54, 30 + n * 5)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "energy_kwh": self.energy_kwh,
            "cycle_values_w": self.cycle_values_w[-100:],
        }

    @classmethod
    def from_dict(cls, entity_id: str, data: dict[str, Any] | None) -> "InverterModel":
        data = data or {}
        return cls(
            entity_id=entity_id,
            energy_kwh=float(data.get("energy_kwh", 0.0) or 0.0),
            cycle_values_w=[float(v) for v in data.get("cycle_values_w", []) if v is not None],
        )
