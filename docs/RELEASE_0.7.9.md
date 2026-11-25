# Power Fingerprint 0.7.9

Correct false individual wattages learned from simultaneous household changes.
Raw concurrent transitions and estimated remainders after subtracting other loads
no longer contribute to individual-device learning or Powercalc graduation.

## After updating

1. Download 0.7.9 through HACS and restart Home Assistant.
2. Startup migrates model storage to schema 12, removing contaminated soft
   evidence and potentially contaminated pairing credit. Affected unexported
   profiles are reassessed and may return to learning.
3. Check the learned-readiness and Powercalc candidate lists. Collect independent
   quiet ON/OFF measurements before treating a new fixed profile as measured.

Clean small-step learning remains available. Accepted edge observations,
validated sessions, fingerprints, cumulative energy, entity identities, explicit
fixed wattages, and fixed modes are retained. A calibration batch does not start
by itself.

Preserved cumulative energy may include earlier inaccurate estimates; this update
does not reconstruct past consumption. Existing Powercalc handoffs are not
revoked automatically and should be reviewed if based on concurrent changes.

Offline regression checks include repeated group steps, unsafe remainder
attribution, runtime fallback, migration preservation, clean pairing, restart
idempotency, and diagnostic replay. These checks do not replace live measurement
of a particular device's actual electrical input.
