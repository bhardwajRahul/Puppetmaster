"""Shared durable effect and cancellation operations for both stores."""
from __future__ import annotations

import json
from dataclasses import replace
from contextlib import contextmanager

from puppetmaster.contracts import (
    CancellationReceipt, ContractConflict, EffectReceipt, TaskBinding, immutable_digest,
)
from puppetmaster.models import JobRef, TaskStatus, is_terminal_job_status, to_jsonable
from puppetmaster.projections import connection


@contextmanager
def _transaction(store):
    if store.backend_name == "sqlite":
        with store._writer_scope():
            with connection(store) as c:
                yield c
    else:
        with connection(store) as c:
            c.execute("BEGIN IMMEDIATE")
            yield c


def task_binding(task):
    return TaskBinding(task.id, getattr(task, "generation", None),
                       getattr(task, "lease_id", None), getattr(task, "lease_owner", None))


def _load(c, kind, ref, key):
    row = c.execute("SELECT data FROM contract_receipts WHERE kind=? AND job_id=? AND id=?",
                    (kind, ref.job_id, key)).fetchone()
    return json.loads(row[0]) if row else None


def _save(c, kind, ref, key, value):
    c.execute("""INSERT INTO contract_receipts VALUES(?,?,?,?)
        ON CONFLICT(kind,job_id,id) DO UPDATE SET data=excluded.data""",
              (kind, ref.job_id, key, json.dumps(to_jsonable(value), sort_keys=True)))


def _effect(raw):
    return EffectReceipt(**{**raw, "job_ref": JobRef(**raw["job_ref"]),
                            "binding": TaskBinding(**raw["binding"]),
                            "evidence_refs": tuple(raw["evidence_refs"])})


def _cancel(raw):
    return CancellationReceipt(**{**raw, "job_ref": JobRef(**raw["job_ref"]),
                                 "bindings": tuple(TaskBinding(**b) for b in raw["bindings"])})


def _key(value):
    if not isinstance(value, str) or len(value) > 256 or not value.strip():
        raise ValueError("contract identity must be a nonempty string of at most 256 characters")


def _check_effect_cancellation(c, receipt):
    if c.execute("""SELECT 1 FROM cancellation_targets
        WHERE job_id=? AND task_id=? AND binding_digest=? LIMIT 1""",
        (receipt.job_ref.job_id, receipt.binding.task_id,
         immutable_digest(receipt.binding))).fetchone():
        raise ContractConflict("effect blocked by scoped cancellation")


class StoreContracts:
    def request_cancellation(self, job_ref, request_id, bindings):
        """Request cooperative stop of exactly these generations, never successors."""
        self.validate_job_ref(job_ref, strict=True)
        _key(request_id)
        from itertools import islice
        bindings = tuple(islice(bindings, 201))
        if not bindings or len(bindings) > 200:
            raise ValueError("provide 1..200 unique task bindings")
        if any(not isinstance(b, TaskBinding) for b in bindings):
            raise ValueError("cancellation requires task bindings")
        if len({b.task_id for b in bindings}) != len(bindings):
            raise ValueError("provide 1..200 unique task bindings")
        bindings = tuple(sorted(bindings, key=lambda b: b.task_id))
        with _transaction(self) as c:
            self.validate_job_ref(job_ref, connection=c, strict=True)
            prior = _load(c, "cancel", job_ref, request_id)
            if prior:
                prior = _cancel(prior)
                return prior if prior.bindings == bindings else replace(prior, outcome="conflict")
            tasks = [self.get_task_by_id(b.task_id) for b in bindings]
            if any(t.job_id != job_ref.job_id for t in tasks):
                raise ValueError("cancellation target belongs to another job")
            if any(b.generation is None or task_binding(t) != b for t, b in zip(tasks, bindings)):
                outcome = "stale_binding"
            elif is_terminal_job_status(self.get_job(job_ref.job_id).status) or all(
                    t.status in (TaskStatus.COMPLETE, TaskStatus.FAILED) for t in tasks):
                outcome = "already_terminal"
            else:
                outcome = "requested"
            receipt = CancellationReceipt(job_ref, request_id, bindings, outcome, 1)
            _save(c, "cancel", job_ref, request_id, receipt)
            if outcome == "requested":
                for b, task in zip(bindings, tasks):
                    c.execute("INSERT INTO cancellation_targets VALUES(?,?,?,?,?,?,?,?)",
                              (job_ref.job_id, b.task_id, b.generation, b.lease_id or "",
                               b.owner or "", request_id, immutable_digest(b),
                               int(task.status in (TaskStatus.COMPLETE, TaskStatus.FAILED))))
            return receipt

    def get_cancellation_receipt(self, job_ref, request_id):
        with connection(self, metadata_only=True) as c:
            self.validate_job_ref(job_ref, connection=c)
            value = _load(c, "cancel", job_ref, request_id)
            return _cancel(value) if value else None

    def cancellation_pending(self, job_ref, binding):
        with connection(self) as c:
            self.validate_job_ref(job_ref, connection=c, strict=True)
            row = c.execute("""SELECT 1 FROM cancellation_targets
                WHERE job_id=? AND task_id=? AND binding_digest=? LIMIT 1""",
                (job_ref.job_id, binding.task_id, immutable_digest(binding))).fetchone()
        return bool(row)

    def observe_cancellation(self, job_ref, binding, *, cleanup="unknown"):
        """Called after local execution stops; says nothing about remote effects."""
        self.validate_job_ref(job_ref, strict=True)
        if cleanup not in {"unknown", "partial", "local_process_exited"}:
            raise ValueError("invalid cleanup outcome")
        with _transaction(self) as c:
            self.validate_job_ref(job_ref, connection=c, strict=True)
            rows = c.execute("""SELECT request_id FROM cancellation_targets
                WHERE job_id=? AND task_id=? AND binding_digest=? AND observed=0""",
                (job_ref.job_id, binding.task_id, immutable_digest(binding))).fetchall()
            c.execute("""UPDATE cancellation_targets SET observed=1
                WHERE job_id=? AND task_id=? AND binding_digest=?""",
                (job_ref.job_id, binding.task_id, immutable_digest(binding)))
            for row in rows:
                prior = _cancel(_load(c, "cancel", job_ref, row[0]))
                pending = c.execute("""SELECT 1 FROM cancellation_targets
                    WHERE job_id=? AND request_id=? AND observed=0 LIMIT 1""",
                    (job_ref.job_id, row[0])).fetchone()
                _save(c, "cancel", job_ref, row[0], replace(
                    prior, revision=prior.revision + 1,
                    outcome="requested" if pending else "observed_stop", cleanup=cleanup))

    def get_effect_receipt(self, job_ref, effect_id):
        with connection(self, metadata_only=True) as c:
            self.validate_job_ref(job_ref, connection=c)
            raw = _load(c, "effect", job_ref, effect_id)
            return _effect(raw) if raw else None

    def _accept_effect(self, c, receipt):
        self.validate_job_ref(receipt.job_ref, connection=c, strict=True)
        for key in (receipt.effect_id, receipt.run_id, receipt.attempt_id, receipt.request_digest):
            _key(key)
        if receipt.replay_policy not in {"safe", "reconcile_first", "requires_authorization", "provider_idempotent"}:
            raise ValueError("invalid replay policy")
        if receipt.revision != 1 or receipt.outcome != "not_dispatched" or receipt.evidence_refs:
            raise ValueError("new effect must start not_dispatched at revision 1")
        raw = _load(c, "effect", receipt.job_ref, receipt.effect_id)
        if raw:
            prior = _effect(raw)
            # Explicit v2 authorization permits replay of the persisted v1 identity.
            comparison = prior
            if prior.job_ref.version == 1:
                self.validate_job_ref(prior.job_ref, connection=c)
                comparison = replace(prior, job_ref=receipt.job_ref)
            if replace(comparison, revision=1, outcome="not_dispatched", evidence_refs=()) != receipt:
                raise ContractConflict("effect intent has different immutable facts")
            return prior, False
        task = self.get_task_by_id(receipt.binding.task_id)
        if (task.job_id != receipt.job_ref.job_id or task_binding(task) != receipt.binding
                or task.status != TaskStatus.RUNNING or receipt.binding.generation is None
                or not receipt.binding.lease_id or not receipt.binding.owner):
            raise ContractConflict("stale effect lease binding")
        attempts = self.list_attempts(receipt.job_ref.job_id, task_id=task.id)
        if not any(a.attempt_id == receipt.attempt_id and a.run_id == receipt.run_id for a in attempts):
            raise ValueError("effect requires its recorded invocation attempt")
        _save(c, "effect", receipt.job_ref, receipt.effect_id, receipt)
        return receipt, True

    def record_effect(self, receipt):
        """Accept immutable logical intent. Exact replay is a read, never dispatch."""
        with _transaction(self) as c:
            return self._accept_effect(c, receipt)[0]

    def execute_effect(self, intent, operation):
        """Execute one new logical effect; retries return the durable receipt.

        ``operation`` must return an EffectObservation. Neither exceptions nor
        missing evidence imply failed_no_effect. Persistence precedes dispatch;
        a crash or failed observation write leaves in_flight for reconciliation.
        """
        from puppetmaster.contracts import EffectObservation
        with _transaction(self) as c:
            receipt, created = self._accept_effect(c, intent)
            if not created:
                return receipt
            _check_effect_cancellation(c, intent)
            flight = replace(receipt, revision=2, outcome="in_flight",
                             evidence_refs=("dispatch:" + intent.attempt_id,))
            _save(c, "effect", intent.job_ref, intent.effect_id, flight)
        try:
            observation = operation()
            if not isinstance(observation, EffectObservation):
                raise ValueError("effect callback must return EffectObservation")
        except BaseException as exc:
            try:
                self.advance_effect(intent.job_ref, intent.effect_id, expected_revision=2,
                                    outcome="unknown", evidence_refs=("exception:" + type(exc).__name__,))
            except Exception:
                # Durable in_flight remains a replay fence if reconciliation fails.
                pass
            raise
        return self.advance_effect(intent.job_ref, intent.effect_id, expected_revision=2,
                                   outcome=observation.outcome, evidence_refs=observation.evidence_refs)

    def advance_effect(self, job_ref, effect_id, *, expected_revision, outcome, evidence_refs=()):
        """CAS observation update. Unknown can be reconciled, never redispatched."""
        self.validate_job_ref(job_ref, strict=True)
        if outcome not in {"in_flight", "succeeded", "failed_no_effect", "unknown"}:
            raise ValueError("invalid effect outcome")
        from puppetmaster.contracts import bounded_evidence
        evidence_refs = bounded_evidence(evidence_refs)
        with _transaction(self) as c:
            self.validate_job_ref(job_ref, connection=c, strict=True)
            raw = _load(c, "effect", job_ref, effect_id)
            if not raw:
                raise KeyError(effect_id)
            prior = _effect(raw)
            if prior.revision == expected_revision + 1 and prior.outcome == outcome and prior.evidence_refs == evidence_refs:
                return prior
            if prior.revision != expected_revision:
                raise ContractConflict("effect revision conflict")
            allowed = {"not_dispatched": {"in_flight", "failed_no_effect"},
                       "in_flight": {"succeeded", "failed_no_effect", "unknown"},
                       "unknown": {"succeeded", "failed_no_effect"}}
            if outcome not in allowed.get(prior.outcome, set()):
                raise ContractConflict("effect transition requires reconciliation or a new authorization")
            if outcome == "in_flight":
                _check_effect_cancellation(c, prior)
                task = self.get_task_by_id(prior.binding.task_id)
                if task_binding(task) != prior.binding or task.status != TaskStatus.RUNNING:
                    raise ContractConflict("stale effect lease binding")
            receipt = replace(prior, revision=prior.revision + 1, outcome=outcome,
                              evidence_refs=evidence_refs)
            _save(c, "effect", job_ref, effect_id, receipt)
            return receipt
