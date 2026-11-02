# Interpret learning progress

## Export readiness and learned readiness

An explicit fixed wattage permits energy accounting before the integration has
measured a mature profile. **Energy-ready** therefore means the current accounting
method is usable. **Learned-ready** means the underlying measured evidence passes
the learning gates.

In 0.7.4, an untrained placeholder remains eligible for the active batch even
while its energy sensor is already on the Energy dashboard. A fixed locked
profile remains excluded by design.

The Live isolation summary sensor has these additional attributes:

| Attribute | Meaning |
|---|---|
| `learned_ready_count` | Profiles passing the learned-evidence requirements |
| `not_yet_learned_count` | Tracked profiles still failing those requirements |
| `placeholder_count` | Active provisional fixed-wattage profiles |
| `locked_count` | Active fixed profiles intentionally excluded from training |
| `learning_blocker_counts` | Reasons that measured profiles remain incomplete |

The Learning status sensor exposes the same progress as
`learned_profile_ready_count`, `not_yet_learned_count`,
`fixed_placeholder_count`, and `fixed_locked_count`.

Per-source power sensors expose `learned_profile_ready` and
`learning_readiness_reasons`. Existing energy-ready attributes retain their
accounting meaning.

## Common limits

- **Insufficient samples:** too few clean measurements within the edge window.
  Compare actual source reports with residual timer samples; held readings are
  not additional meter reports.
- **Concurrent tracked transition:** another tracked load changed during the
  measurement. A quieter deliberate cycle can provide more useful evidence.
- **Step too small:** the observed electrical change cannot pass the signal
  gate. A fixed measured wattage can remain appropriate for very small loads.
- **Between-session variability:** repeated cycles do not yet agree closely
  enough for a stable profile.
- **Missing outdoor members:** an unavailable light cannot be treated as OFF.
  Resolve availability before expecting ensemble learning for that member.
- **Insufficient outdoor rank:** repeated identical all-on/all-off patterns
  cannot identify individual members. Independent switching patterns are needed.

The source's report interval limits temporal resolution. A faster residual
timer does not make the physical meter report faster. This release retains
the existing timing, noise, concurrency, fit-quality, and symmetry checks.

## Judge a new run

Check the batch timestamp, new fingerprints, validated sessions, per-device
learning reasons, and this-run accepted/rejected observations. A historical
batch summary is not evidence that a new version has been tested live.

Diagnostic replay checks persistence and known decision paths. It does not
reproduce the home's future electrical background or guarantee successful
calibration of every device.
