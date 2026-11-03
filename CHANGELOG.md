# Changelog

## 0.7.5

- Stop recording the Learning status sensor's live diagnostic attributes. It
  publishes about every five seconds and most attributes change each time, so
  every update stored a new attribute row (about 2 GB in ten days on a large
  installation). The state and eight slowly changing counts are still recorded;
  all attributes remain in the live state.
- Add regression checks that keep the recorded and unrecorded attribute lists in
  step with what the sensor publishes.

## 0.7.4

- Keep undertrained fixed-wattage placeholders in the calibration batch queue.
- Report learned readiness and its blockers separately from provisional energy
  export readiness. Add learned, pending, placeholder, and locked summary counts.
- Preserve existing outdoor membership from model storage when upgrading a
  private build with an implicit group; explicit options, including an empty
  group, take precedence.
- Remove household-specific defaults from the distributable source.
- Add HACS metadata, documentation, validation, and publishing support.

## 0.7.3 — previous private distribution

- Backfill per-source power and energy sensor areas and follow source-area moves.
- Preserve entity IDs, statistics identifiers, device links, and energy history.

## 0.7.2 — previous private distribution

- Bound edge waits while adapting to the source report cadence.
- Revisit devices that have sessions but still fail actual profile readiness.
- Refresh outdoor baselines and reject unavailable or confounded members.
- Add a native isolation-summary sensor for efficient dashboard headers.
