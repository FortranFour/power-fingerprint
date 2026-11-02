# Power Fingerprint 0.7.4

Fixed-wattage placeholders no longer prevent training. An undertrained device
can keep its provisional energy accounting and still enter the calibration
batch. Locked profiles remain outside active calibration.

The Learning status sensor and Live isolation summary expose measured learning
progress separately from energy export readiness. Per-device power sensors
include `learned_profile_ready` and `learning_readiness_reasons`.

This is the first repository-ready distribution. HACS installs
`custom_components/power_fingerprint` in place of an existing manual copy.
Keep the existing integration entry and model storage. No model reset is needed.
The saved outdoor group is recovered when previous private builds supplied it
implicitly; explicit options always take precedence.

The package preserves the integration domain, entity unique-ID format, model
schema, fixed wattages and modes, fingerprints, and cumulative energy. Existing
card resources need no changes for this release.

Validation: 58 offline regression checks passed with a private diagnostic replay,
including an upgrade replay that preserved fixed settings, per-device energy,
validated-session counts, fingerprints, and outdoor membership. Registry and
event boundaries are represented by test stand-ins. Live household learning
results still require a new run after installation.

Read [the migration guide](MIGRATION.md) before adopting HACS for a manual install.
