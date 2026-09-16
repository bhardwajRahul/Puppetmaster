"""Universal live steering over the existing session command ledger.

Native adapter channels (Codex app-server) are an optimization. Every
adapter still consumes queued commands at the next shared-runtime boundary:
pre-dispatch, mid-turn (agentic / multi-turn), repair retry, and post-output.
A native ack is delivery acceptance, not proof the model applied the text.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, List, Optional, Sequence

from puppetmaster.models import Task, TaskStatus, now_iso
from puppetmaster.session_commands import (
    CommandDisposition,
    JobCommandLedger,
    SessionCommandEntry,
    SessionCommandKind,
    SessionCommandStatus,
    ledger_for_store,
)


BOUNDARY_PRE_DISPATCH = "pre_dispatch"
BOUNDARY_MID_TURN = "mid_turn"
BOUNDARY_REPAIR = "repair"
BOUNDARY_POST_OUTPUT = "post_output"

STATUS_QUEUED = "queued"
STATUS_ACCEPTED_NATIVE = "accepted_native"
STATUS_QUEUED_FOR_SUCCESSOR = "queued_for_successor"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_SUPERSEDED = "superseded"
STATUS_APPLIED = "applied"

_LIVE_TASK = {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.BLOCKED}
_TERMINAL_TASK = {TaskStatus.COMPLETE, TaskStatus.FAILED, TaskStatus.SKIPPED}


def enqueue_steer(
    store: Any,
    job_id: str,
    message: str,
    *,
    task_id: Optional[str] = None,
    issued_by: str = "host",
    kind: SessionCommandKind = SessionCommandKind.STEER,
) -> dict:
    """Append a steer/interrupt and emit a receipt."""
    text = str(message or "").strip()
    if not text:
        raise ValueError("steer message must be non-empty")
    payload = {"text": text, "delivery": STATUS_QUEUED}
    if task_id:
        payload["task_id"] = task_id
    entry = ledger_for_store(store, job_id).append(
        kind,
        issued_by=issued_by,
        payload=payload,
    )
    _emit(
        store,
        job_id,
        "steer.queued",
        {
            "command_id": entry.id,
            "task_id": task_id,
            "kind": entry.kind.value,
            "issued_by": issued_by,
            "delivery": STATUS_QUEUED,
        },
    )
    return entry.to_dict()


def broadcast_steer(
    store: Any,
    job_id: str,
    message: str,
    *,
    issued_by: str = "host",
) -> List[dict]:
    """Fan one message out to every live task (plus a record-only job entry)."""
    entries = [
        enqueue_steer(
            store,
            job_id,
            message,
            task_id=task.id,
            issued_by=issued_by,
        )
        for task in store.list_tasks(job_id)
        if task.status in _LIVE_TASK
    ]
    text = str(message or "").strip()
    record = ledger_for_store(store, job_id).append(
        SessionCommandKind.STEER,
        issued_by=issued_by,
        payload={"text": text, "broadcast": True, "record_only": True, "delivery": STATUS_QUEUED},
    )
    entries.append(record.to_dict())
    return entries


def drain_pending(
    store: Any,
    task: Task,
    *,
    boundary: str,
    native_deliver: Optional[Callable[[str], Optional[str]]] = None,
) -> List[str]:
    """Consume pending commands for ``task`` at a runtime boundary.

    Returns texts the caller must inject into the next model turn.
    Post-output EXECUTE on a finished one-shot adapter is receipted
    ``queued_for_successor`` and not applied here.
    """
    applied: List[str] = []
    ledger = ledger_for_store(store, task.job_id)
    for entry, disposition in ledger.evaluate_pending():
        if not _targets_task(entry, task):
            continue
        if disposition != CommandDisposition.EXECUTE:
            ledger.apply_disposition(entry, disposition)
            _emit(
                store,
                task.job_id,
                "steer.%s" % disposition.value,
                {"command_id": entry.id, "task_id": task.id, "delivery": disposition.value},
            )
            continue
        if entry.kind == SessionCommandKind.INTERRUPT:
            ledger.apply_disposition(entry, disposition)
            _interrupt(store, task, ledger, entry)
            continue
        text = _command_text(entry)
        if not text:
            ledger.apply_disposition(entry, disposition)
            ledger.rewrite_status(
                entry.id,
                SessionCommandStatus.CANCELLED,
                resolution=STATUS_CANCELLED,
            )
            continue
        if boundary == BOUNDARY_POST_OUTPUT:
            ledger.rewrite_status(
                entry.id,
                SessionCommandStatus.PENDING,
                resolution=STATUS_QUEUED_FOR_SUCCESSOR,
            )
            _emit(
                store,
                task.job_id,
                "steer.queued_for_successor",
                {"command_id": entry.id, "task_id": task.id},
            )
            continue
        if native_deliver is not None:
            try:
                native_status = native_deliver(text)
            except Exception:
                native_status = None
            if native_status == STATUS_ACCEPTED_NATIVE:
                ledger.apply_disposition(entry, disposition)
                ledger.rewrite_status(
                    entry.id,
                    SessionCommandStatus.APPLIED,
                    resolution=STATUS_ACCEPTED_NATIVE,
                    mapped_task_id=task.id,
                )
                _emit(
                    store,
                    task.job_id,
                    "steer.accepted_native",
                    {
                        "command_id": entry.id,
                        "task_id": task.id,
                        "note": "ack is not model-applied",
                    },
                )
                continue
        ledger.apply_disposition(entry, disposition)
        ledger.rewrite_status(
            entry.id,
            SessionCommandStatus.APPLIED,
            resolution=STATUS_APPLIED,
            mapped_task_id=task.id,
        )
        applied.append(text)
        _emit(
            store,
            task.job_id,
            "steer.applied",
            {"command_id": entry.id, "task_id": task.id, "boundary": boundary},
        )
    return applied


def apply_steering_to_task(task: Task, messages: Sequence[str]) -> Task:
    """Stamp steering text onto instruction + payload for the next execute."""
    texts = [str(item).strip() for item in messages if str(item).strip()]
    if not texts:
        return task
    extra = "\n\nHost steering:\n" + "\n".join("- %s" % item for item in texts)
    payload = dict(task.payload or {})
    prior = [str(item) for item in payload.get("steering_messages") or [] if item]
    payload["steering_messages"] = prior + texts
    return replace(task, instruction=str(task.instruction or "") + extra, payload=payload)


def spawn_successors_for_job(store: Any, job_id: str) -> List[Task]:
    """Create one successor task per post-output steer still waiting."""
    from puppetmaster.models import new_id

    ledger = ledger_for_store(store, job_id)
    spawned: List[Task] = []
    seen = set()
    for entry in ledger.list_entries():
        if entry.status != SessionCommandStatus.PENDING:
            continue
        if entry.resolution != STATUS_QUEUED_FOR_SUCCESSOR:
            continue
        parent_id = str(
            (entry.payload or {}).get("task_id") or entry.mapped_task_id or ""
        )
        if not parent_id or parent_id in seen:
            continue
        parent = _task_by_id(store, job_id, parent_id)
        if parent is None or parent.status not in _TERMINAL_TASK:
            continue
        generation = int((parent.payload or {}).get("successor_generation") or 0) + 1
        if generation > 8:
            ledger.rewrite_status(
                entry.id, SessionCommandStatus.EXPIRED, resolution=STATUS_EXPIRED
            )
            continue
        seen.add(parent_id)
        text = _command_text(entry)
        child_payload = dict(parent.payload or {})
        child_payload["steering_successor"] = True
        child_payload["successor_of"] = parent.id
        child_payload["successor_generation"] = generation
        child = replace(
            parent,
            id=new_id("task"),
            status=TaskStatus.QUEUED,
            depends_on=list(parent.depends_on or []) + [parent.id],
            instruction=str(parent.instruction or "")
            + ("\n\nHost steering:\n- %s" % text if text else ""),
            payload=child_payload,
            attempts=0,
            lease_owner=None,
            lease_expires_at=None,
            lease_id=None,
            completed_at=None,
            updated_at=now_iso(),
        )
        store.save_task(child)
        ledger.apply_disposition(entry, CommandDisposition.EXECUTE)
        ledger.rewrite_status(
            entry.id,
            SessionCommandStatus.APPLIED,
            resolution=STATUS_APPLIED,
            mapped_task_id=child.id,
        )
        _emit(
            store,
            job_id,
            "steer.successor",
            {"parent_id": parent.id, "task_id": child.id, "command_id": entry.id},
        )
        spawned.append(child)
    return spawned


def pending_steer_texts(store: Any, task: Task) -> List[str]:
    texts = []
    for entry, disposition in ledger_for_store(store, task.job_id).evaluate_pending():
        if disposition != CommandDisposition.EXECUTE:
            continue
        if not _targets_task(entry, task):
            continue
        if entry.kind == SessionCommandKind.INTERRUPT:
            continue
        text = _command_text(entry)
        if text:
            texts.append(text)
    return texts


def native_steer_items(store: Any, task: Task) -> List[dict]:
    """Items for Codex ``pending_steering``: id, text, acknowledge."""
    items = []
    ledger = ledger_for_store(store, task.job_id)

    def _ack(command_id: str, state: str, detail: str = "") -> None:
        if state == "accepted":
            ledger.rewrite_status(
                command_id,
                SessionCommandStatus.APPLIED,
                resolution=STATUS_ACCEPTED_NATIVE,
                mapped_task_id=task.id,
            )
            _emit(
                store,
                task.job_id,
                "steer.accepted_native",
                {"command_id": command_id, "task_id": task.id, "detail": detail},
            )

    for entry, disposition in ledger.evaluate_pending():
        if disposition != CommandDisposition.EXECUTE:
            continue
        if not _targets_task(entry, task):
            continue
        if entry.kind != SessionCommandKind.STEER:
            continue
        text = _command_text(entry)
        if not text:
            continue
        ledger.apply_disposition(entry, disposition)
        items.append(
            {
                "id": entry.id,
                "text": text,
                "acknowledge": lambda state, detail="", _id=entry.id: _ack(_id, state, detail),
            }
        )
    return items


def _targets_task(entry: SessionCommandEntry, task: Task) -> bool:
    payload = entry.payload or {}
    if payload.get("record_only"):
        return False
    target = payload.get("task_id") or entry.mapped_task_id
    if target in (None, "", task.id):
        return True
    return False


def _command_text(entry: SessionCommandEntry) -> str:
    payload = entry.payload or {}
    return str(payload.get("text") or payload.get("prompt") or "").strip()


def _interrupt(
    store: Any,
    task: Task,
    ledger: JobCommandLedger,
    entry: SessionCommandEntry,
) -> None:
    try:
        from puppetmaster.cancellation import request_cancel

        request_cancel(task.job_id)
    except Exception:
        pass
    ledger.rewrite_status(
        entry.id,
        SessionCommandStatus.CANCELLED,
        resolution=STATUS_CANCELLED,
        mapped_task_id=task.id,
        mapped_request_id=entry.id,
    )
    _emit(
        store,
        task.job_id,
        "steer.interrupt",
        {"command_id": entry.id, "task_id": task.id},
    )


def _task_by_id(store: Any, job_id: str, task_id: str) -> Optional[Task]:
    getter = getattr(store, "get_task_by_id", None)
    if callable(getter):
        try:
            found = getter(task_id)
            if found is not None:
                return found
        except Exception:
            pass
    for task in store.list_tasks(job_id):
        if task.id == task_id:
            return task
    return None


def _emit(store: Any, job_id: str, event: str, payload: dict) -> None:
    if store is None or not hasattr(store, "emit"):
        return
    try:
        store.emit(job_id, event, payload)
    except Exception:
        pass
