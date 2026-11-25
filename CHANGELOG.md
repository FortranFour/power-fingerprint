# Changelog

## Unreleased

- Stop using concurrent whole-house transitions, including estimated remainders
  after subtracting other loads, as evidence for one device's fixed wattage.
  Repeated group changes could otherwise mark small lights as learned and ready
  for Powercalc at hundreds of watts without a validated session.
- Migrate model storage to schema 12, removing that contaminated soft evidence
  and its pairing credit. Recheck affected profiles while preserving accepted
  edges, validated sessions, cumulative energy, and manual fixed settings.
- Keep clean small-step learning available and prevent a concurrent edge from
  forming a pair with a clean edge. Add offline regressions and diagnostic replay.

## 0.7.8

- Match the icon's blue to the original dashboard artwork: #0076FD. Correct
  the darker shade introduced by background extraction in 0.7.7.
- Preserve the fingerprint, lightning-bolt cutout, dimensions, and exact alpha
  masks in the source image and both integration icons.
- No runtime, learning, calibration, or energy-accounting changes.

## 0.7.7

- Restore the original blue fingerprint with its central lightning-bolt cutout.
  Replace the generic fingerprint introduced in 0.7.6 with transparent 256- and
  512-pixel images based on the original project artwork.
- Retain the restored source image and document the bolt as part of the brand
  identity. Remove the replaced generic SVG and its attribution.
- No learning, calibration, model, or energy-accounting changes.

## 0.7.6

- Bundle a transparent cyan fingerprint integration icon at 256 and 512 pixels
  in the local brand directory, so Home Assistant can show it on the integrations
  page. The same icon works on light and dark backgrounds.
- Publish explicitly named release commits only after the Python and HACS
  validation jobs pass, using the tested commit and matching release notes.
- Explain residual power, missing-meter uncertainty, and standby limitations
  in the learning diagnostics guide. No learning or energy-accounting changes.

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
