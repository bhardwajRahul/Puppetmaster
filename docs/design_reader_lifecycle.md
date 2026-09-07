# Readonly reader lifecycle

`connect(store)` prepares an idle helper, then acquires `ReaderAdmission` before
requesting an open. Successful sessions retain admission through the worker's
release acknowledgment. Failed opens report `session_closed` only after normal
return from the descriptor-owning stack. Retrying callers release admission,
back off, reacquire it, and repeat identity and metadata fences in the same helper.

Two designs were considered:

- A per-database helper broker could schedule requests and own every lifecycle.
  It would require another persistent service and queue protocol.
- A ready helper with explicit open retains the existing process boundary and
  keeps the admission module responsible only for per-inode exclusion. This is
  the selected design; no persisted schema or job wire format changes.

`readonly_admission.ReaderAdmission` owns kernel exclusion and fork bookkeeping.
`readonly._Transport` owns process, pipes, reader thread, and monotonic close state.
`readonly_cleanup.CleanupRegistry` owns registered transports and any admission
permit whose cleanup failed. Finalizers only retire tokens. Explicit maintenance
rotates retained owners; there is no cleanup I/O in finalizers or slot lookup.
Ownership is registered before publishing a weak finalizer, and a failed active
connection relinquishes its cache slot without relinquishing its admission.

The polling boundary retries `ReadUnavailable` within a context-local read
deadline. `StoreIdentityError` is not a transient availability failure.
