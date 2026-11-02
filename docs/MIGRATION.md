# Adopt HACS for an existing installation

Keep the existing Power Fingerprint integration entry. HACS replaces code in
`/config/custom_components/power_fingerprint`; the integration's learned model
is stored separately under `/config/.storage/power_fingerprint.<entry_id>`.

## 1. Back up the current installation

In the Home Assistant terminal:

```bash
(
  set -e
  pf_backup="/config/power_fingerprint_backups/pre-hacs-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$pf_backup"
  cp -a /config/custom_components/power_fingerprint "$pf_backup/"
  for pf_store in /config/.storage/power_fingerprint.*; do
    if [ -f "$pf_store" ]; then
      cp -a "$pf_store" "$pf_backup/"
    fi
  done
  printf 'Backup: %s\n' "$pf_backup"
)
```

Confirm the backup contains the integration directory and the model file. A
normal Home Assistant backup is also suitable.

## 2. Download through HACS

After the repository's first release has been published:

1. Open HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/FortranFour/power-fingerprint` as **Integration**.
3. Open Power Fingerprint and download **0.7.4** over the existing manual copy.
4. Restart Home Assistant:

```bash
ha core restart
```

Keep the existing integration entry. HACS needs the first download to establish
the installed version; merely adding the repository does not adopt local files.

## 3. Check the preserved configuration

The tracked list, exclusions, fixed wattages and modes, existing sensor identities,
energy counters, fingerprints, area inheritance, and saved outdoor membership
are retained. The model schema stays at version 11. The update does not start
calibration automatically.

Existing dashboard YAML, `/config/www` resources, and private Powercalc model
files outside the integration directory remain managed in their current locations.

## 4. Collect new training evidence

During a suitable quiet period, start **Calibrate devices needing training** from the
integration's controls. Undertrained placeholders are now included. Unavailable
devices are skipped, explicit exclusions still apply, and locked profiles remain
excluded. Use the new learning counts and a fresh diagnostic download to judge
the run; older saved batch results retain their original dates.

## Future updates and rollback

Publish a new GitHub release with matching integration version metadata. HACS
will offer that release as an update. Download it and restart Home Assistant.

For a rollback before older versions have public releases, restore the integration
directory from the backup and restart. Restore model storage only if needed and
with Home Assistant stopped, so a running integration cannot overwrite it.

HACS adoption does not require deleting the integration entry, removing entities,
or resetting models or energy totals.
