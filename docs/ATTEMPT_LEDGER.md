# Attempt consumption ledger

This additive API records invocation facts without changing selected-result
accounting in `usage.py`, cost reports, dashboards, or receipts. Worker execution
is instrumented. Cumulative admission is enforced separately through
[budget reservations](BUDGET_RESERVATIONS.md), using the same invocation identity. An empty ledger on an old
job means no observations were captured, not zero consumption.

## API and ownership

`puppetmaster.attempts` owns two frozen scalar-only dataclasses. Both
`SwarmStore` and `SQLiteSwarmStore` expose:

- `record_attempt(attempt: ExecutionAttempt) -> bool`
- `record_usage_observation(observation: UsageObservation) -> bool`
- `list_attempts(job_id, *, task_id=None) -> list[ExecutionAttempt]`
- `list_usage_observations(job_id, *, attempt_id=None) -> list[UsageObservation]`

Writes return `True` on insertion, `False` on exact replay, and raise
`LedgerConflictError` for different content at the same key. Observations require
an existing attempt in the same job. Lists return typed records, ordered by
`(started_at, attempt_id)` and `(attempt_id, observation_id)`, respectively.

An attempt key is `(job_id, attempt_id)`. `ExecutionAttempt.from_run(run,
adapter=...)` uses `AgentRun.id`, never `Task.attempts` or `worker_id`. If one run
makes multiple invocations, pass each actual invocation identity as
`invocation_id`; reusing the default would conflate them. The record also retains
`run_id` and `task_id`. Integrators must create it when execution starts, and
retain it when execution fails. Mutable run status is deliberately excluded.
For a sub-invocation with its own start time, construct `ExecutionAttempt`
directly with that timestamp. Reuse without execution creates no record.

An observation key is `(job_id, attempt_id, observation_id)`. The caller must
reuse the source event ID and timestamp when replaying a report; a new delivery
UUID on every retry defeats deduplication. More complete reports get distinct
source event IDs. These are immutable snapshots, not deltas. Multiple providers
or cumulative snapshots can overlap: summing every observation is invalid.
The reporting API below reconciles these snapshots before summing attempts.

## Values and canonical form

Every token field is a nonnegative integer or `None`. Unknown fields remain
`None` even when another field is measured. `usage_state` is `unknown` (all fields
absent), `measured`, or `estimated` (at least one field present). A measured zero
is explicitly `0` with state `measured`. Missing Codex usage is represented by
the default unknown observation; this API never substitutes the existing
selected-result usage normalizer for missing data.

Cost has independent `cost_state` and nullable `cost_usd`. A known cost requires
`cost_basis`: `api`, `plan_marginal`, or `api_equivalent`. Plan billing may have
explicit measured zero marginal cost while tokens remain unknown. API-equivalent
cost is an estimate, never a measured charge. A basis can be known while its
amount remains unknown. Cost is normalized to finite, nonnegative float USD;
NaN, infinity, bools, negative values and contradictory states are rejected.

Canonical JSON sorts keys, includes all nulls, and uses compact separators and
ASCII escaping. Identity strings are preserved exactly. Both backends compare
that canonical representation, including source and timestamp. The file backend
uses indented JSON physically; decoding produces the same canonical form.
There are no mutable dictionaries in either record.

## Persistence and compatibility

The separate ledger was chosen over adding consumption fields to mutable
`AgentRun` records: retries, heartbeats and terminal updates must not overwrite
immutable launch facts. The model module imports only `models`, avoiding the
store/cost/usage import cycle. No automatic `save_run` hook fabricates attempts.

SQLite schema v3 introduced `execution_attempts` and `usage_observations`;
the current schema v4 also adds budget reservations. Supervisor
migration creates empty tables and updates the version transactionally; v1
still receives the existing graph backfill. Worker attach refuses older schemas.
No historical attempt totals are inferred. SQLite writes serialize through a
writer transaction and reuse an enclosing completion transaction when present;
they neither emit events nor independently commit that transaction.

The file store uses job-wide, crash-expiring locks shared with budget operations
and the existing atomic
JSON rename convention. Opaque IDs are hashed into filenames. Lock contention
raises a retryable `RuntimeError`. This backend offers neither multi-record
transactions nor fsync durability guarantees. A process paused beyond the
300-second lock TTL can race a reclaimer; SQLite is the stronger concurrent
store. Reads across files are not snapshot-isolated.

Both backends retain ledger records across retry counters and subgraph resets.
Explicit job deletion removes them. Legacy jobs return empty lists until callers
write actual invocation records. Direct database/file tampering is outside the
immutable API contract.

## Runtime integration (slice 2)

`WorkerRuntime.run_once` binds its actual store, running `AgentRun`, and task
using `invocation.execution_scope` only when working-set reuse did not supply
persisted artifacts. `LocalWorker.run` records a generic adapter invocation
after preflight. Invocation-aware adapters instead record at their real CLI or
provider boundary, so there is no extra parent attempt to double-count:

- `CliWorkerAdapter._run_cli_lifecycle`: Codex, Claude Code, Antigravity, Cursor
  implement, and Hermes implement, after executable resolution and edit guards.
- Cursor analyze and Hermes analyze: each CLI launch, including Hermes' second
  analysis attempt when its first output is malformed.
- `AgenticAdapter._provider_call`: each admitted provider call, including key
  rotation, forced-tool retry, backoff, and subsequent agent turns. Circuit and
  rate-limit refusals before dialing create no attempt.
- `OpenAIAdapter.run`: the direct HTTP invocation.

Each launch creates one ID of the form `run_id:invoke_<nonce>`. The nonce is
generated once at that boundary, independent of task counters and reset; store
retries retain the same record. The runtime's saved run remains the owner even
though `LocalWorker` returns its own completion record. Actual resolved model
names are retained where the adapter exposes them; omitted CLI defaults remain
unknown. Task retry, fallback, and subgraph reset cannot reuse an invocation ID.

The CLI wrapper parses raw JSON/JSONL usage before snapshots, finalizers, gates,
lease checks, or artifact persistence. Missing Codex usage stays unknown even
when its legacy selected-result artifact reports zero. Provider responses carry
an additive `AssistantTurn.accounting_usage` snapshot so legacy normalized
counts and selected totals are unchanged. Partial raw metrics stay nullable;
measured zeros survive. Generic adapters record usage-bearing return artifacts
using stable artifact IDs. There is no second runtime usage recorder.

Observation keys identify raw stdout events, provider returns, or generic
artifact sources within an invocation. Identical repeated reports reuse their
timestamp and key; differing reports at the same key surface a ledger conflict.
Multiple stdout summaries can overlap and must not be summed blindly. Known
plan billing records zero marginal cost separately from reported API-equivalent
estimates. Metered/provider API costs retain their API basis. An amount with no
billing provenance remains unknown rather than being labeled an API charge.

## Failure boundaries

Consumption writes are best effort, with two immediate attempts per write. A failed start
write is retried with the same identity when usage returns. Persistent failures
emit `consumption.persistence_failed` with operation, attempt/task identity and
exception type; decoding failures emit `consumption.capture_failed`. Logging
remains available if the event store also fails. No raw exception text, provider
response, credentials, or prompts are copied into these events. Accounting
failures do not erase accepted results or change completion gates. Budgeted
dispatch requires durable reservation and adoption before execution; failures
there stop dispatch. Missing authoritative usage leaves pending liability.

A returned exception or unavailable usage produces an explicit unknown
observation in `finally`. A hard process kill can leave an attempt without an
observation; its consumption is unknown. Persistent storage failure can leave
no ledger record, and the warning/event is the only evidence of that gap.
This is not an atomic transaction with provider execution: a process may die
between the pre-call record and the actual launch. The record identifies a
launch attempt, not proof that a provider charged for it.

Retries hidden inside an external CLI are part of that CLI invocation. Truncated
CLI output or an interrupted provider stream may leave usage unavailable. Calls
made directly to adapters without `execution_scope(store, run, task)` log an
unbound-accounting warning and do not invent a store or job. Runtime-managed jobs
always bind the authoritative scope; standalone embedders must do so explicitly.

## Consumption report (slice 3)

`puppetmaster.consumption.build_attempt_consumption_report(store, job_id)` returns
a frozen `AttemptConsumptionReport`, using only `list_attempts` and
`list_usage_observations`. `report.to_dict()` produces detached JSON-serializable
fields. The dashboard snapshot exposes the same object under the separate
`attempt_consumption` field. Selected-result `cost`, token aggregation, terminal
receipts, and per-task routing budgets retain their existing meanings.

The report contains `job_id`, `attempt_count`, `attempts`, and `totals`. Each
attempt row retains the immutable `ExecutionAttempt`, sorted `observation_ids`,
and its reconciled `totals`. Rows sort by `(started_at, attempt_id)`. Both row
and job totals use `ConsumptionTotals`, with explicit fields:

- `tokens_in`, `tokens_out`, `cache_read_tokens`, `cache_write_tokens`
- `api_cost_usd`, `plan_marginal_cost_usd`, `api_equivalent_cost_usd`

Each field is a `ConsumptionMetric`: `total` (nullable), `known_subtotal`,
`status` (`unknown`, `partial`, `measured`, or `estimated`), `known_attempts`,
`unknown_attempts`, `estimated_attempts`, and `conflicting_attempts`.
`total` exists only when every recorded attempt has a reconciled value for that
metric. Estimated values remain labeled, even when coverage is complete.
An empty ledger has unknown totals and zero known attempts; its empty subtotal
of zero is not measured consumption. A captured explicit zero is measured.

Within an invocation, equal known values count once, even under different
observation IDs. Null observations cannot erase known values; complementary
partial snapshots fill separate metrics. If equal measured and estimated values
coexist, measured evidence wins. Different known values conflict: the metric
is unknown for that invocation and contributes no known subtotal. The ledger
does not encode authoritative final-snapshot precedence or delta boundaries,
so neither timestamp ordering nor taking a maximum proves an actual total.
Conflicts can be inspected through the row's observation IDs and the store read
API. Immutable key replays and conflicting writes retain the slice-1 rules.

Cost reconciliation runs separately per basis. Plan marginal zero never becomes
API zero, and API-equivalent estimates never become charges. A missing basis
remains unknown for that attempt, even when another basis is known. Thus mixed
plan/API jobs can expose partial subtotals in each column; callers must not sum
these columns as if they were one bill. No current registry prices are inferred.
Token columns also remain separate because providers differ in cache inclusion.

Reports include failed/retried/fallback/escalated invocations by immutable ID,
regardless of reset counters or withdrawn artifacts. Reuse creates no execution
consumption. Reset/reopen preserves reports; explicit job deletion removes the
ledger and returns an empty, unknown report. Historical jobs are not backfilled.

This report is a read API. Runtime budget enforcement uses the separate
[reservation lifecycle](BUDGET_RESERVATIONS.md), with authoritative cumulative
reconciliation rather than sums of these observations. During
live writes, the two read calls are not a shared transaction snapshot. A later
read can contain new attempts or observations; callers should reread after
execution settles. Complete metric coverage means coverage of recorded attempts
only: slice-2 persistence failures and hidden CLI retries remain the documented
telemetry limits. Unknown, partial, estimated, and conflicting metrics must not
be treated as exact measured charges.
