# V1 acts on opt-in

`PUPPETMASTER_JEV=1` plus a key may skip a conflict-auditor when
peer FINDINGs do not contradict. That replaces a model call the
kernel would have made anyway. `JEV_ACT` is not required.

## Sub-features

- `opt-in-v1` skips a compatible-pair auditor and writes
  `acted=true`.
- High contradiction noul, mechanical conflict, thin set, and
  fail-open still leave the auditor queued.

## How to get to it (user POV)

Export `PUPPETMASTER_JEV=1` and an OpenRouter key.

## Driving it with unittest

```bash
python -m unittest tests.test_jev_transition.ConflictAuditorGateTests.test_opt_in_low_noul_skips_auditor -v
```

Proof: auditor `SKIPPED`, GATE `acted=true`, skip VERIFICATION present.

## Gotchas

Mechanical `detect_contradictory_peers` still wins and does not
call Jev.
