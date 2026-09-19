# ACT experiment skips

`PUPPETMASTER_JEV_ACT=1` lets V2–V4 reuse / demote / stop. That
cancels work the kernel does not currently put to a model. It is
not the ship default.

## Sub-features

- `act-v2` returns a prior job handle instead of launching.
- `act-v3` refuses gist admission on a plumbing FINDING.
- `act-v4` drops `enqueue_subtasks`.

V1 already acts on opt-in alone.

## How to get to it (user POV)

Do not set this on a real swarm.

## Driving it with unittest

```bash
python -m unittest tests.test_jev_transition.AlreadyAnsweredTests.test_act_high_noul_reuses_prior_and_writes_handle_gate -v
```

Proof: reuse / demote / stop only when ACT is set.

## Gotchas

Forced high already-answered noul plus ACT can swallow a new goal.
