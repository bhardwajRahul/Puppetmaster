# V2–V4 observe receipts

Opt-in without `PUPPETMASTER_JEV_ACT` scores already-answered,
FINDING admission, and stop-spawn. Those graph actions stay
launch / admit / enqueue.

## Sub-features

- `observe-v2` records a score on the launched job and still launches.
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
`v234_match_today=true`. `python -m puppetmaster show <job_id>`
includes `## Jev` when a GATE exists, and omits that section when
unset.

## Gotchas

A 401 is not a Jev score. Fail-open looks like unset plus a fail_open
GATE. The Marionette Settings key is not read by product code.
