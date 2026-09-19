"""Doctor must probe owned hook interpreters, not just path existence."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.diagnostics import _hooks_check, run_doctor
from puppetmaster.hook_installers import (
    executable_from_hook_command,
    owned_hook_interpreters,
    render_claude_hooks,
    render_cursor_hooks,
)


def _write_hooks(path: Path, rendered: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")


class OwnedHookInterpreterTests(unittest.TestCase):
    def test_extracts_quoted_windows_python(self):
        command = (
            '"C:/Program Files/Python312/python.exe" -m puppetmaster '
            "invocation-gate --host cursor --event user-prompt"
        )
        self.assertEqual(
            executable_from_hook_command(command),
            "C:/Program Files/Python312/python.exe",
        )

    def test_ignores_foreign_hooks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            _write_hooks(
                root / ".cursor" / "hooks.json",
                {
                    "version": 1,
                    "hooks": {
                        "beforeSubmitPrompt": [
                            {"command": "echo not-ours"},
                        ]
                    },
                },
            )
            self.assertEqual(owned_hook_interpreters(root, home=home), [])

    def test_reads_project_and_home_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            stale = "/opt/homebrew/opt/python@3.12/bin/python3.12"
            _write_hooks(
                root / ".cursor" / "hooks.json",
                {"version": 1, "hooks": render_cursor_hooks(stale)},
            )
            _write_hooks(
                home / ".claude" / "settings.json",
                {"hooks": render_claude_hooks(stale)},
            )
            rows = owned_hook_interpreters(root, home=home)
            labels = {label for label, _exe in rows}
            self.assertIn(".cursor/hooks.json", labels)
            self.assertIn("~/.claude/settings.json", labels)
            self.assertTrue(all(exe == stale for _label, exe in rows))


class HookInterpreterDoctorTests(unittest.TestCase):
    def test_quiet_when_no_owned_hooks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            check = _hooks_check(root, home=home)
            self.assertEqual(check.name, "hooks")
            self.assertEqual(check.status, "optional")

    def test_warns_when_interpreter_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            missing = str(root / "no-such-python")
            _write_hooks(
                root / ".cursor" / "hooks.json",
                {"version": 1, "hooks": render_cursor_hooks(missing)},
            )
            check = _hooks_check(root, home=home)
            self.assertEqual(check.status, "warn")
            self.assertIn(missing, check.detail)
            self.assertIn("missing", check.detail)
            self.assertIn("install-hooks --force", check.detail)

    def test_warns_when_interpreter_exists_but_cannot_import(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            stale = root / "stale-python"
            stale.write_text("not a python that can import puppetmaster\n", encoding="utf-8")
            _write_hooks(
                root / ".cursor" / "hooks.json",
                {"version": 1, "hooks": render_cursor_hooks(str(stale))},
            )
            check = _hooks_check(root, home=home)
            self.assertEqual(check.status, "warn")
            self.assertIn("cannot import puppetmaster", check.detail)
            self.assertIn("puppetmaster setup", check.detail)

    def test_ok_when_interpreter_imports_puppetmaster(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            home = root / "home"
            exe = str(root / "good-python")
            Path(exe).write_text("", encoding="utf-8")
            _write_hooks(
                root / ".cursor" / "hooks.json",
                {"version": 1, "hooks": render_cursor_hooks(exe)},
            )
            with patch(
                "puppetmaster.diagnostics._probe_hook_interpreter",
                return_value=(True, "ok"),
            ):
                check = _hooks_check(root, home=home)
            self.assertEqual(check.status, "ok")
            self.assertIn("import puppetmaster", check.detail)

    def test_run_doctor_includes_hooks_and_stays_quiet_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            home = root / "isolated-home"
            home.mkdir()
            env = {"HOME": str(home), "PATH": str(root / "empty")}
            with patch.dict(os.environ, env, clear=False), patch(
                "puppetmaster.hook_installers.Path.home",
                return_value=home,
            ):
                checks = {item.name: item for item in run_doctor(root, state_dir=root / "state")}
            self.assertIn("hooks", checks)
            self.assertEqual(checks["hooks"].status, "optional")


class ProbeHookInterpreterTests(unittest.TestCase):
    def test_probe_treats_nonzero_import_as_failure(self):
        from puppetmaster.diagnostics import _probe_hook_interpreter

        with tempfile.TemporaryDirectory() as raw:
            exe = Path(raw) / "python"
            exe.write_text("", encoding="utf-8")
            with patch(
                "puppetmaster.diagnostics.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
            ):
                ok, reason = _probe_hook_interpreter(str(exe))
            self.assertFalse(ok)
            self.assertEqual(reason, "cannot-import")
