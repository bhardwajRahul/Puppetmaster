"""Public, bounded store contracts. Identities always include the owning store."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from puppetmaster.bounded_json import loads as metadata_json_loads
from dataclasses import dataclass
from typing import Literal, Optional, Tuple

from puppetmaster.selected_economics import SelectedEconomics, SelectedMetric, SelectedTotals
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
    outcome: Literal["pending_publication", "published", "stale_lease", "invalidated", "legacy_unknown", "unavailable"]


@dataclass(frozen=True)
class TaskBinding:
    task_id: str
    generation: Optional[int]
    lease_id: Optional[str]
    owner: Optional[str]

    def __post_init__(self):
        for name, value in (('task_id', self.task_id), ('lease_id', self.lease_id), ('owner', self.owner)):
            if value is None and name != 'task_id':
                continue
            if not isinstance(value, str) or not value or len(value) > 4096 or len(value.encode()) > 4096:
                raise ValueError("task binding requires bounded string identities")
        if self.generation is not None and (type(self.generation) is not int or not 0 <= self.generation <= 9223372036854775807):
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


def bounded_evidence(values):
    from itertools import islice
    values = tuple(islice(values, 201))
    if not values or len(values) > 200:
        raise ValueError("provide 1..200 evidence refs")
    total = 0
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("invalid evidence ref")
        size = len(value.encode('utf-8'))
        total += size
        if size > 4096 or total > 65536:
            raise ValueError("evidence refs exceed byte budget")
    return values


@dataclass(frozen=True)
class EffectObservation:
    outcome: Literal["succeeded", "failed_no_effect", "unknown"]
    evidence_refs: Tuple[str, ...]

    def __post_init__(self):
        if self.outcome not in {"succeeded", "failed_no_effect", "unknown"}:
            raise ValueError("invalid effect observation")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("effect observation requires immutable evidence refs")
        bounded_evidence(self.evidence_refs)


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
    previous_membership: Literal["present", "absent", "unavailable"] = "unavailable"
    previous_status: Optional[str] = None
    previous_origin: Optional[str] = None
    previous_project_id: Optional[str] = None
    previous_session_id: Optional[str] = None

    goal_preview: Optional[str] = None
    goal_preview_truncated: Optional[bool] = None
    delivery: Literal["pending", "blocked", "unverified", "unavailable"] = "unavailable"
    quality: Literal["unverified", "unavailable"] = "unavailable"


JobSummary = MetadataRef


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
    reason: Optional[str] = None
    retry_after_ms: Optional[int] = None


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
        token = base64.urlsafe_b64encode(hmac.digest(self.secret, body, "sha256") + body).decode()
        if len(token) > 4096:
            from puppetmaster.readonly import ReadUnavailable
            raise ReadUnavailable("unable to open metadata continuation: cursor byte budget exceeded")
        return token

    @staticmethod
    def inspect(token):
        if type(token) is not str or not token or len(token) > 4096:
            raise ValueError("invalid cursor")
        try:
            raw = base64.b64decode(token, altchars=b"-_", validate=True)
            value = metadata_json_loads(raw[32:])
            if (not isinstance(value, dict) or type(value.get('v')) is not int
                    or value['v'] != 1 or not isinstance(value.get('scope'), str)):
                raise ValueError("invalid cursor")
            return raw, value
        except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid cursor") from exc

    def decode(self, token: str, scope: str) -> dict:
        try:
            raw, value = self.inspect(token)
            signature, body = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.secret, body, "sha256")):
                raise ValueError("invalid cursor")
            if value.get("scope") != scope:
                raise ValueError("cursor does not match query")
            return value
        except (TypeError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("invalid cursor") from exc
