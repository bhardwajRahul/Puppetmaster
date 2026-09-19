# Feature map

Jev is an opt-in overlay on Puppetmaster analysis graph edges. Unset
is today's kernel. Observe scores. ACT may skip.

## Baseline

- Isolated `TemporaryDirectory` store. Never the user's live state dir.
- `hermetic_env` clears `PUPPETMASTER_JEV` and `PUPPETMASTER_JEV_ACT`.
- Doctor before a live Decisions drive.

## Proof

- Unset: `urlopen` not called.
- Observe: same graph actions as unset, plus `GATE` with `acted=false`.
- ACT: documented experiment only. Do not drive it against a real swarm.

## Features

- [Unset stays today's kernel](./unset.md)
- [Observe-only receipts](./observe.md)
- [ACT experiment skips](./act.md)
- [Launched-job V2 receipt](./v2-on-job.md)
