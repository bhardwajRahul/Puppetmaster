"""Shared write-intent matrix stays aligned with edit admission."""
from __future__ import annotations

import os
import sys
import unittest

_HERMETIC_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERMETIC_DIR not in sys.path:
    sys.path.insert(0, _HERMETIC_DIR)
import hermetic_env  # noqa: F401

from puppetmaster.adapters.registry import ADAPTERS
from puppetmaster.write_intent import adapter_may_write


class WriteIntentTests(unittest.TestCase):
    def test_registered_adapters_are_classified(self) -> None:
        for name in ADAPTERS:
            self.assertIsInstance(adapter_may_write(name, {}), bool, name)

    def test_admission_matrix(self) -> None:
        cases = [
            ("local", {}, False),
            ("local", {"mode": "implement"}, True),
            ("openai", {}, False),
            ("openai", {"mode": "implement"}, True),
            ("shell", {"read_only": True}, True),
            ("codex", {"sandbox": "read-only"}, False),
            (
                "codex",
                {
                    "sandbox": "read-only",
                    "dangerously_bypass_approvals_and_sandbox": True,
                },
                True,
            ),
            ("claude-code", {"permission_mode": "plan"}, False),
            ("claude-code", {"permission_mode": "acceptEdits"}, True),
            ("agy", {"mode": "plan"}, False),
            ("agy", {"mode": "accept-edits"}, True),
            ("cursor", {}, True),
            # Regression: the agentic adapter is what the Marionette harness
            # pins for run_swarm (HARNESS_SWARM_ADAPTER=agentic), and the
            # harness marks every analysis role read_only/no_edit/dry_run.
            # agentic was absent from this matrix, so it kept returning True
            # and every read-only swarm worker took an exclusive claim on its
            # whole write scope (default ".") -- 1 winner, N-1
            # EditAdmissionTimeout failures. Read-only must be a hard fence.
            ("agentic", {}, True),
            ("agentic", {"read_only": True}, False),
            ("agentic", {"read_only": True, "no_edit": True, "dry_run": True}, False),
            ("agentic", {"mode": "implement"}, True),
            ("agentic", {"sandbox": "read-only"}, False),
            ("cursor", {"read_only": True}, False),
            ("hermes", {"no_edit": True}, False),
            # shell is an acting adapter: a read-only hint never disarms it.
            ("future-adapter", {}, True),
        ]
        for adapter, payload, expected in cases:
            self.assertEqual(
                adapter_may_write(adapter, payload),
                expected,
                (adapter, payload),
            )

    def test_swarm_mode_analysis_is_a_conservative_override(self) -> None:
        """swarm_mode was declared then discarded; honour it one-way only."""
        # "analysis" can remove a claim but never grant one.
        self.assertFalse(adapter_may_write("agentic", {}, swarm_mode="analysis"))
        self.assertFalse(adapter_may_write("cursor", {}, swarm_mode="analysis"))
        self.assertFalse(adapter_may_write("hermes", {}, swarm_mode="analysis"))
        # An explicit implement payload still wins (matches spec_edits_files).
        self.assertTrue(
            adapter_may_write("agentic", {"mode": "implement"}, swarm_mode="analysis"))
        # "edit" grants nothing on its own; unknown/None change nothing.
        self.assertFalse(adapter_may_write("local", {}, swarm_mode="edit"))
        self.assertTrue(adapter_may_write("agentic", {}, swarm_mode="edit"))
        self.assertTrue(adapter_may_write("agentic", {}, swarm_mode=None))
