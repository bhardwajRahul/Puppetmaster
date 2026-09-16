"""Strict, optional terminal verdicts emitted by individual workers.

Worker verdicts are advisory attribution, not a replacement for runtime gates.
Older adapters/jobs may omit them, so absence remains observable but is never
interpreted as a pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Optional

from puppetmaster.models import Artifact, ArtifactType, Task

_LINE = re.compile(r"^VERDICT:\s*(PASS|FAIL|PARTIAL)\s+[-\u2013\u2014]\s+(.+?)\s*$")
_STRUCTURED = frozenset(("PASS", "FAIL", "PARTIAL"))
# Artifact status is a closed vocabulary. Keep the worker-facing label
# ``PARTIAL`` but map it to the existing degraded outcome.
_RESULTS = {"PASS": "passed", "FAIL": "failed", "PARTIAL": "degraded"}


@dataclass(frozen=True)
class WorkerVerdict:
    verdict: str
    reason: str


@dataclass(frozen=True)
class WorkerOutput:
    """An adapter-native final message split from its optional verdict."""

    body: str
    verdict: Optional[WorkerVerdict]


def parse_terminal_verdict(text: object) -> Optional[WorkerVerdict]:
    """Parse exactly one terminal ``VERDICT`` line; malformed input is absent.

    A verdict must be the final non-blank line. This deliberately avoids a
    casual progress note, prompt injection in tool output, or an earlier
    contradictory verdict becoming terminal.
    """
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = [line for line in lines if line.startswith("VERDICT:")]
    if len(matches) != 1 or not lines or matches[0] != lines[-1]:
        return None
    match = _LINE.fullmatch(matches[0])
    if match is None:
        return None
    return WorkerVerdict(match.group(1), match.group(2))


def parse_structured_verdict(value: object) -> Optional[WorkerVerdict]:
    """Validate a native-tool/JSON ``worker_verdict`` object."""
    if not isinstance(value, dict):
        return None
    verdict = value.get("verdict")
    reason = value.get("reason")
    if not isinstance(verdict, str) or verdict not in _STRUCTURED:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return WorkerVerdict(verdict, reason.strip())


def extract_worker_output(text: object) -> WorkerOutput:
    """Split one valid terminal verdict from an adapter's final message.

    Callers must pass the adapter-native final response (Cursor/Claude result,
    Codex last_message, OpenAI content, etc.), never a raw command/tool log.
    Invalid or duplicate verdict-looking lines remain in ``body`` so consumers
    retain the ordinary report while declining to mint a verdict.
    """
    body = text if isinstance(text, str) else "" if text is None else str(text)
    verdict = parse_terminal_verdict(body)
    if verdict is None:
        return WorkerOutput(body.strip(), None)
    lines = body.splitlines()
    last_nonblank = max(index for index, line in enumerate(lines) if line.strip())
    del lines[last_nonblank]
    return WorkerOutput("\n".join(lines).strip(), verdict)


def worker_verdict_artifact(
    task: Task, worker_id: str, verdict: WorkerVerdict, *, source: str
) -> Artifact:
    return Artifact(
        job_id=task.job_id,
        task_id=task.id,
        type=ArtifactType.VERIFICATION,
        created_by=worker_id,
        confidence=1.0,
        evidence=["worker_verdict", "source:" + source],
        payload={
            "check": task.instruction,
            "adapter": task.adapter,
            "kind": "worker_verdict",
            "source": "worker",
            "advisory": True,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "result": _RESULTS[verdict.verdict],
        },
    )


def verdict_artifacts(task: Task, worker_id: str, artifacts: list[Artifact]) -> list[Artifact]:
    """Validate adapter-normalized verdicts without scanning raw payload text.

    Ordinary artifacts are preserved. Any malformed, duplicate, or
    contradictory verdict candidates are dropped rather than treated as PASS.
    """
    ordinary: list[Artifact] = []
    found: list[tuple[WorkerVerdict, str, Artifact]] = []
    invalid = False
    for artifact in artifacts:
        payload = artifact.payload or {}
        if payload.get("kind") != "worker_verdict":
            ordinary.append(artifact)
            continue
        parsed = parse_structured_verdict(payload)
        if (
            parsed is None
            or payload.get("source") != "worker"
            or payload.get("advisory") is not True
        ):
            invalid = True
            continue
        source = next(
            (
                item.split(":", 1)[1]
                for item in artifact.evidence
                if item.startswith("source:")
            ),
            "structured",
        )
        found.append((parsed, source, artifact))
    if invalid or len(found) != 1:
        return ordinary
    verdict, source, candidate = found[0]
    canonical = worker_verdict_artifact(task, worker_id, verdict, source=source)
    return ordinary + [replace(canonical, id=candidate.id)]
