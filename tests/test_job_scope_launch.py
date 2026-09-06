"""Scope transport through public launch boundaries and actual local jobs."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

from puppetmaster.cli import build_parser
from puppetmaster.mcp_server import (
    browser_swarm_schema, goal_schema, launcher_environment, prewalk_schema,
)
from puppetmaster.store import SwarmStore
from puppetmaster.swarm_launch import detach_analysis_swarm


class JobScopeLaunchTests(unittest.TestCase):
    def test_local_cli_and_mcp_environment_create_scoped_jobs(self):
        fields = dict(origin="test-host", project_id="project", session_id="session")
        for transport in ("cli", "mcp"):
            with self.subTest(transport=transport), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "workers.json"
                config.write_text(json.dumps({"workers": [{"role": "explore",
                    "instruction": "Inspect the local demo", "adapter": "local",
                    "payload": {"disable_memory": True}}]}))
                command = [sys.executable, "-m", "puppetmaster", "--state-dir", str(root / "state"),
                           "--backend", "file"]
                env = os.environ.copy()
                for name in fields:
                    env.pop("PUPPETMASTER_JOB_" + name.upper(), None)
                env.pop("PUPPETMASTER_LAUNCH_KEY", None)
                if transport == "cli":
                    for name, value in fields.items():
                        command.extend(["--" + name.replace("_", "-"), value])
                else:
                    with patch.dict(os.environ, env, clear=True):
                        env = launcher_environment(fields)
                command.extend(["run", "Scope launch test", "--config", str(config),
                                "--worker-mode", "inline", "--disable-memory"])
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                store = SwarmStore(root / "state")
                jobs = store.list_jobs()
                self.assertEqual(len(jobs), 1)
                for name, value in fields.items():
                    self.assertEqual(getattr(jobs[0], name), value)
                self.assertEqual(store.list_job_summaries(**fields).items[0].id, jobs[0].id)

    def test_detached_cli_transports_scope_in_parseable_command(self):
        fields = dict(origin="host", project_id="p", session_id="s")
        with TemporaryDirectory() as tmp:
            process = MagicMock(pid=1234)
            process.poll.return_value = 0
            with patch("puppetmaster.swarm_launch.subprocess.Popen", return_value=process) as popen, \
                 patch("puppetmaster.swarm_launch.wait_for_job_id", return_value="job_test"):
                detach_analysis_swarm(goal="audit", roles=["explore"], adapter="codex",
                    state_dir=Path(tmp), cwd=tmp, **fields)
            command = popen.call_args.args[0]
            parsed = build_parser().parse_args(command[command.index("puppetmaster") + 1:])
            for name, value in fields.items():
                self.assertEqual(getattr(parsed, name), value)

    def test_launch_schemas_and_invalid_stamps(self):
        for schema in (goal_schema("test"), browser_swarm_schema(), prewalk_schema()):
            for name in ("origin", "project_id", "session_id"):
                self.assertEqual(schema["properties"][name]["type"], "string")
                for value in ("", 7, "x" * 257):
                    with self.assertRaises(ValueError):
                        launcher_environment({name: value})

    def test_crash_demo_cli_forwards_scope(self):
        with TemporaryDirectory() as tmp:
            command = [sys.executable, "-m", "puppetmaster", "--state-dir", tmp,
                       "--backend", "file", "--origin", "crash-host", "--project-id", "p",
                       "--session-id", "s", "crash-demo", "--goal", "Scope crash demo"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            job = SwarmStore(Path(tmp)).list_jobs()[0]
            self.assertEqual((job.origin, job.project_id, job.session_id), ("crash-host", "p", "s"))
