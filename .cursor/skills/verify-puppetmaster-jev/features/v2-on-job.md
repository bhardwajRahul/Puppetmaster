# Launched-job V2 receipt

Observe already-answered scores land on the job that launched, not the
prior job. Implement / edit swarms are not scored.

## Sub-features

- `v2-observe-launch` writes `edge=already_answered` on the new job.
- `v2-no-prior-write` leaves the prior job without a GATE.
- `v2-edit-skip` does not call Decisions for `mode=implement`.

## How to get to it (user POV)

Start an analysis swarm with Jev opted in. Read artifacts on that
`job_id`.

## Driving it with unittest

```bash
python -m unittest tests.test_jev_transition.AlreadyAnsweredTests.test_observe_high_noul_does_not_reuse tests.test_jev_transition.AlreadyAnsweredTests.test_orchestrator_observe_stamps_launched_analysis_job -v
```

Proof: launched job GATE `acted=false`, `would_action` set, evidence is
the prior job id. Prior job has no GATE.

## Gotchas

`apply_already_answered` is a no-op unless ACT. The orchestrator hook
is what writes the observe receipt after `create_tasks`.
