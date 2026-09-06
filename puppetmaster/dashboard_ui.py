"""Packaged presentation assets for the zero-dependency dashboard.

The HTTP and snapshot code live in :mod:`puppetmaster.dashboard`.  Keeping the
HTML, tokens, styling, and browser state here makes that transport module
readable without introducing an asset route or a frontend build.
"""
from pathlib import Path
from typing import Tuple


_ASSET_DIR = Path(__file__).with_name("dashboard_assets")
_RENDERER_MARKER = "{{RENDERER_JS}}"


def _read_asset(name: str) -> str:
    return (_ASSET_DIR / name).read_text(encoding="utf-8")


def load_dashboard_fragments() -> Tuple[str, str]:
    """Return the HTML before and after the pure renderer helper script."""
    shell = _read_asset("shell.html")
    if shell.count(_RENDERER_MARKER) != 1:
        raise ValueError("dashboard shell must contain one renderer marker")
    before, after = shell.split(_RENDERER_MARKER)
    before = before.replace("{{TOKENS_CSS}}", _read_asset("tokens.css"))
    before = before.replace("{{DASHBOARD_CSS}}", _read_asset("dashboard.css"))
    after = after.replace("{{DASHBOARD_JS}}", _read_asset("dashboard.js"))
    return before, after
