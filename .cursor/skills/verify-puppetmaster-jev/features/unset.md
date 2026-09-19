# Unset stays today's kernel

With `PUPPETMASTER_JEV` unset, a swarm never opens a Decisions socket
even if an OpenRouter key is in the environment.

## Sub-features

- `unset-no-socket` leaves the auditor queued.
- `unset-admits` still creates a gist when mechanical admission passes.
- `unset-enqueues` still creates follow-up tasks.

## How to get to it (user POV)

Do not export `PUPPETMASTER_JEV`. Start a swarm as usual.

## Driving it with unittest

```bash
python -m unittest tests.test_jev_transition.ConflictAuditorGateTests.test_unset_does_not_network_and_leaves_auditor_queued -v
```

Proof: `urlopen` not called. Auditor status `queued`. No `jev_transition` GATE.

## Gotchas

A leftover `OPENROUTER_API_KEY` is not opt-in. Only `PUPPETMASTER_JEV`.
