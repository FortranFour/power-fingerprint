# Power Fingerprint brand artwork

The integration's original **Bolt C** mark combines a blue fingerprint with
a lightning bolt cut through the center. Keep that negative-space bolt when
updating the artwork; a generic fingerprint is not the same brand mark.

`icon-source.png` is the restored artwork with a transparent background,
extracted from the original project artwork using image generation.
`icon.png` (256 pixels) and `icon@2x.png` (512 pixels) are downscaled copies.
Both work on light and dark backgrounds.

The canonical blue is **#0076FD** (RGB 0, 118, 253), sampled from the original
dashboard icon's most frequent saturated stroke color. All visible pixels in
the source and both bundled icons use this RGB value. Antialiasing is carried
by the alpha channel. Version 0.7.8 corrects the darker color introduced during
background extraction in 0.7.7, preserving all three alpha masks exactly.

Version 0.7.7 replaces the generic Material Design fingerprint introduced in
0.7.6. Its unrelated SVG and attribution have been removed along with it.
