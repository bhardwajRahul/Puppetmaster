"""WorkspaceScope freeze — no silent mid-process store-root swap."""
from __future__ import annotations

import os
import sys

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from puppetmaster.store_factory import create_store
from puppetmaster.workspace_scope import (
    WorkspaceScopeFrozenError,
    WorkspaceScopeKind,
    assert_same_scope,
    bind_workspace_scope,
    classify_scope_kind,
    current_workspace_scope,
    reset_workspace_scope,
)


class WorkspaceScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_workspace_scope()

    def tearDown(self) -> None:
        reset_workspace_scope()

    def test_bind_idempotent_same_root(self) -> None:
        with TemporaryDirectory() as tmp:
            a = bind_workspace_scope(tmp, kind=WorkspaceScopeKind.LOCAL)
            b = bind_workspace_scope(tmp, kind=WorkspaceScopeKind.EXPLICIT)
            self.assertEqual(a.resolved_root(), b.resolved_root())
            self.assertEqual(current_workspace_scope().kind, WorkspaceScopeKind.LOCAL)

    def test_bind_refuses_silent_swap(self) -> None:
        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "a"
            second = Path(tmp) / "b"
            first.mkdir()
            second.mkdir()
            bind_workspace_scope(first)
            with self.assertRaises(WorkspaceScopeFrozenError):
                bind_workspace_scope(second)

    def test_rebind_escape_hatch(self) -> None:
        with TemporaryDirectory() as tmp:
            first = Path(tmp) / "a"
            second = Path(tmp) / "b"
            first.mkdir()
            second.mkdir()
            bind_workspace_scope(first)
            scope = bind_workspace_scope(second, rebind=True)
            self.assertEqual(scope.resolved_root(), second.resolve())

    def test_create_store_ensure_refuses_foreign_root(self) -> None:
        with TemporaryDirectory() as tmp:
            bound = Path(tmp) / "bound"
            other = Path(tmp) / "other"
            bound.mkdir()
            other.mkdir()
            bind_workspace_scope(bound)
            create_store("file", bound, mode="ensure")  # same root ok
            with self.assertRaises(WorkspaceScopeFrozenError):
                create_store("file", other, mode="ensure")
            # deferred attach of another project remains allowed
            create_store("file", other, mode="deferred")

    def test_assert_same_scope_noop_when_unbound(self) -> None:
        assert_same_scope("/tmp/whatever-unbound-check")

    def test_classify_scope_kind(self) -> None:
        self.assertEqual(classify_scope_kind(explicit_state_dir=False), WorkspaceScopeKind.LOCAL)
        self.assertEqual(classify_scope_kind(explicit_state_dir=True), WorkspaceScopeKind.EXPLICIT)
        self.assertEqual(
            classify_scope_kind(explicit_state_dir=False, development=True),
            WorkspaceScopeKind.DEVELOPMENT,
        )


if __name__ == "__main__":
    unittest.main()
