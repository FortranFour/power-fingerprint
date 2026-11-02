# Changelog

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
