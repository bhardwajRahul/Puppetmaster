"""Normalized task failure-edge policy shared by every execution adapter."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


_RETRY = re.compile(r"^retry\(([0-9]+)\)$")
_ACTIONS = frozenset({"abort", "continue", "retry"})


@dataclass(frozen=True)
class FailurePolicy:
    action: str
    retries: int = 0
    allow_routing_change: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "retries": self.retries,
            "allow_routing_change": self.allow_routing_change,
        }


def normalize_failure_policy(value: Any) -> Optional[dict[str, Any]]:
    """Validate and canonicalize ``on_fail`` / ``payload.failure_policy``.

    Accepted spellings are ``abort``, ``continue``, ``retry(n)``, and a
    structured object with ``action`` plus ``retries`` for retry.  Integers are
    deliberately type-strict: JSON booleans must never become retry counts.
    """
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in {"abort", "continue"}:
            return FailurePolicy(raw).as_dict()
        match = _RETRY.fullmatch(raw)
        if match:
            count = int(match.group(1))
            if count > 10:
                raise ValueError("failure policy retries must be between 0 and 10")
            return FailurePolicy("retry", count).as_dict()
        raise ValueError("on_fail must be abort, continue, or retry(n)")
    if not isinstance(value, Mapping):
        raise ValueError("on_fail must be a string or object")
    unknown = set(value) - {"action", "retries", "allow_routing_change"}
    if unknown:
        raise ValueError("unknown on_fail field(s): " + ", ".join(sorted(unknown)))
    action = value.get("action")
    if not isinstance(action, str) or action.strip().lower() not in _ACTIONS:
        raise ValueError("on_fail.action must be abort, continue, or retry")
    action = action.strip().lower()
    retries = value.get("retries", 0)
    if type(retries) is not int or not 0 <= retries <= 10:
        raise ValueError("failure policy retries must be an integer between 0 and 10")
    if action != "retry" and "retries" in value and retries != 0:
        raise ValueError("on_fail.retries is only valid for action=retry")
    allow = value.get("allow_routing_change", False)
    if type(allow) is not bool:
        raise ValueError("on_fail.allow_routing_change must be boolean")
    return FailurePolicy(action, retries, allow).as_dict()


def task_failure_policy(payload: Any) -> Optional[dict[str, Any]]:
    if not isinstance(payload, dict) or "failure_policy" not in payload:
        return None
    return normalize_failure_policy(payload.get("failure_policy"))


def clear_failure_edge_state(payload: dict[str, Any]) -> dict[str, Any]:
    cleared = dict(payload)
    for key in tuple(cleared):
        if key.startswith("failure_policy_") or key == "failure_cut":
            cleared.pop(key, None)
    return cleared
