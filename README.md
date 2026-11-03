# Power Fingerprint

Power Fingerprint is an experimental Home Assistant integration that learns
power profiles for unmetered devices by comparing their state changes with
whole-house power after subtracting directly metered loads. It integrates
estimated device energy and can export mature profiles to a private local
Powercalc library.

Version **0.7.5** · Home Assistant **2026.10 or later** · MIT license

## What changed in 0.7.5

The **Learning status** sensor no longer sends its live diagnostics to the
recorder. It publishes about every five seconds, and most of its attributes
(sample counts, report ages, rolling medians, calibration lists) change on every
publish, so each update stored a new copy of the whole attribute block. On a
large installation that reached about 2 GB of database in ten days, enough to
push full backups past cloud size limits.

The sensor's state and a few slowly changing counts are still recorded. Every
attribute remains in the live state, so dashboards and templates are unaffected;
only their history is no longer kept.

See [release notes](docs/RELEASE_0.7.5.md) and the [changelog](CHANGELOG.md).

## Installation with HACS

1. In HACS, open the three-dot menu and choose **Custom repositories**.
2. Add `https://github.com/FortranFour/power-fingerprint`, category **Integration**.
3. Open Power Fingerprint in HACS, download the release, and restart Home Assistant.
4. For a new installation, add **Power Fingerprint** in Settings → Devices & services.

Already have a manual installation? Download through HACS over the same
`custom_components/power_fingerprint` directory and keep the existing integration
entry. Follow the [migration guide](docs/MIGRATION.md) to back up the model first.

HACS manages integration files. Its first download is required even if the
integration is already installed manually. Later published releases appear as
updates in HACS; installing an integration update requires a Home Assistant restart.

## Setup

Choose one whole-house power sensor and the unmetered devices to track. The
supported source domains are lights, switches, fans, media players, and climate
entities. Review the discovery suggestions before accepting them.

Power Fingerprint can subtract directly metered devices from the Energy dashboard;
additional meters and exclusions are configurable. Avoid subtracting the same
physical consumption through both a parent meter and its child meters.

Use integration options to select an outdoor ensemble explicitly. New installations
have no predefined household entities. Existing installations recover the previous
group from saved model data when the option was implicit in older builds.

## Learning and calibration

Learning observes normal use while enabled. Use **Calibrate devices needing training** for
deliberate training of eligible lights, switches, and fans during a quiet period.
The button operates real devices and restores their previous states. Climate
and media-player entities are learned passively and are outside active batch
calibration. A batch never starts automatically on installation or restart.

Small loads, coarse or infrequent meter reports, concurrent activity, and missing
devices can prevent reliable attribution. The integration retains conservative
acceptance and profile-quality requirements. A successful batch attempt collects
evidence; it does not guarantee that a device is ready after one attempt.

- **Automatic:** use the learner's normal runtime behavior.
- **Placeholder:** use an explicit fixed wattage for accounting while continuing
  learning and allowing active calibration when the learned profile needs work.
- **Locked:** use the explicit wattage and exclude the device from active training.

See [learning diagnostics](docs/LEARNING.md) for interpreting progress and blockers.

## Existing dashboards

The integration keeps the `power_fingerprint` domain and its entity unique-ID
format. Existing dashboard references and energy statistics remain associated
with the same sensors when upgrading an existing integration entry.

Per-source power and energy entities follow the source entity's effective area,
including disabled compatibility sensors. A source entity's own area overrides
its device's area. Assign an area to imported entities that have no local device.

Existing dashboard YAML and `/config/www/power-fingerprint-wrap-row.js` remain
user-managed. HACS does not copy this repository's `extras` directory into `www`.
The optional wrapping helper is included in `extras` for manual installation;
it extends an already installed `template-entity-row` resource. Existing users
of that helper do not need to change it for 0.7.5.

The native Live isolation summary sensor supplies the header counts without
whole-house sensor-domain scans.

## Development

```bash
python scripts/check_package.py
python -m unittest discover -s tests -v
```

Tests use stand-ins for Home Assistant boundaries and run without installing
Home Assistant. For private diagnostic replay, set `PF_DIAGNOSTICS` to a local
diagnostics JSON before running the tests. Do not commit the diagnostics.

GitHub Actions validates the source on pushes and pull requests. Maintainers
publish a GitHub release after those checks pass; HACS follows published releases.
See [publishing](docs/PUBLISHING.md).
