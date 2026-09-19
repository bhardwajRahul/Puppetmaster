#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))
import hermetic_env  # noqa: F401

from puppetmaster.jev import acting, opted_in


def main() -> int:
    leaked = [
        name
        for name in ("PUPPETMASTER_JEV", "PUPPETMASTER_JEV_ACT")
        if os.environ.get(name)
    ]
    body = {
        "repo": str(REPO),
        "opted_in": opted_in(),
        "acting": acting(),
        "host_jev_leak": leaked,
        "ok": (not opted_in()) and (not acting()) and (not leaked),
    }
    print(json.dumps(body, indent=2))
    return 0 if body["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
