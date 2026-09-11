"""Pure-function tests for Windows CI unittest sharding.

Uses a fake id list (not live discovery) so UNITTEST_SHARD cannot skip these
assertions when the helper itself is under a shard filter.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.ci_shard import (
    _iter_cases,
    filter_suite,
    parse_unittest_shard,
    select_unittest_shard,
)


# Unsorted on purpose: a valid shard must sort, an invalid spec must not.
_FAKE_IDS = ("z", "a", "m", "b", "y", "c", "x", "d")
_SORTED_IDS = ("a", "b", "c", "d", "m", "x", "y", "z")


def _named_case(test_id: str) -> unittest.FunctionTestCase:
    case = unittest.FunctionTestCase(lambda: None)
    case.id = lambda _test_id=test_id: _test_id
    return case


class ParseUnittestShardTests(unittest.TestCase):
    def test_empty_and_invalid_are_none(self) -> None:
        for spec in ("", None, "   ", "nope", "2", "2/4/5", "a/4", "2/b", "0/4", "5/4", "2/0"):
            self.assertIsNone(parse_unittest_shard(spec), spec)

    def test_valid_one_based_pair(self) -> None:
        self.assertEqual(parse_unittest_shard("2/4"), (2, 4))
        self.assertEqual(parse_unittest_shard(" 1/4 "), (1, 4))
        self.assertEqual(parse_unittest_shard("4/4"), (4, 4))


class SelectUnittestShardTests(unittest.TestCase):
    def test_empty_and_invalid_are_identity(self) -> None:
        ids = list(_FAKE_IDS)
        for spec in ("", None, "nope", "0/4", "5/4"):
            self.assertEqual(select_unittest_shard(ids, spec), ids)

    def test_four_shards_partition(self) -> None:
        parts = [select_unittest_shard(_FAKE_IDS, "%s/4" % n) for n in (1, 2, 3, 4)]
        self.assertEqual(parts[0], ["a", "m"])
        self.assertEqual(parts[1], ["b", "x"])
        self.assertEqual(parts[2], ["c", "y"])
        self.assertEqual(parts[3], ["d", "z"])
        flat = [item for part in parts for item in part]
        self.assertEqual(len(flat), len(_SORTED_IDS))
        self.assertEqual(len(set(flat)), len(_SORTED_IDS))
        self.assertEqual(sorted(flat), list(_SORTED_IDS))
        for left, right in ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)):
            self.assertFalse(set(parts[left]) & set(parts[right]))


class FilterSuiteTests(unittest.TestCase):
    def _nested_suite(self) -> unittest.TestSuite:
        inner = unittest.TestSuite([_named_case("c"), _named_case("a")])
        return unittest.TestSuite(
            [_named_case("b"), inner, _named_case("d"), _named_case("m")]
        )

    def test_empty_and_invalid_are_identity(self) -> None:
        suite = self._nested_suite()
        original = [case.id() for case in _iter_cases(suite)]
        for spec in ("", None, "nope", "0/4"):
            filtered = filter_suite(suite, spec)
            self.assertIs(filtered, suite)
            self.assertEqual([case.id() for case in _iter_cases(filtered)], original)

    def test_drops_tests_outside_the_shard(self) -> None:
        # Nested ids walk to b, c, a, d, m; select sorts to a,b,c,d,m.
        # Shard 2/4 keeps sorted index 1, 5, ... → b.
        filtered = filter_suite(self._nested_suite(), "2/4")
        self.assertEqual([case.id() for case in filtered], ["b"])
        shard1 = filter_suite(self._nested_suite(), "1/4")
        self.assertEqual([case.id() for case in shard1], ["a", "m"])


class WindowsWorkflowTests(unittest.TestCase):
    def test_ci_yml_shards_windows_only(self) -> None:
        text = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("test-windows:", text)
        self.assertIn("UNITTEST_SHARD", text)
        self.assertIn("python -m tests.ci_shard", text)
        self.assertIn("vars.CI_WINDOWS_RUNNER || 'windows-latest'", text)
        self.assertNotIn("runs-on: blacksmith-", text)
        self.assertIn("python -m unittest discover -s tests -v", text)


if __name__ == "__main__":
    unittest.main()
