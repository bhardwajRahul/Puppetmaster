from __future__ import annotations

"""Build GATE / skip VERIFICATION rows. Callers own store writes."""

from typing import Any, Optional, Sequence

from puppetmaster.models import Artifact, ArtifactType

from .questions import CONFLICT_THRESHOLD


def build_transition_gate(
    *,
    job_id: str,
    task_id: str,
    edge: str,
    action: str,
    reason: str,
    threshold: float = CONFLICT_THRESHOLD,
    max_noul: Optional[float] = None,
    pairs_scored: int = 0,
    pairs_capped: bool = False,
    evidence: Optional[Sequence[str]] = None,
    would_action: Optional[str] = None,
    acted: bool = False,
) -> Artifact:
    """Receipt for a Jev graph-edge decision. ``passed`` is always True.

    ``passed`` means the gate chose a legal action. It is not a quality
    verdict. ``quality.py`` treats ``passed is False`` as fail-closed.

    ``action`` is what the graph did. ``would_action`` is Jev's
    recommendation. Observe-only writes ``acted=false`` and keeps
    ``action`` on today's spawn/admit/enqueue path.
    """
    recommended = would_action if would_action else action
    payload: dict[str, Any] = {
        "gate": "jev_transition",
        "edge": edge,
        "passed": True,
        "action": action,
        "would_action": recommended,
        "acted": bool(acted),
        "reason": reason,
        "threshold": threshold,
        "max_noul": max_noul,
        "pairs_scored": pairs_scored,
        "pairs_capped": pairs_capped,
    }
    rows = [str(item) for item in (evidence or ()) if str(item).strip()]
    if not rows:
        rows = ["jev_transition:%s" % edge]
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.GATE,
        created_by="jev-transition",
        confidence=1.0,
        evidence=rows,
        payload=payload,
    )


def build_skip_verification(
    *,
    job_id: str,
    task_id: str,
    edge: str,
) -> Artifact:
    """Satisfy skip receipts without emitting FINDINGs."""
    check = "jev_transition:%s" % edge
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.VERIFICATION,
        created_by="jev-transition",
        confidence=1.0,
        evidence=["result:skipped", "check:%s" % check],
        payload={"check": check, "result": "skipped"},
    )
