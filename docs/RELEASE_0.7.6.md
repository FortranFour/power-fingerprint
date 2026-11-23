# Power Fingerprint 0.7.6

Power Fingerprint now supplies its integration icon to Home Assistant.

## Changes

- Bundle transparent cyan fingerprint PNGs at 256 and 512 pixels in
  `custom_components/power_fingerprint/brand/`, with SVG source and attribution.
  Home Assistant uses the same icon on light and dark backgrounds.
- Publish an explicitly named release commit only after the Python regression
  checks, package checks, and HACS validation pass. The release points to that
  exact tested commit and includes its matching release notes.
- Expand the learning guide's explanation of unidentified consumption,
  missing-meter uncertainty, and the limits of learning standby power from
  on/off transitions.

Learning, calibration, energy accounting, stored models, and entity IDs are
unchanged. No model reset or recalibration is required for this update.

## Installation

Update Power Fingerprint to **0.7.6** through HACS and restart Home Assistant.
Refresh the browser if it retains the old missing-icon placeholder.
The icon is included in this release; selecting the development branch is
unnecessary.

## Validation

Package metadata, Python compilation, JSON, and icon dimensions/transparency
were checked. All 61 offline regression checks passed with private diagnostic
replay enabled; private diagnostics are not included in the repository.
These checks do not replace testing on a running Home Assistant instance.
