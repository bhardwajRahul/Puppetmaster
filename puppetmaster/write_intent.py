"""Shared write-intent matrix for every registered adapter.

``spec_edits_files`` (dirty-tree / swarm mode) and ``edit_admission`` (file
claims) must agree on whether a task may mutate the working tree. The
classifier lives here so a new adapter cannot fall out of the admission
layer just because a local frozenset was stale.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def canonicalize_adapter_name(adapter: str) -> str:
    try:
        from puppetmaster.platform_lock import canonicalize_adapter

        return canonicalize_adapter(adapter)
    except Exception:
        return str(adapter or "").strip().lower()


def adapter_may_write(
    adapter: str,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    swarm_mode: Optional[str] = None,
) -> bool:
    """Return True when this adapter/payload pair may mutate the workspace.

    Conservative for unknown adapters: treat them as writers so claims and
    dirty-tree guards still fire.
    """
    del swarm_mode  # reserved; payload/permission fields are authoritative
    name = canonicalize_adapter_name(adapter)
    payload = dict(payload or {})
    implement = (
        str(payload.get("mode") or "").strip().lower() == "implement"
        or bool(payload.get("implement"))
    )
    if name in {"local", "openai"}:
        return implement
    if name == "shell":
        return True
    if name in {"agentic", "cursor", "hermes"}:
        return True
    if name == "claude-code":
        default = (
            "plan"
            if (payload.get("read_only") or payload.get("sandbox") == "read-only")
            else "acceptEdits"
        )
        return str(payload.get("permission_mode", default)) != "plan"
    if name == "codex":
        sandbox = str(payload.get("sandbox", "workspace-write"))
        bypass = bool(payload.get("dangerously_bypass_approvals_and_sandbox"))
        return sandbox != "read-only" or bypass
    if name in {"antigravity", "agy"}:
        default = (
            "plan"
            if (payload.get("read_only") or payload.get("sandbox") == "read-only")
            else "accept-edits"
        )
        return str(payload.get("mode", default)) != "plan"
    return True
