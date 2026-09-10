"""WorkspaceScope freeze — store roots must not silently swap mid-process.

Comet/Zeron lesson: ``AuthState`` (live credentials) and ``WorkspaceScope``
(storage/transport boundary) are separate state machines. The engine captures
scope once at startup. Sign-in, token refresh, or profile change must not
re-resolve an open store root underneath a running supervisor / MCP server.

Puppetmaster today is local-first (no synced cloud profile), but the same
invariant applies to ``PUPPETMASTER_STATE_DIR`` / ``--state-dir``:

- Bind the *primary* engine scope once (CLI dispatch / MCP server boot).
- Cross-project job lookup may *attach* another state dir read-only for that
  call; it must not rebind the frozen primary scope.
- Multi-profile / auth work that ever selects store roots must refuse silent
  mid-process swaps (:exc:`WorkspaceScopeFrozenError`).

Marionette / Automaton / Discord OS remain viewports; they do not own the
frozen engine scope.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union


class WorkspaceScopeKind(str, Enum):
    """Storage boundary kinds. Synced is reserved for a future profile."""

    LOCAL = "local"
    EXPLICIT = "explicit"  # --state-dir / PUPPETMASTER_STATE_DIR
    DEVELOPMENT = "development"


class WorkspaceScopeFrozenError(RuntimeError):
    """Raised when code attempts to swap the frozen primary store root."""


@dataclass(frozen=True)
class WorkspaceScope:
    kind: WorkspaceScopeKind
    root: Path
    # Optional human label (workspace path, profile id, …). Not a second root.
    label: str = ""

    def resolved_root(self) -> Path:
        return self.root.expanduser().resolve()


_lock = threading.Lock()
_frozen: Optional[WorkspaceScope] = None


def reset_workspace_scope() -> None:
    """Tests only: clear the process freeze."""
    global _frozen
    with _lock:
        _frozen = None


def current_workspace_scope() -> Optional[WorkspaceScope]:
    with _lock:
        return _frozen


def bind_workspace_scope(
    root: Union[Path, str],
    *,
    kind: Union[WorkspaceScopeKind, str] = WorkspaceScopeKind.LOCAL,
    label: str = "",
    rebind: bool = False,
) -> WorkspaceScope:
    """Freeze the primary store root for this process.

    First call wins. Later calls with the *same* resolved root are idempotent.
    A different root raises :exc:`WorkspaceScopeFrozenError` unless
    ``rebind=True`` (explicit operator escape hatch — never for auth churn).
    """
    global _frozen
    kind_enum = (
        kind if isinstance(kind, WorkspaceScopeKind) else WorkspaceScopeKind(str(kind))
    )
    scope = WorkspaceScope(
        kind=kind_enum,
        root=Path(root),
        label=str(label or ""),
    )
    resolved = scope.resolved_root()
    with _lock:
        if _frozen is None or rebind:
            _frozen = WorkspaceScope(kind=kind_enum, root=resolved, label=scope.label)
            return _frozen
        if _frozen.resolved_root() == resolved:
            return _frozen
        raise WorkspaceScopeFrozenError(
            "workspace scope is frozen for this process: "
            f"bound={_frozen.resolved_root()} attempted={resolved}. "
            "Stop the engine before changing --state-dir / profile / auth "
            "that would swap store roots. Cross-project attach is fine; "
            "silent rebind is not."
        )


def assert_same_scope(root: Union[Path, str]) -> None:
    """No-op when unbound; otherwise require ``root`` matches the freeze."""
    scope = current_workspace_scope()
    if scope is None:
        return
    resolved = Path(root).expanduser().resolve()
    if scope.resolved_root() != resolved:
        raise WorkspaceScopeFrozenError(
            "store root does not match frozen workspace scope: "
            f"bound={scope.resolved_root()} attempted={resolved}"
        )


def classify_scope_kind(
    *,
    explicit_state_dir: bool,
    development: bool = False,
) -> WorkspaceScopeKind:
    if development:
        return WorkspaceScopeKind.DEVELOPMENT
    if explicit_state_dir:
        return WorkspaceScopeKind.EXPLICIT
    return WorkspaceScopeKind.LOCAL
