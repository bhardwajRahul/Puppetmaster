"""Per-job run journal + crash-recovery stamps (StrongOrc-adjacent durability).

Comet/Zeron lesson: an on-disk append-only journal is the crash gauge. A
journal whose last event is not a terminal ``done`` / ``aborted`` belongs to
a run that died mid-stream. Boot recovery stamps ``aborted`` and closes the
journal. Auto-resume is bounded by a persisted attempt counter so an engine
that crashes-on-revive cannot loop forever.

This is orchestration durability — not model rank. It sits beside
:mod:`puppetmaster.liveness` (stall reaper) and
:mod:`puppetmaster.host_lifecycle` (host.started / host.recovered).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from puppetmaster.models import now_iso

JOURNAL_DIRNAME = "run-journals"
MAX_AUTO_RESUME = 3
TERMINAL_KINDS = frozenset({"done", "aborted", "failed", "cancelled"})


@dataclass(frozen=True)
class JournalEvent:
    seq: int
    kind: str
    at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JournalEvent":
        return cls(
            seq=int(raw.get("seq") or 0),
            kind=str(raw.get("kind") or ""),
            at=str(raw.get("at") or ""),
            payload=dict(raw.get("payload") or {}),
        )

    @property
    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_KINDS


class RunJournal:
    """Append-only JSONL journal, one file per job under ``run-journals/``."""

    def __init__(self, root: Union[Path, str], job_id: str) -> None:
        self.root = Path(root)
        self.job_id = str(job_id).strip()
        if not self.job_id:
            raise ValueError("job_id required")
        self._dir = self.root / JOURNAL_DIRNAME
        safe = _safe_id(self.job_id)
        self._path = self._dir / f"{safe}.jsonl"
        self._resume_path = self._dir / f"{safe}.resume"
        self._lock = threading.Lock()

    def append(self, kind: str, payload: Optional[dict[str, Any]] = None) -> JournalEvent:
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            next_seq = self._next_seq_unlocked()
            event = JournalEvent(
                seq=next_seq,
                kind=str(kind),
                at=now_iso(),
                payload=dict(payload or {}),
            )
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
            return event

    def replay(self, after_seq: int = 0) -> list[JournalEvent]:
        events = self._read_all()
        return [event for event in events if event.seq > after_seq]

    def last_event(self) -> Optional[JournalEvent]:
        events = self._read_all()
        return events[-1] if events else None

    def is_stale(self) -> bool:
        last = self.last_event()
        if last is None:
            return False
        return not last.is_terminal

    def stamp_aborted(self, *, reason: str = "crash_recovery") -> Optional[JournalEvent]:
        """Close a mid-stream journal with a synthetic aborted terminal."""
        last = self.last_event()
        if last is None or last.is_terminal:
            return None
        return self.append(
            "aborted",
            {
                "reason": reason,
                "closed_seq": last.seq,
                "closed_kind": last.kind,
            },
        )

    def resume_attempts(self) -> int:
        if not self._resume_path.is_file():
            return 0
        try:
            return int(self._resume_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            return 0

    def note_resume_attempt(self) -> int:
        next_count = self.resume_attempts() + 1
        self._dir.mkdir(parents=True, exist_ok=True)
        self._resume_path.write_text(str(next_count), encoding="utf-8")
        return next_count

    def clear_resume_attempts(self) -> None:
        try:
            self._resume_path.unlink()
        except FileNotFoundError:
            pass

    def may_auto_resume(self, *, max_attempts: int = MAX_AUTO_RESUME) -> bool:
        return self.resume_attempts() < max_attempts

    def _next_seq_unlocked(self) -> int:
        last = self.last_event()
        return (last.seq + 1) if last else 1

    def _read_all(self) -> list[JournalEvent]:
        if not self._path.is_file():
            return []
        events: list[JournalEvent] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Torn trailing line from a crash mid-write — tolerate.
                continue
            if isinstance(data, dict):
                events.append(JournalEvent.from_dict(data))
        return events


def journal_for_store(store: Any, job_id: str) -> RunJournal:
    return RunJournal(Path(store.root), job_id)


def list_stale_job_ids(root: Union[Path, str]) -> list[str]:
    """Job ids whose journal's last event is not terminal."""
    directory = Path(root) / JOURNAL_DIRNAME
    if not directory.is_dir():
        return []
    stale: list[str] = []
    for path in sorted(directory.glob("*.jsonl")):
        job_id = path.stem
        journal = RunJournal(root, job_id)
        if journal.is_stale():
            stale.append(job_id)
    return stale


@dataclass(frozen=True)
class CrashRecoveryStamp:
    job_id: str
    aborted: bool
    resume_attempts: int
    may_auto_resume: bool
    reason: str


def recover_stale_journals(
    store: Any,
    *,
    reason: str = "host.recovered",
    max_auto_resume: int = MAX_AUTO_RESUME,
    emit: bool = True,
) -> list[CrashRecoveryStamp]:
    """Stamp aborted on mid-stream journals during crash recovery.

    Does **not** auto-relaunch workers. Callers that want revive must check
    ``may_auto_resume`` and bump ``note_resume_attempt`` themselves.
    """
    root = Path(store.root)
    stamps: list[CrashRecoveryStamp] = []
    for job_id in list_stale_job_ids(root):
        journal = RunJournal(root, job_id)
        attempts = journal.resume_attempts()
        may = attempts < max_auto_resume
        closed = journal.stamp_aborted(reason=reason)
        stamp = CrashRecoveryStamp(
            job_id=job_id,
            aborted=closed is not None,
            resume_attempts=attempts,
            may_auto_resume=may,
            reason=reason,
        )
        stamps.append(stamp)
        if emit and closed is not None:
            try:
                store.emit(
                    job_id,
                    "run.journal.aborted",
                    {
                        "reason": reason,
                        "resume_attempts": attempts,
                        "may_auto_resume": may,
                        "closed_seq": closed.payload.get("closed_seq"),
                    },
                )
            except Exception:
                pass
    return stamps


def _safe_id(job_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in job_id)
