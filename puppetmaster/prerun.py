"""Cheap prerun skip: refuse adapter spawn without a script runtime."""
from __future__ import annotations

from typing import Any, Optional


def prerun_skip_reason(task: Any) -> Optional[str]:
    """Return a skip reason when payload.prerun asks to skip the LLM.

    Harbour's runner treats exit 77 as skip. We take the same meaning from
    an explicit payload flag instead of executing a prerun script.
    """
    payload = getattr(task, "payload", None) or {}
    if not isinstance(payload, dict):
        return None
    prerun = payload.get("prerun")
    if not isinstance(prerun, dict):
        return None
    skip = prerun.get("skip")
    if skip is not True and skip != "true" and skip != 1:
        return None
    reason = prerun.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return "prerun.skip"
