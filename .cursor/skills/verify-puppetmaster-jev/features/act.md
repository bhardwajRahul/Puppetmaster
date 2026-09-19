# ACT experiment skips

`PUPPETMASTER_JEV_ACT=1` restores skip / demote / reuse / stop. This
can be worse than today. It is not the ship default.

## Sub-features

- `act-v1` skips a compatible-pair auditor.
- `act-v2` returns a prior job handle instead of launching.
- `act-v3` refuses gist admission on a plumbing FINDING.
- `act-v4` drops `enqueue_subtasks`.

## How to get to it (user POV)

Do not. Tonight's opt-in testing stays observe-only.

## Driving it with unittest

```bash
python -m unittest tests.test_jev_transition.ConflictAuditorGateTests.test_act_compatible_low_noul_skips_auditor -v
```

Proof: auditor `SKIPPED` and GATE `acted=true` only when ACT is set.

## Gotchas

Never set ACT on a real user swarm. Forced high noul plus ACT swallows
a new goal.
