"""Public, bounded store contracts. Identities always include the owning store."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Literal, Optional, Tuple

from puppetmaster.models import JobRef, to_jsonable

MAX_PAGE = 200
MAX_BYTES = 262144
MAX_SCAN = 1000


class ContractConflict(ValueError):
    """An immutable identity was reused with different facts."""


def immutable_digest(value) -> str:
    return hashlib.sha256(json.dumps(to_jsonable(value), sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class CompletionReceipt:
    job_ref: JobRef
    run_id: str
    intent_digest: Optional[str]
    outcome: Literal["pending_publication", "published", "stale_lease", "invalidated", "legacy_unknown"]


@dataclass(frozen=True)
class TaskBinding:
    task_id: str
    generation: Optional[int]
    lease_id: Optional[str]
    owner: Optional[str]

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("task binding requires a task id")
        if self.generation is not None and (type(self.generation) is not int or self.generation < 0):
            raise ValueError("task generation must be nonnegative or unknown")


@dataclass(frozen=True)
class CancellationReceipt:
    job_ref: JobRef
    request_id: str
    bindings: Tuple[TaskBinding, ...]
    outcome: Literal["requested", "observed_stop", "stale_binding", "already_terminal", "conflict"]
    revision: int
    cleanup: Literal["unknown", "partial", "local_process_exited"] = "unknown"


@dataclass(frozen=True)
class EffectReceipt:
    job_ref: JobRef
    effect_id: str
    request_digest: str
    binding: TaskBinding
    run_id: str
    attempt_id: str
    revision: int
    outcome: Literal["not_dispatched", "in_flight", "succeeded", "failed_no_effect", "unknown"]
    replay_policy: Literal["safe", "reconcile_first", "requires_authorization", "provider_idempotent"]
    evidence_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectObservation:
    outcome: Literal["succeeded", "failed_no_effect", "unknown"]
    evidence_refs: Tuple[str, ...]

    def __post_init__(self):
        if self.outcome not in {"succeeded", "failed_no_effect", "unknown"}:
            raise ValueError("invalid effect observation")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("effect observation requires immutable evidence refs")


@dataclass(frozen=True)
class MetadataRef:
    job_ref: JobRef
    id: str
    kind: Literal["job", "task", "artifact"]
    status: Optional[str]
    sha256: Optional[str]
    revision: int
    stamp: Literal["known", "legacy_unknown"]
    deleted: bool = False
    task_count: Optional[int] = None
    artifact_count: Optional[int] = None
    binding: Optional[TaskBinding] = None
    task_id: Optional[str] = None
    artifact_type: Optional[str] = None
    origin: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class JobSummaryFilter:
    status: Optional[str] = None
    job_ref: Optional[JobRef] = None
    origin: Optional[str] = None
    project_id: Optional[str] = None
    session_id: Optional[str] = None


@dataclass(frozen=True)
class MetadataPage:
    items: Tuple[MetadataRef, ...]
    outcome: Literal["complete", "partial", "unavailable", "cursor_expired"]
    revision: int
    next_cursor: Optional[str] = None
    scanned: int = 0


@dataclass(frozen=True)
class ProcessCleanupReceipt:
    local_process: Literal["observed_exit", "unknown"]
    descendants: Literal["partial", "unknown"]
    remote_effects: Literal["unknown"] = "unknown"


class CursorCodec:
    """Authenticated, versioned tokens scoped to a store, query and snapshot."""

    def __init__(self, secret: bytes):
        self.secret = secret

    def encode(self, value: dict) -> str:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(hmac.digest(self.secret, body, "sha256") + body).decode()

    def decode(self, token: str, scope: str) -> dict:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError("invalid cursor")
        try:
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            signature, body = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.secret, body, "sha256")):
                raise ValueError("invalid cursor")
            value = json.loads(body)
            if value.get("v") != 1 or value.get("scope") != scope:
                raise ValueError("cursor does not match query")
            return value
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("invalid cursor") from exc
