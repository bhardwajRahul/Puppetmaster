from __future__ import annotations

"""Human-readable Jev receipts for stitch / show."""

from typing import Any, Iterable, List

from puppetmaster.models import Artifact, ArtifactType

_EDGE_LABEL = {
    "conflict_auditor": "conflict-auditor",
    "already_answered": "already-answered",
    "finding_admission": "finding-admission",
    "stop_spawn": "stop-spawn",
}

_APPLIED_LABEL = {
    "conflict_auditor": {"spawn": "ran", "skip": "skipped"},
    "already_answered": {"spawn": "launched", "skip": "reused"},
    "finding_admission": {"spawn": "admitted", "skip": "demoted"},
    "stop_spawn": {"spawn": "enqueued", "skip": "stopped"},
}


def transition_gates(artifacts: Iterable[Artifact]) -> List[Artifact]:
    rows = []
    for artifact in artifacts:
        if getattr(artifact, "type", None) != ArtifactType.GATE:
            continue
        payload = getattr(artifact, "payload", None) or {}
        if payload.get("gate") != "jev_transition":
            continue
        rows.append(artifact)
    rows.sort(key=lambda item: str((item.payload or {}).get("edge") or ""))
    return rows


def _noul_text(payload: dict[str, Any]) -> str:
    raw = payload.get("max_noul")
    if raw is None:
        return ""
    try:
        return " noul=%.2f" % float(raw)
    except (TypeError, ValueError):
        return ""


def format_transition_line(artifact: Artifact) -> str:
    payload = getattr(artifact, "payload", None) or {}
    edge = str(payload.get("edge") or "jev")
    label = _EDGE_LABEL.get(edge, edge.replace("_", "-"))
    applied = str(payload.get("action") or "spawn")
    would = str(payload.get("would_action") or applied)
    applied_word = _APPLIED_LABEL.get(edge, {}).get(applied, applied)
    noul = _noul_text(payload)
    reason = str(payload.get("reason") or "")
    if reason == "fail_open":
        return "- %s: fail-open%s — %s" % (label, noul, applied_word)
    if payload.get("acted"):
        return "- %s: %s%s (%s)" % (label, applied_word, noul, reason or "acted")
    return "- %s: would %s%s — %s" % (label, would, noul, applied_word)


def render_transition_section(artifacts: Iterable[Artifact]) -> List[str]:
    """Empty when the job has no Jev GATE. Unset summaries stay unchanged."""
    rows = transition_gates(artifacts)
    if not rows:
        return []
    lines = ["", "## Jev"]
    seen = set()
    for artifact in rows:
        line = format_transition_line(artifact)
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines
