"""Tree-green CI skip: same bytes are enough; SHA identity is not required."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ci_skip_if_green as gate  # noqa: E402


def _tree_map(mapping):
    def tree_for(sha):
        return mapping.get(sha)

    return tree_for


class SkipIfGreenTests(unittest.TestCase):
    def test_matching_green_run_accepts_same_tree_from_pr_head(self):
        match = gate.matching_green_run(
            "tree-dest",
            [
                {"headSha": "dest-tip", "url": "https://example/pr", "conclusion": "success"},
                {"headSha": "other", "url": "https://example/other", "conclusion": "success"},
            ],
            _tree_map({"dest-tip": "tree-dest", "other": "tree-other"}),
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["headSha"], "dest-tip")

    def test_matching_green_run_rejects_different_tree(self):
        match = gate.matching_green_run(
            "tree-main-conflict",
            [{"headSha": "dest-tip", "conclusion": "success"}],
            _tree_map({"dest-tip": "tree-dest"}),
        )
        self.assertIsNone(match)

    def test_matching_green_run_skips_non_success_and_empty_tree(self):
        self.assertIsNone(
            gate.matching_green_run(
                "tree-a",
                [{"headSha": "sha-a", "conclusion": "failure"}],
                _tree_map({"sha-a": "tree-a"}),
            )
        )
        self.assertIsNone(
            gate.matching_green_run(
                "",
                [{"headSha": "sha-a", "conclusion": "success"}],
                _tree_map({"sha-a": ""}),
            )
        )

    def test_should_skip_push_suite_only_on_push_with_other_green_tree(self):
        runs = [
            {"headSha": "pr-head", "databaseId": 11, "conclusion": "success"},
            {"headSha": "this-push", "databaseId": 22, "conclusion": "success"},
        ]
        trees = {"pr-head": "tree-dest", "this-push": "tree-dest"}
        resolver = _tree_map(trees)
        self.assertFalse(
            gate.should_skip_push_suite(
                "pull_request", "tree-dest", runs, resolver, current_run_id=22
            )
        )
        self.assertTrue(
            gate.should_skip_push_suite(
                "push", "tree-dest", runs, resolver, current_run_id=22
            )
        )
        self.assertFalse(
            gate.should_skip_push_suite(
                "push", "tree-conflict", runs, resolver, current_run_id=22
            )
        )
        self.assertFalse(
            gate.should_skip_push_suite(
                "push",
                "tree-dest",
                [{"headSha": "this-push", "databaseId": 22, "conclusion": "success"}],
                resolver,
                current_run_id=22,
            )
        )
        self.assertFalse(
            gate.should_skip_push_suite(
                "push",
                "tree-dest",
                [{"headSha": "pr-head", "databaseId": 11, "conclusion": "failure"}],
                resolver,
                current_run_id=22,
            )
        )

    def test_cmd_skip_if_green_lookup_failure_runs_suite(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        dest = Path(handle.name)
        self.addCleanup(dest.unlink)
        prior = os.environ.get("GITHUB_OUTPUT")
        os.environ["GITHUB_OUTPUT"] = str(dest)
        if prior is None:
            self.addCleanup(lambda: os.environ.pop("GITHUB_OUTPUT", None))
        else:
            self.addCleanup(lambda: os.environ.__setitem__("GITHUB_OUTPUT", prior))
        original_tree = gate.git_tree_sha
        original_list = gate.list_workflow_runs
        gate.git_tree_sha = lambda rev="HEAD": "tree-dest"

        def boom(*_args, **_kwargs):
            raise subprocess.CalledProcessError(1, ["gh", "run", "list"])

        gate.list_workflow_runs = boom
        self.addCleanup(lambda: setattr(gate, "list_workflow_runs", original_list))
        self.addCleanup(lambda: setattr(gate, "git_tree_sha", original_tree))
        args = argparse.Namespace(
            event="push",
            repo="owner/repo",
            run_id="99",
            sha="HEAD",
            workflow="CI",
            limit=80,
        )
        self.assertEqual(gate.cmd_skip_if_green(args), 0)
        self.assertEqual(dest.read_text(encoding="utf-8"), "skip_suite=false\n")

    def test_write_github_output_appends_when_path_given(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        dest = Path(handle.name)
        self.addCleanup(dest.unlink)
        dest.write_text("", encoding="utf-8")
        gate.write_github_output("skip_suite", "true", path=str(dest))
        gate.write_github_output("skip_suite", "false", path=str(dest))
        self.assertEqual(
            dest.read_text(encoding="utf-8"),
            "skip_suite=true\nskip_suite=false\n",
        )

    def test_workflow_wires_reuse_job(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("reuse-green-tree", text)
        self.assertIn("ci_skip_if_green.py", text)
        self.assertIn("skip_suite", text)
        self.assertIn("needs: reuse-green-tree", text)
        self.assertIn("needs.reuse-green-tree.outputs.skip_suite != 'true'", text)


if __name__ == "__main__":
    unittest.main()
