# Power Fingerprint 0.7.5

The Learning status sensor no longer fills the recorder database.

It publishes about every five seconds, and most of its attributes are live
diagnostics: sample counts, report ages, rolling residual medians, rejection
tallies, and calibration candidate and result lists. Because they differ on
every publish, the recorder could not reuse a stored attribute row and saved a
new copy of the whole block each time. On a large installation this reached
about 2 GB of `state_attributes` in ten days, a third of the database, and
pushed full backups past cloud size limits.

In 0.7.5 the sensor marks those attributes as unrecorded. Its state (`learning`,
`calibrating`, `paused`, and so on) is still recorded, together with these
slowly changing counts:

- `learned_profile_ready_count`
- `not_yet_learned_count`
- `fixed_placeholder_count`
- `fixed_locked_count`
- `total_meter_count`
- `energy_dashboard_exported_count`
- `powercalc_exported_count`
- `unified_model_version`

Every attribute is still present in the live state, so dashboards, templates
and the wrap-row card are unaffected. Only the history of the unrecorded
attributes is no longer kept.

## After updating

Existing rows stay in the database until they age out under the recorder's
`purge_keep_days`. To reclaim the space at once, run in Developer tools →
Actions:

```yaml
action: recorder.purge_entities
data:
  keep_days: 0
  entity_id:
    - sensor.power_fingerprint_learning_status
```

then `recorder.purge` with `repack: true`. If you excluded the sensor from the
recorder as a workaround, you can remove that exclusion after updating.

No model, profile, or energy data changes. No reset is needed.

Validation: 61 offline regression checks passed, including new checks that the
published attribute list matches the declared list, that volatile diagnostics
are unrecorded, and that the recorded counts stay unchanged when only samples
change.
