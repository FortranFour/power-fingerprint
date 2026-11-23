# Power Fingerprint 0.7.7

Restore the lightning bolt in the integration icon.

Version 0.7.6 accidentally substituted a generic fingerprint for the original
Power Fingerprint artwork. This release restores the blue fingerprint with its
distinctive lightning bolt cut through the center, using the original project
artwork as the reference.

The icon is supplied as transparent 256- and 512-pixel PNGs and works on light
and dark backgrounds. The restored source image is retained with the package.

## Installation

Update to **0.7.7** through HACS and restart Home Assistant. Refresh the browser
if it still displays the previous fingerprint. No model reset or recalibration
is required. Learning, calibration, stored models, and energy accounting are
unchanged.

## Validation

Package metadata, compilation, JSON, PNG dimensions and transparency checked.
All 61 offline regression checks passed with private diagnostic replay enabled.
The release publishes only after the Python and HACS validation jobs pass.
