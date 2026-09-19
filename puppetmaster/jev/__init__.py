from __future__ import annotations

import os
from typing import Any, Optional

from .client import decide, opted_in, parse_noul, resolve_openrouter_key

_OPT_IN = frozenset(("1", "true", "on", "yes"))


def enabled() -> bool:
    """Opt-in plus a resolvable OpenRouter key. Unset never opens a socket."""
    if not opted_in():
        return False
    return bool(resolve_openrouter_key())


def acting() -> bool:
    """Opt-in plus ``PUPPETMASTER_JEV_ACT``. Default is observe-only.

    Observe still scores and writes GATE rows. It never skips, demotes,
    reuses, or stops. ACT is a later experiment switch, not a ship default.
    """
    if not opted_in():
        return False
    raw = (os.environ.get("PUPPETMASTER_JEV_ACT") or "").strip().lower()
    return raw in _OPT_IN


def decide_noul(
    state: Any,
    question: str,
    key: str = "noul",
    *,
    api_key: str = "",
) -> Optional[float]:
    """Ask one noul. None when unset, disabled, or any failure. Never raises."""
    text = (question or "").strip()
    if not text or not enabled():
        return None
    body = decide(
        state,
        {key: {"type": "noul", "instructions": text}},
        key=api_key,
    )
    if body is None:
        return None
    return parse_noul(body.get("answers"), key)


__all__ = [
    "acting",
    "decide",
    "decide_noul",
    "enabled",
    "opted_in",
    "parse_noul",
    "resolve_openrouter_key",
]
