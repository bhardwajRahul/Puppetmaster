"""Stdlib-only unittest sharding for Windows CI.

``UNITTEST_SHARD=2/4`` (1-based) keeps tests whose sorted index satisfies
``i % total == (idx - 1)``. Empty or invalid specs are a no-op so a local
``python -m unittest discover -s tests -v`` run is unchanged.
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import Iterator, List, Optional, Sequence, Tuple


def parse_unittest_shard(spec: Optional[str]) -> Optional[Tuple[int, int]]:
    """Parse ``2/4`` into a 1-based ``(idx, total)`` pair, or None if unusable."""
    raw = (spec or "").strip()
    if not raw:
        return None
    parts = raw.split("/")
    if len(parts) != 2:
        return None
    try:
        idx = int(parts[0])
        total = int(parts[1])
    except ValueError:
        return None
    if total < 1 or idx < 1 or idx > total:
        return None
    return (idx, total)


def select_unittest_shard(ids: Sequence[str], spec: Optional[str]) -> List[str]:
    """Stable-sort ``ids`` and keep the requested shard, or return all as-is."""
    parsed = parse_unittest_shard(spec)
    if parsed is None:
        return list(ids)
    idx, total = parsed
    ordered = sorted(ids)
    keep_slot = idx - 1
    return [item for i, item in enumerate(ordered) if i % total == keep_slot]


def _iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for child in _iter_cases(item):
                yield child
        else:
            yield item


def filter_suite(suite: unittest.TestSuite, spec: Optional[str]) -> unittest.TestSuite:
    """Keep TestCase entries whose ids fall in ``spec``; walk nested suites."""
    if parse_unittest_shard(spec) is None:
        return suite
    cases = list(_iter_cases(suite))
    keep = set(select_unittest_shard([case.id() for case in cases], spec))
    return unittest.TestSuite(case for case in cases if case.id() in keep)


def discover_and_run() -> None:
    spec = os.environ.get("UNITTEST_SHARD")
    suite = unittest.TestLoader().discover("tests")
    if spec:
        suite = filter_suite(suite, spec)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    discover_and_run()
