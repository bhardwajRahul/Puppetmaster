from __future__ import annotations

"""Transition-oracle edges.

Conflict-auditor (V1) may skip on opt-in: that replaces a model call.
Already-answered, FINDING admission, and stop-spawn stay observe-only
unless ``PUPPETMASTER_JEV_ACT``. Jev never invents FINDINGs.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from puppetmaster.claim_conflicts import detect_contradictory_peers
from puppetmaster.models import Artifact, ArtifactType, JobStatus, Task, TaskStatus

from . import acting, decide, enabled, opted_in, parse_noul
from .artifacts import build_skip_verification, build_transition_gate
from .questions import (
    ADMISSION_INSTRUCTIONS,
    ADMISSION_THRESHOLD,
    ALREADY_ANSWERED_INSTRUCTIONS,
    ALREADY_ANSWERED_THRESHOLD,
    CHARS_PER_TOKEN,
    CLAIM_CHARS,
    CONFLICT_THRESHOLD,
    CONTRADICT_INSTRUCTIONS,
    EVIDENCE_LOCI,
    GOAL_CHARS,
    PAIR_CAP,
    STOP_INSTRUCTIONS,
    STOP_THRESHOLD,
    TOKEN_BUDGET,
)

_CLAIM_TYPES = frozenset(
    {ArtifactType.FINDING, ArtifactType.GIST, ArtifactType.DECISION}
)
_PRIOR_JOB_SCAN = 30
_PRIOR_CLAIM_CAP = 20

EDGE_CONFLICT_AUDITOR = "conflict_auditor"
EDGE_ALREADY_ANSWERED = "already_answered"
EDGE_FINDING_ADMISSION = "finding_admission"
EDGE_STOP_SPAWN = "stop_spawn"

_READY = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING})


@dataclass(frozen=True)
class TransitionDecision:
    action: str
    reason: str
    threshold: float = CONFLICT_THRESHOLD
    max_noul: Optional[float] = None
    pairs_scored: int = 0
    pairs_capped: bool = False
    evidence: Tuple[str, ...] = ()
    write_gate: bool = True
    would_action: str = ""
    acted: bool = False


def _would(decision: TransitionDecision) -> str:
    return decision.would_action or decision.action


def may_act(edge: str) -> bool:
    """V1 acts on opt-in. Other edges need the ACT experiment switch."""
    if edge == EDGE_CONFLICT_AUDITOR:
        return opted_in()
    return acting()


def _bind_applied(
    decision: TransitionDecision, *, edge: str
) -> TransitionDecision:
    """Stamp applied action. Non-V1 observe-only never returns skip."""
    would = _would(decision)
    applied = "skip" if may_act(edge) and would == "skip" else "spawn"
    return replace(
        decision,
        action=applied,
        would_action=would,
        acted=applied == "skip",
    )


def _persist_bound_gate(
    store: Any,
    *,
    job_id: str,
    task_id: str,
    edge: str,
    decision: TransitionDecision,
) -> TransitionDecision:
    bound = _bind_applied(decision, edge=edge)
    if decision.write_gate:
        _persist(
            store,
            build_transition_gate(
                job_id=job_id,
                task_id=task_id,
                edge=edge,
                action=bound.action,
                would_action=bound.would_action,
                acted=bound.acted,
                reason=bound.reason,
                threshold=bound.threshold,
                max_noul=bound.max_noul,
                pairs_scored=bound.pairs_scored,
                pairs_capped=bound.pairs_capped,
                evidence=bound.evidence,
            ),
        )
    return bound


def _fresh_judgment_roles() -> frozenset:
    try:
        from puppetmaster.orchestrator import _FRESH_JUDGMENT_ROLES

        return frozenset(_FRESH_JUDGMENT_ROLES)
    except Exception:
        return frozenset({"conflict-auditor", "review", "audit", "redteam", "test"})


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _claim_text(artifact: Artifact) -> str:
    return str((artifact.payload or {}).get("claim") or "").strip()


def _task_role(store: Any, artifact: Artifact) -> str:
    task_id = getattr(artifact, "task_id", None)
    if not task_id:
        return ""
    try:
        task = store.get_task_by_id(task_id)
    except Exception:
        return ""
    return str(getattr(task, "role", "") or "")


def collect_peer_findings(store: Any, job_id: str) -> List[Artifact]:
    """FINDINGs with a claim, excluding fresh-judgment roles' own conclusions."""
    excluded = _fresh_judgment_roles()
    peers: List[Artifact] = []
    try:
        artifacts = list(store.list_artifacts(job_id) or [])
    except Exception:
        return []
    for artifact in artifacts:
        if getattr(artifact, "type", None) != ArtifactType.FINDING:
            continue
        if not _claim_text(artifact):
            continue
        if _task_role(store, artifact) in excluded:
            continue
        peers.append(artifact)
    return peers


def _job_cwd(store: Any, job_id: str) -> Optional[Path]:
    try:
        for task in store.list_tasks(job_id):
            payload = getattr(task, "payload", None) or {}
            cwd = payload.get("cwd")
            if cwd:
                path = Path(str(cwd))
                if path.is_dir():
                    return path
    except Exception:
        return None
    return None


def _job_goal(store: Any, job_id: str) -> str:
    try:
        job = store.get_job(job_id)
    except Exception:
        return ""
    return _clip(getattr(job, "goal", "") or "", GOAL_CHARS)


def _pair_state(goal: str, left: Artifact, right: Artifact) -> dict:
    def _side(artifact: Artifact) -> dict:
        evidence = [str(item) for item in (artifact.evidence or []) if str(item).strip()]
        return {
            "id": artifact.id,
            "claim": _clip(_claim_text(artifact), CLAIM_CHARS),
            "evidence": evidence[:EVIDENCE_LOCI],
            "confidence": float(getattr(artifact, "confidence", 0.0) or 0.0),
        }

    return {"goal": goal, "left": _side(left), "right": _side(right)}


def _ranked_pairs(findings: Sequence[Artifact]) -> List[Tuple[Artifact, Artifact]]:
    indexed = list(findings)
    pairs: List[Tuple[Artifact, Artifact, float]] = []
    for i, left in enumerate(indexed):
        for right in indexed[i + 1 :]:
            rank = max(
                float(getattr(left, "confidence", 0.0) or 0.0),
                float(getattr(right, "confidence", 0.0) or 0.0),
            )
            pairs.append((left, right, rank))
    pairs.sort(key=lambda item: (-item[2], item[0].id, item[1].id))
    return [(left, right) for left, right, _rank in pairs]


def _estimate_tokens(state: Any, questions: dict) -> int:
    try:
        import json

        blob = json.dumps({"state": state, "questions": questions}, default=str)
    except (TypeError, ValueError):
        blob = str(state) + str(questions)
    return max(1, (len(blob) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def _score_pairs(
    goal: str,
    pairs: Sequence[Tuple[Artifact, Artifact]],
) -> Tuple[Optional[float], int, Tuple[str, ...], Optional[str]]:
    """Return (max_noul, scored, evidence, fail_reason)."""
    if not pairs:
        return (0.0, 0, (), None)
    questions = {}
    states = []
    keys = []
    for index, (left, right) in enumerate(pairs):
        key = "contradict_%d" % index
        keys.append((key, left, right))
        questions[key] = {
            "type": "noul",
            "instructions": CONTRADICT_INSTRUCTIONS,
        }
        states.append(_pair_state(goal, left, right))
    payload_state = {"goal": goal, "pairs": states}
    if _estimate_tokens(payload_state, questions) > TOKEN_BUDGET:
        return (None, 0, ("fail-open:token_budget",), "fail_open")
    try:
        body = decide(payload_state, questions)
    except Exception as exc:
        return (None, 0, ("fail-open:%s" % type(exc).__name__,), "fail_open")
    if body is None:
        return (None, 0, ("fail-open:DecideNone",), "fail_open")
    answers = body.get("answers")
    max_noul: Optional[float] = None
    max_ids: Tuple[str, ...] = ()
    scored = 0
    for key, left, right in keys:
        noul = parse_noul(answers, key)
        if noul is None:
            return (None, scored, ("fail-open:incomplete_scoring",), "fail_open")
        scored += 1
        if max_noul is None or noul > max_noul:
            max_noul = noul
            max_ids = (left.id, right.id)
    return (max_noul, scored, max_ids, None)


def decide_conflict_auditor(
    store: Any,
    job_id: str,
    *,
    persist_not_opted_in: bool = False,
) -> TransitionDecision:
    """Choose skip or spawn for a conflict-auditor edge. Never raises."""
    try:
        return _decide_conflict_auditor(
            store, job_id, persist_not_opted_in=persist_not_opted_in
        )
    except Exception as exc:
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            evidence=("fail-open:%s" % type(exc).__name__,),
        )


def _decide_conflict_auditor(
    store: Any,
    job_id: str,
    *,
    persist_not_opted_in: bool,
) -> TransitionDecision:
    if not opted_in():
        return TransitionDecision(
            action="spawn",
            reason="not_opted_in",
            write_gate=persist_not_opted_in,
        )
    if not enabled():
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            evidence=("fail-open:MissingKey",),
        )
    findings = collect_peer_findings(store, job_id)
    if len(findings) < 2:
        return TransitionDecision(
            action="spawn",
            reason="thin_set",
            evidence=tuple(item.id for item in findings),
        )
    cwd = _job_cwd(store, job_id)
    try:
        conflicts = detect_contradictory_peers(findings, cwd=cwd)
    except Exception as exc:
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            evidence=("fail-open:%s" % type(exc).__name__,),
        )
    if conflicts:
        ids = tuple(conflicts[0].artifact_ids)
        return TransitionDecision(
            action="spawn",
            reason="mechanical_conflict",
            evidence=("mechanical:%s,%s" % (ids[0], ids[1]),),
        )
    pairs = _ranked_pairs(findings)
    if len(pairs) > PAIR_CAP:
        return TransitionDecision(
            action="spawn",
            reason="pair_cap",
            pairs_capped=True,
            pairs_scored=0,
        )
    max_noul, scored, evidence, fail_reason = _score_pairs(
        _job_goal(store, job_id), pairs
    )
    if fail_reason:
        return TransitionDecision(
            action="spawn",
            reason=fail_reason,
            pairs_scored=scored,
            evidence=evidence,
        )
    if max_noul is not None and max_noul >= CONFLICT_THRESHOLD:
        return TransitionDecision(
            action="spawn",
            reason="noul_above_threshold",
            max_noul=max_noul,
            pairs_scored=scored,
            evidence=evidence,
        )
    return TransitionDecision(
        action="skip",
        reason="no_pair_above_threshold",
        max_noul=0.0 if max_noul is None else max_noul,
        pairs_scored=scored,
        evidence=evidence,
    )


def _existing_gate(store: Any, job_id: str, task_id: str) -> Optional[Artifact]:
    try:
        artifacts = list(store.list_artifacts(job_id) or [])
    except Exception:
        return None
    for artifact in artifacts:
        if getattr(artifact, "type", None) != ArtifactType.GATE:
            continue
        if getattr(artifact, "task_id", None) != task_id:
            continue
        payload = getattr(artifact, "payload", None) or {}
        if payload.get("gate") != "jev_transition":
            continue
        if payload.get("edge") != EDGE_CONFLICT_AUDITOR:
            continue
        return artifact
    return None


def _decision_from_gate(artifact: Artifact) -> TransitionDecision:
    payload = artifact.payload or {}
    action = str(payload.get("action") or "spawn")
    if action not in {"skip", "spawn"}:
        action = "spawn"
    would = str(payload.get("would_action") or action)
    if would not in {"skip", "spawn"}:
        would = action
    max_noul = payload.get("max_noul")
    try:
        max_noul_f = float(max_noul) if max_noul is not None else None
    except (TypeError, ValueError):
        max_noul_f = None
    return TransitionDecision(
        action=action,
        reason=str(payload.get("reason") or "fail_open"),
        threshold=float(payload.get("threshold") or CONFLICT_THRESHOLD),
        max_noul=max_noul_f,
        pairs_scored=int(payload.get("pairs_scored") or 0),
        pairs_capped=bool(payload.get("pairs_capped")),
        evidence=tuple(str(item) for item in (artifact.evidence or [])),
        write_gate=False,
        would_action=would,
        acted=bool(payload.get("acted")),
    )


def _persist(store: Any, artifact: Artifact) -> None:
    try:
        store.save_artifact(artifact)
    except Exception:
        return


def apply_conflict_auditor_gate(
    store: Any,
    task: Task,
    *,
    worker_id: Optional[str] = None,
    persist_not_opted_in: bool = False,
) -> TransitionDecision:
    """Decide and apply skip/spawn for one auditor task. Fail-open. Never raises."""
    try:
        if getattr(task, "role", None) != "conflict-auditor":
            return TransitionDecision(
                action="spawn",
                reason="not_opted_in",
                write_gate=False,
            )
        status = getattr(task, "status", None)
        if status not in _READY and status != TaskStatus.SKIPPED:
            return TransitionDecision(
                action="spawn",
                reason="not_opted_in",
                write_gate=False,
            )
        existing = _existing_gate(store, task.job_id, task.id)
        if existing is not None:
            return _decision_from_gate(existing)
        if status == TaskStatus.SKIPPED:
            return TransitionDecision(action="skip", reason="no_pair_above_threshold")
        decision = _persist_bound_gate(
            store,
            job_id=task.job_id,
            task_id=task.id,
            edge=EDGE_CONFLICT_AUDITOR,
            decision=decide_conflict_auditor(
                store, task.job_id, persist_not_opted_in=persist_not_opted_in
            ),
        )
        if decision.acted:
            try:
                store.update_task_status(
                    task, TaskStatus.SKIPPED, worker_id=worker_id
                )
            except Exception:
                return TransitionDecision(
                    action="spawn",
                    reason="fail_open",
                    evidence=("fail-open:SkipStatus",),
                    would_action=_would(decision),
                    acted=False,
                )
            _persist(
                store,
                build_skip_verification(
                    job_id=task.job_id,
                    task_id=task.id,
                    edge=EDGE_CONFLICT_AUDITOR,
                ),
            )
        return decision
    except Exception as exc:
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            evidence=("fail-open:%s" % type(exc).__name__,),
        )


def apply_ready_conflict_auditor_gates(
    store: Any,
    job_id: str,
    tasks: Optional[Iterable[Task]] = None,
) -> List[TransitionDecision]:
    """Apply the gate to each ready conflict-auditor task on the job."""
    decisions: List[TransitionDecision] = []
    try:
        rows = list(tasks) if tasks is not None else list(store.list_tasks(job_id))
    except Exception:
        return decisions
    for task in rows:
        if getattr(task, "role", None) != "conflict-auditor":
            continue
        if getattr(task, "status", None) not in _READY:
            continue
        decisions.append(apply_conflict_auditor_gate(store, task))
    return decisions


@dataclass(frozen=True)
class AlreadyAnsweredReuse:
    prior_job_id: str
    handle_job_id: str
    decision: TransitionDecision


def explicit_analysis_trigger(goal: str) -> bool:
    """True when the goal itself names Puppetmaster / a swarm. Those still launch."""
    try:
        from puppetmaster.invocation_gate import _EXPLICIT_DELEGATE_PATTERNS
    except Exception:
        return False
    text = (goal or "").lower()
    try:
        return any(pattern.search(text) for pattern in _EXPLICIT_DELEGATE_PATTERNS)
    except Exception:
        return False


def _normalize_cwd(cwd: Any) -> str:
    raw = str(cwd or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve())
    except OSError:
        return raw


def _job_matches_cwd(store: Any, job_id: str, cwd: str) -> bool:
    wanted = _normalize_cwd(cwd)
    if not wanted:
        return False
    try:
        tasks = list(store.list_tasks(job_id) or [])
    except Exception:
        return False
    for task in tasks:
        payload = getattr(task, "payload", None) or {}
        candidate = payload.get("cwd") or payload.get("workspace")
        if candidate and _normalize_cwd(candidate) == wanted:
            return True
    return False


def _artifact_claim_text(artifact: Artifact) -> str:
    payload = getattr(artifact, "payload", None) or {}
    for key in ("claim", "decision", "summary"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def collect_prior_claims(
    store: Any,
    cwd: str,
    *,
    exclude_job_ids: Optional[Sequence[str]] = None,
) -> Tuple[str, List[dict]]:
    """Recent FINDING/GIST/DECISION claims for this cwd. Empty is not answered."""
    excluded = set(exclude_job_ids or ())
    try:
        jobs = list(store.list_jobs() or [])
    except Exception:
        return ("", [])
    jobs.sort(key=lambda job: str(getattr(job, "created_at", "") or ""), reverse=True)
    from puppetmaster.metr_seams import is_coordination_protocol_payload

    claims: List[dict] = []
    prior_job_id = ""
    scanned = 0
    for job in jobs:
        if scanned >= _PRIOR_JOB_SCAN:
            break
        if job.id in excluded:
            continue
        if not _job_matches_cwd(store, job.id, cwd):
            continue
        scanned += 1
        try:
            artifacts = list(store.list_artifacts(job.id) or [])
        except Exception:
            continue
        job_claims = []
        for artifact in artifacts:
            if getattr(artifact, "type", None) not in _CLAIM_TYPES:
                continue
            if is_coordination_protocol_payload(artifact):
                continue
            text = _artifact_claim_text(artifact)
            if not text:
                continue
            job_claims.append(
                {
                    "id": artifact.id,
                    "job_id": job.id,
                    "type": str(artifact.type),
                    "claim": _clip(text, CLAIM_CHARS),
                    "confidence": float(getattr(artifact, "confidence", 0.0) or 0.0),
                }
            )
        if not job_claims:
            continue
        if not prior_job_id:
            prior_job_id = job.id
        claims.extend(job_claims)
        if len(claims) >= _PRIOR_CLAIM_CAP:
            break
    return (prior_job_id, claims[:_PRIOR_CLAIM_CAP])


def decide_already_answered(
    store: Any,
    goal: str,
    cwd: str,
    exclude_job_ids: Optional[Sequence[str]] = None,
) -> TransitionDecision:
    """Reuse a prior analysis job or fail-open to today's launch. Never raises."""
    try:
        if not opted_in():
            return TransitionDecision(
                action="spawn",
                reason="not_opted_in",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                write_gate=False,
            )
        if explicit_analysis_trigger(goal):
            return TransitionDecision(
                action="spawn",
                reason="explicit_trigger",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                write_gate=False,
            )
        if not enabled():
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                evidence=("fail-open:MissingKey",),
            )
        prior_job_id, claims = collect_prior_claims(
            store, cwd, exclude_job_ids=exclude_job_ids
        )
        if not prior_job_id or not claims:
            return TransitionDecision(
                action="spawn",
                reason="thin_set",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                write_gate=False,
            )
        state = {
            "goal": _clip(goal, GOAL_CHARS),
            "prior_job_id": prior_job_id,
            "claims": claims,
        }
        questions = {
            "already_answered": {
                "type": "noul",
                "instructions": ALREADY_ANSWERED_INSTRUCTIONS,
            }
        }
        if _estimate_tokens(state, questions) > TOKEN_BUDGET:
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                evidence=("fail-open:token_budget",),
            )
        body = decide(state, questions)
        noul = parse_noul((body or {}).get("answers"), "already_answered")
        if noul is None:
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                evidence=("fail-open:DecideNone",),
            )
        if noul >= ALREADY_ANSWERED_THRESHOLD:
            return TransitionDecision(
                action="skip",
                reason="already_answered",
                threshold=ALREADY_ANSWERED_THRESHOLD,
                max_noul=noul,
                pairs_scored=1,
                evidence=(prior_job_id,),
            )
        return TransitionDecision(
            action="spawn",
            reason="noul_below_threshold",
            threshold=ALREADY_ANSWERED_THRESHOLD,
            max_noul=noul,
            pairs_scored=1,
            evidence=(prior_job_id,),
        )
    except Exception as exc:
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            threshold=ALREADY_ANSWERED_THRESHOLD,
            evidence=("fail-open:%s" % type(exc).__name__,),
        )


def record_already_answered_on_job(
    store: Any,
    job_id: str,
    goal: str,
    cwd: str,
    *,
    task_id: str = "",
) -> Optional[TransitionDecision]:
    """Write the already-answered receipt on the job that actually launched."""
    try:
        if not opted_in():
            return None
        decided = decide_already_answered(
            store, goal, cwd, exclude_job_ids=(job_id,)
        )
        if not decided.write_gate:
            return _bind_applied(decided, edge=EDGE_ALREADY_ANSWERED)
        return _persist_bound_gate(
            store,
            job_id=job_id,
            task_id=task_id or "jev-transition",
            edge=EDGE_ALREADY_ANSWERED,
            decision=decided,
        )
    except Exception:
        return None


def apply_already_answered(
    store: Any,
    goal: str,
    cwd: str,
) -> Optional[AlreadyAnsweredReuse]:
    """Create a handle GATE and return the prior job when reuse wins.

    Observe-only does not persist here. The launched analysis job records
    the score via ``record_already_answered_on_job``.
    """
    try:
        if not acting():
            return None
        decided = decide_already_answered(store, goal, cwd)
        prior_job_id = decided.evidence[0] if decided.evidence else ""
        bound = _bind_applied(decided, edge=EDGE_ALREADY_ANSWERED)
        if not bound.acted:
            return None
        if not prior_job_id:
            return None
        handle = store.create_job(goal, label="already answered")
        task = Task(
            job_id=handle.id,
            role="jev-transition",
            instruction="already-answered reuse",
            status=TaskStatus.SKIPPED,
            payload={"cwd": cwd} if cwd else {},
        )
        store.save_task(task)
        bound = _persist_bound_gate(
            store,
            job_id=handle.id,
            task_id=task.id,
            edge=EDGE_ALREADY_ANSWERED,
            decision=decided,
        )
        try:
            store.update_job_status(handle.id, JobStatus.COMPLETE)
        except Exception:
            pass
        return AlreadyAnsweredReuse(
            prior_job_id=prior_job_id,
            handle_job_id=handle.id,
            decision=bound,
        )
    except Exception:
        return None


def decide_finding_admission(finding: Artifact) -> TransitionDecision:
    """Demote plumbing FINDINGs. Fail-open keeps today's admission. Never raises."""
    try:
        if not opted_in():
            return TransitionDecision(
                action="spawn",
                reason="not_opted_in",
                threshold=ADMISSION_THRESHOLD,
                write_gate=False,
            )
        if not enabled():
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=ADMISSION_THRESHOLD,
                evidence=("fail-open:MissingKey",),
                write_gate=False,
            )
        claim = _claim_text(finding)
        if not claim:
            return TransitionDecision(
                action="spawn",
                reason="thin_set",
                threshold=ADMISSION_THRESHOLD,
                write_gate=False,
            )
        state = {
            "claim": _clip(claim, CLAIM_CHARS),
            "evidence": [
                str(item)
                for item in (finding.evidence or [])
                if str(item).strip()
            ][:EVIDENCE_LOCI],
        }
        questions = {
            "repo_fact": {
                "type": "noul",
                "instructions": ADMISSION_INSTRUCTIONS,
            }
        }
        body = decide(state, questions)
        noul = parse_noul((body or {}).get("answers"), "repo_fact")
        if noul is None:
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=ADMISSION_THRESHOLD,
                evidence=("fail-open:DecideNone",),
                write_gate=False,
            )
        if noul < ADMISSION_THRESHOLD:
            return TransitionDecision(
                action="skip",
                reason="plumbing_demoted",
                threshold=ADMISSION_THRESHOLD,
                max_noul=noul,
                pairs_scored=1,
                evidence=(finding.id,),
            )
        return TransitionDecision(
            action="spawn",
            reason="noul_above_threshold",
            threshold=ADMISSION_THRESHOLD,
            max_noul=noul,
            pairs_scored=1,
            evidence=(finding.id,),
        )
    except Exception as exc:
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            threshold=ADMISSION_THRESHOLD,
            evidence=("fail-open:%s" % type(exc).__name__,),
            write_gate=False,
        )


def apply_finding_admission(store: Any, finding: Artifact) -> Optional[bool]:
    """Return False to demote (do not admit). None/True keeps today's admission."""
    try:
        decided = decide_finding_admission(finding)
        bound = _persist_bound_gate(
            store,
            job_id=finding.job_id,
            task_id=finding.task_id,
            edge=EDGE_FINDING_ADMISSION,
            decision=decided,
        )
        if not bound.acted:
            return None
        payload = dict(getattr(finding, "payload", None) or {})
        payload["jev_injectable"] = False
        updated = replace(finding, payload=payload)
        try:
            store.save_artifact(updated)
        except Exception:
            pass
        return False
    except Exception:
        return None


def _existing_edge_gate(store: Any, job_id: str, edge: str) -> Optional[Artifact]:
    try:
        artifacts = list(store.list_artifacts(job_id) or [])
    except Exception:
        return None
    for artifact in artifacts:
        if getattr(artifact, "type", None) != ArtifactType.GATE:
            continue
        payload = getattr(artifact, "payload", None) or {}
        if payload.get("gate") != "jev_transition":
            continue
        if payload.get("edge") != edge:
            continue
        return artifact
    return None


def collect_job_claims(store: Any, job_id: str) -> List[dict]:
    try:
        artifacts = list(store.list_artifacts(job_id) or [])
    except Exception:
        return []
    from puppetmaster.metr_seams import is_coordination_protocol_payload

    claims: List[dict] = []
    for artifact in artifacts:
        if getattr(artifact, "type", None) not in _CLAIM_TYPES:
            continue
        if is_coordination_protocol_payload(artifact):
            continue
        text = _artifact_claim_text(artifact)
        if not text:
            continue
        claims.append(
            {
                "id": artifact.id,
                "type": str(artifact.type),
                "claim": _clip(text, CLAIM_CHARS),
            }
        )
        if len(claims) >= _PRIOR_CLAIM_CAP:
            break
    return claims


def decide_stop_spawn(store: Any, job_id: str) -> TransitionDecision:
    """Stop extra follow-up enqueue when the goal looks answered. Never raises."""
    try:
        if not opted_in():
            return TransitionDecision(
                action="spawn",
                reason="not_opted_in",
                threshold=STOP_THRESHOLD,
                write_gate=False,
            )
        if not enabled():
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=STOP_THRESHOLD,
                evidence=("fail-open:MissingKey",),
                write_gate=False,
            )
        claims = collect_job_claims(store, job_id)
        if not claims:
            return TransitionDecision(
                action="spawn",
                reason="thin_set",
                threshold=STOP_THRESHOLD,
                write_gate=False,
            )
        state = {
            "goal": _job_goal(store, job_id),
            "claims": claims,
        }
        questions = {
            "still_unanswered": {
                "type": "noul",
                "instructions": STOP_INSTRUCTIONS,
            }
        }
        body = decide(state, questions)
        noul = parse_noul((body or {}).get("answers"), "still_unanswered")
        if noul is None:
            return TransitionDecision(
                action="spawn",
                reason="fail_open",
                threshold=STOP_THRESHOLD,
                evidence=("fail-open:DecideNone",),
                write_gate=False,
            )
        if noul < STOP_THRESHOLD:
            return TransitionDecision(
                action="skip",
                reason="goal_answered",
                threshold=STOP_THRESHOLD,
                max_noul=noul,
                pairs_scored=1,
                evidence=(job_id,),
            )
        return TransitionDecision(
            action="spawn",
            reason="noul_above_threshold",
            threshold=STOP_THRESHOLD,
            max_noul=noul,
            pairs_scored=1,
        )
    except Exception as exc:
        return TransitionDecision(
            action="spawn",
            reason="fail_open",
            threshold=STOP_THRESHOLD,
            evidence=("fail-open:%s" % type(exc).__name__,),
            write_gate=False,
        )


def apply_stop_spawn(
    store: Any,
    job_id: str,
    *,
    task_id: str = "",
) -> bool:
    """True when extra follow-ups should not be enqueued."""
    try:
        existing = _existing_edge_gate(store, job_id, EDGE_STOP_SPAWN)
        if existing is not None:
            return bool((existing.payload or {}).get("acted"))
        bound = _persist_bound_gate(
            store,
            job_id=job_id,
            task_id=task_id or "jev-transition",
            edge=EDGE_STOP_SPAWN,
            decision=decide_stop_spawn(store, job_id),
        )
        return bound.acted
    except Exception:
        return False
