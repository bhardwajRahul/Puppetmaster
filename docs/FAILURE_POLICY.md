# Task failure edges

Worker specs may set `on_fail` to `abort`, `continue`, or `retry(n)` where
`n` is an integer from 0 through 10. The value is normalized into the task
payload as `failure_policy`; omitted policy preserves legacy behavior.

`retry(n)` counts policy retries separately from execution `attempts`. Each
retry advances the task generation and is bounded by `n`; cancellation never
retries. `continue` records the original failure and marks the task `SKIPPED`,
which satisfies dependents while keeping the failure artifact and decision
evidence visible. Fallback and review escalation do not multiply explicit
failure retries.

Operators can cut a task generation with `puppetmaster cut JOB --task TASK` or
the `puppetmaster_cut_task` MCP tool. A running task remains pending until its
local lease stops or safe stale recovery observes the stop; active leases are
never cleared by the cut. `restore` / `puppetmaster_restore_task` resets the
consumer closure, retains outputs as superseded artifacts, and clears only the
cut edge state.
