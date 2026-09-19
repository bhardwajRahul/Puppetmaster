# Observe-only receipts

Opt-in without `PUPPETMASTER_JEV_ACT` scores edges and writes GATE
rows. The graph stays spawn / admit / enqueue.

## Sub-features

- `observe-v1` records `would_action=skip` and leaves the auditor queued.
- `observe-v3` records a plumbing `would_action=skip` and still admits.
- `observe-v4` records a stop score and still enqueues.

## How to get to it (user POV)

Export `PUPPETMASTER_JEV=1` and an OpenRouter key. Do not set
`PUPPETMASTER_JEV_ACT`.

## Driving it with the helper

```bash
.cursor/skills/verify-puppetmaster-jev/scripts/prove_observe.py
```

Proof: `/tmp/verify-this/puppetmaster-jev/observe.json` shows
`observe_matches_today=true` and every GATE `acted=false`.
`python -m puppetmaster show <job_id>` includes `## Jev observe`
when a GATE exists, and omits that section when unset.

## Gotchas

A 401 is not a Jev score. Fail-open looks like unset plus a fail_open
GATE. The Marionette Settings key is not read by product code.
