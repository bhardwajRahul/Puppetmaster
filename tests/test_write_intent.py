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
            ("future-adapter", {}, True),
        ]
        for adapter, payload, expected in cases:
            self.assertEqual(
                adapter_may_write(adapter, payload),
                expected,
                (adapter, payload),
            )
