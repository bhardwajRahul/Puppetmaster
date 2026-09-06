# Embedded store contracts

Puppetmaster v1.24.0 extends the existing stores without changing selected-result cost
or token accounting. Import frozen values from `puppetmaster.contracts` and use
the public methods on `SwarmStore` or `SQLiteSwarmStore`. These are embedding
APIs; they do not add a new HTTP service or execute Marionette domain commands.
TypeScript wire declarations are in `clients/typescript/puppetmaster.ts`.

## Identity and completion

`JobRef(job_id, state_id)` is the identity of a job within one resolved state
directory. `resolve_job_state` rejects conflicting IDs, validates explicit
state-directory ownership, and rejects ambiguous unscoped IDs. MCP and CLI
resolution use this boundary. A state ID is a path identity, not an access token.

`submit_completion(task, run, artifacts, event_payload)` returns a
`CompletionReceipt`; `get_completion_receipt(job_ref, run_id)` reads it.
`complete_task` retains its Task return value. The accepted digest covers task,
job, lease and run identities, run start/completion times, artifacts, and event
payload. Heartbeat/lease-renewal timestamps do not change the submission identity.
An identical retry reconciles the same intent. A changed submission raises
`ContractConflict` before overwriting any accepted intent, even after publication.

Outcomes are `pending_publication`, `published`, `stale_lease`, `invalidated`, and
`legacy_unknown`. The last outcome also covers a run without a recorded intent;
it never infers publication from a terminal task. Old completion records retain
unknown identity even if their publication can be replayed. Run IDs remain
unique across jobs in the SQLite backend, as required by its existing run tables.

## Bounded metadata

- `list_job_summaries(...)`
- `read_job_summary_changes(after_revision=0, ...)`
- `list_task_refs(job_ref, ...)`
- `list_artifact_refs(job_ref, ...)`

All return a frozen `MetadataPage`. Task/artifact pages support status and a
scoped JobRef. Job pages additionally support explicit host scope stamps. Task references include generation/lease bindings;
artifact references include task ID, artifact type and hash. Job summaries include
projected task/artifact counts. No page path hydrates instructions, job goals, or
artifact bodies. Counts are maintained on writes, rather than counted on reads.

Bounds are `limit=1..200`, `max_scan=1..1000`, and
`max_bytes=1024..262144`, including the JSON envelope and continuation token under
standard `json.dumps` serialization. Defaults are 100 rows, 1000 scanned rows,
and 256 KiB. A row that cannot fit yields `unavailable`; it cannot produce an
endless empty continuation page. Stable ID keysets order current snapshots;
revision keysets order changes. Cursors are authenticated, versioned, and bound
to the store and filters; clients must treat them as opaque.

`complete` means the query is exhausted at that revision. `partial` supplies a
continuation. Current-snapshot cursors return `cursor_expired` after any indexed
mutation; restart from the first page. Change cursors retain their original high
watermark, so live writes cannot extend an in-progress page sequence. Deletion
tombstones remain in the journal. No journal compaction is implemented yet.

SQLite busy/locked metadata reads return `unavailable` with no items, revision
zero (unknown coverage), zero scanned rows, and the unchanged input cursor.
Retry the same query and cursor after contention clears; no progress was made.
This applies to both SQLite stores and the file store's SQLite metadata index.
Other database errors, apart from missing projection tables, still raise.

Job summary filters support `status`, `job_ref`, `origin`, `project_id`, and
`session_id`, individually or together. Pass keyword filters or a frozen
`JobSummaryFilter` as `filters`. Scope values are exact, case-sensitive strings
of 1–256 characters; `None` means no constraint. Both cursor types bind all
filters. Scope filtering examines at most `max_scan` metadata rows per page;
sparse filters can return empty `partial` pages. Continue until `complete`.

`Job`, store creation methods, and `Orchestrator.run` accept the three optional
scope fields. They persist in both stores and appear on job projection rows.
`save_job(dataclasses.replace(job, origin=...))` may change or clear a stamp.
Each change records old and new scope together with status: entering or staying
in the combined filter emits an upsert (`deleted=false`); leaving emits a
removal (`deleted=true`). Physical deletion also emits a removal, including for
an explicit JobRef after the job is gone. Changes outside both old and new
membership are omitted. Consumers apply revisions in order, removing by JobRef
and ID for tombstones; tombstone fields may describe the new nonmatching state.
Scope changes conflict with retries of a launch key carrying different stamps.

There is no equivalent historical job stamp in labels or worker payloads.
Missing fields load as `None`; legacy rows do not match a scoped filter. We do
not derive scope from goal prose, labels, provider sessions, or state-directory
names. Historical journal rows retain unknown scope; migration does not infer
past membership from the current job. JobRef remains the sole job identity.

MCP launch schemas accept these fields and transport them to the launcher.
CLI callers can put `--origin`, `--project-id`, and `--session-id` before the
subcommand; `run` also accepts them after it. Detached swarm launches forward
these flags. TypeScript exposes `JobScope`, `JobSummaryFilter`, and
`JobSummaryOptions` alongside the metadata row fields.

## Cancellation

`request_cancellation(job_ref, request_id, bindings)` durably requests stop of
1–200 explicit task generations. Use the bindings from `list_task_refs`.
Generations advance on claims and resets independently of resettable retry
counts. A lease nonce and owner further fence each execution. Identical request
IDs replay their receipt; different targets return `conflict`.

The receipt distinguishes `requested`, `observed_stop`, `stale_binding`, and
`already_terminal`. Query it with `get_cancellation_receipt`. Queued targets are
not claimed; eligible queued tasks behind cancelled targets can still be claimed.
Runtime execution contexts check their owning store before external
dispatch; agentic streams retain their cooperative checks and streamed CLI waits
check every 250 ms. A successor is not cancelled by an old binding. The old
string-only process flags apply only to standalone, unscoped adapter callers.

`observed_stop` means the bound local execution scope exited. It never means a
remote provider, a spawned remote operation, or a Marionette command stopped.
Cleanup remains explicitly unknown unless the caller supplies local evidence.
`cleanup_owned_process` holds the Popen object. POSIX group signalling requires
an unreaped, launch-owned session leader and excludes concurrent reaping while
signalling. Escaped descendants require current inherited owner-nonce evidence,
revalidated before signalling. Windows cleanup uses the launch-owned Job Object
and held process handle, never cached ancestry or descendant PIDs. Owned Windows
launches assign the suspended process to the Job Object before resuming it.
Descendant cleanup reports partial or unknown evidence, not universal success.
POSIX nonce discovery still has an inspection-to-signal race and depends on
process visibility. This is bounded best effort, not a security sandbox or
universal containment. Legacy timeout/tree helpers retain their existing scope.

## Effect receipts

`record_effect(EffectReceipt(...))` records a new logical effect at revision 1,
`not_dispatched`. An effect binds a stable effect ID and request digest to JobRef,
task generation, lease owner/nonce, run ID and a recorded invocation attempt.
Changing immutable identity conflicts. Replaying an intent returns its latest
receipt without authorizing dispatch.

`execute_effect(intent, operation)` is the minimal Puppetmaster-owned execution
boundary. Only a newly accepted intent executes. It persists `in_flight` before
calling `operation`, which must return an `EffectObservation` containing an
explicit outcome and evidence references. Exceptions persist `unknown` when
possible; an interrupted observation leaves `in_flight`. Either state fences
replay after reopening the store. The wrapper does not infer success from a
missing exception. It is an explicit embedding API, not automatic wrapping of
all existing adapter/provider or tool calls.

`advance_effect` uses an expected revision and explicit evidence to record
`succeeded`, `failed_no_effect`, or `unknown`. An identical transition retries as
a read; conflicting facts or revisions fail. Unknown outcomes can be reconciled
to a final observation, never automatically moved back to dispatch. Replay
policies (`safe`, `reconcile_first`, `requires_authorization`,
`provider_idempotent`) are recorded policy, not automatic permission to replay.
Budget settlement remains a separate existing mechanism. Marionette continues
to own its domain execution and reconciliation.

## Migration and guarantees

SQLite supervisor initialization migrates v4 to v5 by adding indexed projection,
change-journal, cancellation-target and contract-receipt tables plus source-row
triggers. Projection writes and source writes share a transaction, including
bulk operations and claim/reset paths. Worker attachment does not migrate.
Supervisor initialization also rebuilds generated projection triggers when the
schema version is already 5, repairing persisted SQL from an interrupted source
upgrade. Dependent triggers are dropped before schema changes and recreated in
the same transaction; existing projection rows and journal history are retained.
Stop long-lived old binaries at schema cutover and restart all writers on the
new version. This repair supports restart convergence, not hot compatibility
between old binaries and the upgraded schema. Worker attachment alone does not
perform this repair.
Historical projections are stamped `legacy_unknown`; no old cancellation,
effect, completion identity, or consumption evidence is fabricated. Task rows
without a generation retain null until a new claim/reset creates an epoch.

The file store initializes a versioned `metadata.sqlite3` index once. Each source
write commits a unique pending marker before atomic rename and removes it only
after projecting the durable source. Pages report `unavailable` while any marker
remains, including after a crash. With writers stopped,
`repair_metadata_index()` explicitly scans source records and rebuilds metadata;
page requests never trigger this scan. File completion journals retain their
300-second crash-expiring lock, atomic-rename, and weaker concurrent-writer and
power-loss guarantees. The metadata index does not make file task/lease writes
transactional with cancellation or effects. Restart all writers on upgrade.

`build_job_receipt` now includes `attempt_consumption`, using the existing
consumption report. Its `tokens`, selected-result economics, and cost receipt
semantics are unchanged. Missing historical usage stays unknown.
