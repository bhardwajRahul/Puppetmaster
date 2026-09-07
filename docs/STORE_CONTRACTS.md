# Embedded store contracts

Puppetmaster v1.24.0 extends the existing stores without changing selected-result cost
or token accounting. Import frozen values from `puppetmaster.contracts` and use
the public methods on `SwarmStore` or `SQLiteSwarmStore`. These are embedding
APIs; they do not add a new HTTP service or execute Marionette domain commands.
TypeScript wire declarations are in `clients/typescript/puppetmaster.ts`.

## Identity and completion

`state_identity(path)` remains the path-derived state ID. The two-field
`JobRef(job_id, state_id)` is legacy/v1 and cannot identify a store incarnation.
New references use an explicit version:

```json
{"job_id":"job_example","state_id":"state_example","version":2,"incarnation":"f47a3b9c-0c44-414e-91ea-7cf9bdd56b37"}
```

Call `store.job_ref(job_id)` to bind a job after inspecting its owning store.
It reads existing metadata and never initializes or migrates a store.
`store.bind_job_ref(ref)` requires v2 and pins a continuation before subsequent operations.
`store.bind_job_ref(legacy_ref, legacy_read=True)` explicitly selects a legacy read
without certifying historical incarnation. Python metadata/receipt reads and CLI/MCP
`await`, artifact feed, and cost reads accept v1. They retain the supplied legacy
reference in their returned state; TypeScript read methods accept both versions.
`resolve_job_state` rejects conflicting IDs, validates explicit directory
ownership and incarnation, and rejects ambiguous unscoped IDs. The CLI accepts
`--job-ref '<JSON>'` before the subcommand; MCP forwards the reference across its
CLI subprocess boundary. TypeScript `awaitJob` and `isJobDone` accept either a
reference or the existing legacy job-ID string.

Supervisors persist a random UUID once: in SQLite's `metadata` table (introduced in schema v6, preserved by v7)
or the file backend's `metadata.sqlite3` index. Initialization is transactional
and concurrent bootstrap converges on one UUID. Reopen, v5 migration, and
trigger/index repair preserve it. Missing or corrupt identity in an already
upgraded store fails closed; it is not silently regenerated. Legacy migration
assigns an identity but cannot establish which earlier store produced a v1 ref.

SQLite attach reads metadata without migration or write PRAGMAs. Each operation
connection checks the pinned identity; scoped cancellation, effects and strict
completion validate again on their actual transaction connection. Unscoped metadata
lists, change pages, and the incarnation accessor also compare the actual identity
against the store object's pin in the same read transaction. Detached
launchers bootstrap and pin identity before spawning, and the child checks it
before operating. Replacing a pathname while SQLite has live handles remains an
unsupported filesystem operation: the database handle may continue to address
the original store or SQLite may report an I/O error. These checks prevent a
new operation connection from silently selecting a replacement; they cannot
make arbitrary filesystem replacement atomic with external effects.

Normal database backups/copies retain incarnation. A copy at another path has
a different state ID and requires explicit rebinding. Restoring a copy of the
same logical store retains its identity; incarnation does not detect rollback
within that store. A newly initialized replacement gets a different UUID.
The file backend cannot atomically couple its metadata database with separately
replaced JSON files. Replacing JSON files while retaining metadata is not a new
incarnation and cannot be detected by this contract. Stop writers for copy,
restore, and repair.

Legacy refs remain decodable in persisted receipts and readable through metadata
and receipt APIs. Mutating scoped contracts and `bind_job_ref` require v2 and
return an actionable `StoreIdentityError` for legacy/stale refs. Rebind only after
checking the selected store. Existing unscoped job-ID APIs retain legacy
semantics and do not prove incarnation. A state ID or UUID is not an access token.

`submit_completion(task, run, artifacts, event_payload, job_ref=ref)` returns a
`CompletionReceipt`; `get_completion_receipt(job_ref, run_id)` reads it.
`submit_completion` requires v2. `complete_task` retains its legacy Task return
value and also accepts optional `job_ref=ref` for strict validation. The accepted digest covers task,
job, lease and run identities, run start/completion times, artifacts, and event
payload. Heartbeat/lease-renewal timestamps do not change the submission identity.
An identical retry reconciles the same intent. A changed submission raises
`ContractConflict` before overwriting any accepted intent, even after publication.

Receipt reads use a scalar materialization written at intent submission and each
publication update. They open one read-only metadata transaction, validate identity
on that connection, and never read or decode the completion journal or reserve a
writer. Supervisor migration backfills old receipts; reads do not backfill.

Outcomes are `pending_publication`, `published`, `stale_lease`, `invalidated`,
`legacy_unknown`, and `unavailable`. `unavailable` covers missing projections,
lock contention, oversized metadata, or pending file writes. `legacy_unknown`
also covers a run without a recorded intent;
it never infers publication from a terminal task. Old completion records retain
unknown identity even if their publication can be replayed. Run IDs remain
unique across jobs in the SQLite backend, as required by its existing run tables.

## Bounded historical evidence

The existing ledger/report methods remain unbounded and unchanged in purpose.
The additive embedding APIs read scalar projections written with the sources:

- `list_attempt_refs(job_ref, ...)`
- `list_run_refs(job_ref, ...)`
- `list_process_outcome_refs(job_ref, ...)`
- `list_usage_observation_refs(job_ref, ...)`
- `historical_evidence_counts(job_ref)`

The page methods return `HistoricalPage`; counts return `HistoricalCounts`.
Both types live in `puppetmaster.history_metadata`, with matching TypeScript
declarations. Pages never call `build_attempt_consumption_report` and never load
instructions, artifact bodies, stdout/stderr, or source ledger rows. Counts use
four indexed lookups. Supervisor migration may backfill projections; read-only
queries never repair or backfill them.

Each page accepts `cursor`, `limit` (1–200), `max_scan` (1–1000), and `max_bytes`
(1024–262144). Bounds include the page envelope and cursor in unindented ASCII
JSON (`json.dumps(to_jsonable(page), ensure_ascii=True)`). A row that cannot fit
returns `unavailable` rather than being truncated. `scanned` includes lookahead
rows. Filters are the owning JobRef and the method's evidence kind; authenticated
cursors cannot be reused for another job, kind, path or incarnation. Changing
page-size bounds between requests is allowed.

Pagination uses an insertion sequence and a captured high-water mark. Later
inserts do not expire the cursor or enter that traversal. `captured_count` is the
number of projected records at the first page's snapshot. Run facts may reflect
updates made after that first page; membership is stable, not a frozen run-state
snapshot. Reset/retry/fallback preserves prior attempts and outcomes. Deletion
of records in the selected job/kind expires that history traversal; unrelated
job/kind deletion does not. File-index repair advances the global epoch; a deleted
job returns `unavailable`. Reopen preserves valid cursors. Replacement refuses
old refs/cursors. Invalid or differently scoped tokens raise `ValueError`;
identity errors raise `StoreIdentityError`. Lock contention or an incomplete
file projection returns `unavailable` and preserves the retry cursor.

Observation and process-outcome facts include exact `job_id`, `attempt_id`,
`task_id`, and `run_id`. An indexed `(kind, job_id, attempt_id)` lookup against
projected attempt metadata resolves identity independently of loaded pages.
A missing or oversized attempt projection yields null task/run IDs and
`identity_state="unavailable"`; no cross-job or most-recent-run inference occurs.

Coverage is machine-readable and never asserts complete provider history:

- `captured`: the requested captured records were enumerated; this says nothing
  about missing provider invocations or whether their usage is known.
- `partial`: more pages remain, or the unbounded consumption report has recorded
  attempts without observations.
- `unknown`: no captured evidence, or metadata is unavailable.

`complete_invocation_history` is always `false`. A captured count of zero is a
count of stored records, not measured zero consumption. Observation facts keep
null distinct from explicit zero, preserve conflicting source observations, and
keep `api`, `plan_marginal`, and `api_equivalent` costs separate. No page sums or
silently reconciles them. Process outcome refs use the existing predicate: an
observation has a return code or timeout fact. They are not delivery verdicts or
current task statuses.

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
endless empty continuation page. SQLite CASE guards limit each projected text
column to `min(max_bytes, 4096)` bytes before transfer to Python. History facts
use the same 4096-byte record ceiling, and exact attempt identity resolution
reads at most one such record per observation. Oversized legacy rows return
`unavailable`; IDs, bindings, and scope are never truncated. These are input
bounds in addition to candidate count and serialized-output bounds.
Stable ID keysets order snapshots;
revision keysets order changes. Cursors are authenticated, versioned, and bound
to the store and filters; clients must treat them as opaque.

`complete` means the query is exhausted at that revision. `partial` supplies a
continuation. Snapshot cursors retain their initial journal revision and read
versioned scalar rows through an append-only entity index, with an indexed
version lookup per candidate. A captured maximum key bounds traversal. Candidate
limits apply before the birth-revision cutoff, so later keys can cause sparse
pages without an unbounded database scan or entering snapshot membership.
Later inserts and mutations cannot change snapshot
membership, order, scope, status, or bindings. Change cursors also retain their
original high watermark. Pruning version/entity/journal records or explicit
index repair expires cursors through the retention epoch; replacement remains
an identity error. Deletion tombstones remain in the journal. No automatic
retention or compaction is implemented; versions consume storage until explicit
maintenance.

SQLite busy/locked metadata reads return `unavailable` with no items, revision
zero for snapshots (unknown coverage) or the input revision for changes, zero
scanned rows, and the unchanged input cursor.
Retry the same query and cursor after contention clears; no progress was made.
This applies to both SQLite stores and the file store's SQLite metadata index.
Other database errors, apart from missing projection tables, still raise.

Job summary filters support `status`, `job_ref`, `origin`, `project_id`, and
`session_id`, individually or together. Pass keyword filters or a frozen
`JobSummaryFilter` as `filters`. Scope values are exact, case-sensitive strings
of 1–256 characters; `None` means no constraint. Both cursor types bind all
filters. Scope filtering examines at most `max_scan` metadata rows per page;
sparse filters can return empty `partial` pages. Consumers may continue until
`complete` across ticks. Budgeting one page per UI tick is consumer policy:
the kernel exposes one page per call, never a drain-all API.

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

Job summaries add `goal_preview`, `goal_preview_truncated`, `delivery`, and
`quality`. The preview is the longest Unicode-scalar prefix of the stored goal
that fits **512 UTF-8 bytes**, without ellipsis or normalization. Empty goals
produce `""`/`false`; absent, non-string, or invalid inspected Unicode produces
`null`/`null`. Writers inspect at most the first 513 scalars; characters beyond
that prefix are not certified. Full goals, labels, task instructions and artifact
bodies are never loaded by these readers. The JSON page budget still includes
escaped preview text, the envelope and cursor.

Delivery is a conservative lifecycle projection: queued/running/stitching map to
`pending`; failed/stalled/cancelled to `blocked`; complete to `unverified`.
Unsupported status maps to `unavailable`. Quality remains `unverified` for known
lifecycle values and `unavailable` otherwise. Exit success, completion publication,
and artifact presence do not establish semantic quality or successful delivery.
Ownership is unknown when explicit scope stamps are absent; neither a preview
nor a known projection stamp is ownership evidence.

### Frozen selected-result economics

`store.get_selected_economics(job_ref, expected_summary_revision=None)` requires
a **v2 JobRef**. `SelectedEconomics`, `SelectedTotals`, and `SelectedMetric` are
exported from `puppetmaster.contracts` and `puppetmaster.selected_economics`.
The lookup validates identity, reads the current job revision and reads one
fixed projection in the same read-only snapshot. It does not hydrate a Job,
read source tables, visit history, or call either cost/consumption report builder.
The stored version-1 payload is at most **4096 ASCII-JSON bytes**; the public
response is at most **8192 bytes**. Economics never appears in summary pages.

The required envelope fields are `job_ref`, `outcome`, `summary_revision`,
`receipt_digest`, `source`, `coverage`, `selected_count`, `totals`, `reason`, and
`retry_after_ms`. Missing optional values are explicit `null`. `outcome=available`
means a valid frozen projection was retrieved, not complete invocation telemetry.
`source=terminal_receipt` and `coverage=selected_receipt` describe the selected
receipt only. Missing legacy provenance is explicitly unavailable/unknown.

`totals` has seven fixed metrics: `tokens_in`, `tokens_out`, `cache_read_tokens`,
`cache_write_tokens`, `api_cost_usd`, `plan_marginal_cost_usd`, and
`api_equivalent_cost_usd`. Each metric has `total`, `state`, `known_selected`,
`unknown_selected`, `estimated_selected`, and `conflicting_selected`. A total is
present only for a nonempty, entirely known selected set. Explicit measured zero
remains zero; any estimated contribution makes an entirely known total estimated.
Mixed known/unknown contributions yield `partial` with `total=null`; no known
contributions yield `unknown`. There are no partial subtotals or combined money
metrics. Counts and tokens are exact integers in 0..2^53-1; dollars are finite,
nonnegative and at most 10^12. Overflow yields `numeric_limit`, never rounding.

New terminal writers preserve versioned token-presence facts. An absent SDK token
component stays unknown even when the older cost report defaulted it to zero.
An explicit reported dollar amount, including zero, is separate from registry
valuation. Plan policy marginal zero is **estimated**, not an observed charge.
Registry API-equivalent valuation is estimated. Missing bases remain unknown;
API, plan-marginal and API-equivalent amounts are never added together. A failed
$7 attempt followed by a selected successful $3 attempt therefore reports $3
selected economics; captured attempt consumption may separately report $10.

The first accepted terminal cost receipt freezes `bounded_economics` and its
source-receipt SHA-256 digest (computed without that member). Repeated terminal
writes preserve it; conflicting non-null terminal receipts are rejected.
Artifact updates, registry churn and late observations do not reprice it.
Reopening or explicitly clearing the receipt clears the projection, allowing a
new freeze. Deletion clears current economics; file repair reconstructs it from
the stored receipt. File writes retain the documented weaker crash/power-loss
guarantees and a pending marker makes reads unavailable until explicit repair.

If `expected_summary_revision` differs from the current revision, the result is
`unavailable` with `reason=selection_changed` and `totals=null`. Refetch the summary
before combining it with current economics. This is not a historical economics
query. Missing jobs raise `KeyError`; stale/legacy references raise
`StoreIdentityError`. Missing, pending, malformed, or unavailable snapshots return
explicit reasons, including retryable `read_snapshot_unavailable` on contention.
No response is a whole-job spend total or proof of complete telemetry.

SQLite supervisor migration advances schema v5/v6 to **v7**. The file metadata
index uses `display_economics_version=1`. Both preserve store incarnation and
cursor secrets, add unavailable defaults to old change/version rows, and publish
new current projections at migration time. Old snapshots retain old unknown
fields. Current/change/version copies share the display tuple, including child
counts, completion publication, history updates and tombstones. Legacy attachment
performs no migration or backfill; old projection schemas expose unavailable
new fields. Stop old writers and migrate in the supervisor before worker attach.

The CLI exposes `job-summaries`, `job-summary-changes`, and `selected-economics`.
Matching MCP tools are `puppetmaster_list_job_summaries`,
`puppetmaster_read_job_summary_changes`, and `puppetmaster_selected_economics`.
TypeScript exports `listJobSummaries`, `readJobSummaryChanges`, and
`getSelectedEconomics`, plus bounded wire decoders. Each call returns one page or
one envelope; wrappers never auto-drain. Older-server omissions decode to
unavailable values, not zero or false. Existing APIs remain available.

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
receipt without authorizing dispatch. An explicitly rebound v2 caller can replay
a persisted v1 effect with identical immutable facts. The original legacy receipt
and replay fence are preserved; rebinding does not rewrite historical identity.
The same rule applies to cancellation receipts and completion digests: v1
serialization omits incarnation/version additions, including inside nested
submission payloads.

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

SQLite supervisor initialization migrates v5 through v6 to v7, adding incarnation metadata
and historical evidence projections. Earlier versions also receive the indexed
projection, change-journal, cancellation-target and contract-receipt tables. Projection writes and source writes share a transaction, including
bulk operations and claim/reset paths. Worker attachment does not migrate.
Supervisor initialization also rebuilds generated projection triggers when the
schema version is already 7, repairing persisted SQL from an interrupted source
upgrade. Dependent triggers are dropped before schema changes and recreated in
the same transaction; existing projection rows and journal history are retained.
Stop long-lived old binaries at schema cutover and restart all writers on the
new version. This repair supports restart convergence, not hot compatibility
between old binaries and the upgraded schema. Worker attachment alone does not
perform this repair.
Legacy job/task/artifact projections are stamped `legacy_unknown`; no old cancellation,
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

### Previous membership authority

Job change rows expose `previous_membership` (`present`, `absent`, or
`unavailable`), `previous_status`, `previous_origin`, `previous_project_id`, and
`previous_session_id`. These fields describe the projection immediately before
that change, independently of its current ownership. `absent` proves insertion;
null ownership fields on a `present` row remain unknown ownership, not a match.
A consumer may authorize removal from an old scope only from this prior authority.

The supervisor records the authority in the same transaction as each change.
Legacy history is not reconstructed from current fields. Missing, malformed, or
oversized prior authority returns an empty `unavailable` page. A deleted journal
revision invalidates older revision-only checkpoints as well as active cursors.
`reason` identifies membership/history unavailability when known. On failed change
pages, retain the input checkpoint and cursor; advance only on complete pages.
A fresh snapshot is required when history cannot prove membership.

Store construction is non-creating for both backends; `ensure`/`init` remain the
supervisor bootstrap paths. A v5 SQLite attachment permits explicit legacy v1
reads without bootstrapping an incarnation. It does not certify v2 authority;
mutations still require a v2 reference.

Ownership selection, attachment, schema probes, and metadata reads use an
isolated immutable SQLite reader. It checks the selected root/database identity,
locks the source, and reads the checkpointed main database without copying it or
creating sidecars. An open read stays bound to that database incarnation; a new
operation rejects a replaced root/database. The helper has bounded query and
response budgets and is terminated and reaped on failure. Metadata operations
reuse a store-lifetime helper process, but each read closes its SQLite connection
and source descriptors before returning the helper to idle. Every new session
rechecks identity, locks and checkpoint state; no snapshot survives a write.
Metadata source-contention retries, including SQLite BUSY/LOCKED (base codes
5/6 and extended codes) and active-reader code 5, have a 100 ms budget. Direct probes use
disposable helpers with at most one second of source-contention retries; attach
retains its bounded outer retry policy. Only explicit post-launch reference
binding opts into the caller's longer bounded lock deadline (currently five
seconds); non-lock source unavailability keeps the short budget. Each failed
initialization reaps its helper before retrying. Store disposal reaps its cached helper. Cross-project ownership scans share one
disposable helper. Their bounded membership cache requires unchanged root,
main-database and sidecar inode/size/mtime/ctime stamps. Root directory stamps
invalidate even a WAL created and then unlinked without changing the main DB.
Scoped references always revalidate their incarnation through SQLite.

Weak v1 job-ID-only discovery skips unavailable opportunistic global stores.
It cannot certify global uniqueness: a skipped store may contain the same ID.
Two readable positive owners still produce ambiguity. The caller-resolved target
must remain readable even if another owner is found; its unavailability is never
converted to not-found or fallback. Explicit state directories and scoped JobRefs
fail closed, retaining incarnation, replacement, source-change and WAL checks.
Discovery uses only the non-mutating reader; it never opens a live SQLite
membership probe to infer ownership from an unavailable snapshot.

Live or uncheckpointed WAL/SHM and rollback-journal sidecars return retryable
`unavailable`. Retained WAL sidecars are readable only when a bounded WAL-index
check proves all published frames were checkpointed and the helper excludes
other SQLite connections. Readers
never ignore WAL-only commits, copy the full database/WAL, or repair sidecars.
After the supervisor closes/checkpoints its connections, committed metadata is
readable again. Source changes during a read also return unavailable. POSIX
source descriptors belong only to the helper, so closing them cannot release
SQLite locks owned by the caller. Windows uses a nonblocking `LockFileEx` on a
read handle; its runtime behavior requires a Windows verification pass.

Cancellation consumes at most 201 binding candidates before rejecting a request
over the 200-item limit. Effect evidence allows 1–200 references, at most 4096
UTF-8 bytes each and 65536 bytes combined. Scalar projections are type/byte
checked in SQL before transfer. Invalid persisted scalars return unavailable;
no truncated identity is presented as valid. Emitted cursors fit their decoder's
4096-byte limit. If a continuation cannot fit, the page is unavailable and keeps
the supplied checkpoint rather than emitting an unusable token.

Metadata JSON is byte bounded before decoding and rejects nesting beyond 32
levels, nonfinite numbers, integers outside signed 64-bit range, and finite
numbers whose magnitude exceeds 2^63-1. Malformed historical facts and projected
scope/binding return unavailable without advancing the supplied cursor or change
checkpoint. Cursor inspection and authentication reject malformed JSON with
ValueError. Ledger token counts are nonnegative signed-64-bit integers; costs
are finite, nonnegative numbers no greater than 10^12 USD, validated before float
conversion. These limits describe transport representability, not cost estimates.
JobRef scalar identities must be exact strings of 1–256 ASCII characters;
v2 incarnation must be an exact canonical 36-character UUID string. Legacy v1
keeps its two-field wire shape and cannot carry an incarnation.
