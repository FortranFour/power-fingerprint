# Power Fingerprint 0.7.8

Match the integration icon to the original blue.

The source artwork and both integration icons now use **#0076FD** (RGB 0, 118,
253), sampled from the original dashboard icon. This corrects the darker shade
introduced when removing the artwork's background in 0.7.7.

The fingerprint shape, central lightning-bolt cutout, dimensions, and alpha
masks are preserved exactly. Learning, calibration, stored models, and energy
accounting are unchanged.

## Installation

Update to **0.7.8** through HACS, restart Home Assistant, and refresh the browser
if it retains the previous icon. No model reset or recalibration is required.

## Validation

All visible pixels use the specified RGB value. Alpha masks and dimensions
match 0.7.7 byte for byte. Package checks and all 61 offline regression checks
passed with private diagnostic replay enabled. Publication is gated on Python
and HACS validation.
