from __future__ import annotations

"""OpenRouter Decisions client. Parse at the boundary; never raise to callers."""

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .questions import ENDPOINT, MODEL, TIMEOUT_SECONDS

_KEY_ENVS = ("PUPPETMASTER_OPENROUTER_API_KEY", "OPENROUTER_API_KEY")
_OPT_IN = frozenset(("1", "true", "on", "yes"))


def opted_in() -> bool:
    """User asked for Jev. Empty, auto, and off stay off. Key is not enough."""
    raw = (os.environ.get("PUPPETMASTER_JEV") or "").strip().lower()
    return raw in _OPT_IN


def resolve_openrouter_key() -> str:
    """Return a usable OpenRouter key or empty. Never logs the secret.

    Order is Puppetmaster-owned env, then the generic OpenRouter env.
    V1 does not read Marionette ``keys.json``.
    """
    for name in _KEY_ENVS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def decide(
    state: Any,
    questions: Dict[str, Any],
    key: str = "",
) -> Optional[dict]:
    """POST one System One request. None on any failure. Never raises."""
    explicit = (key or "").strip()
    if not explicit and not opted_in():
        return None
    token = explicit or resolve_openrouter_key()
    if not token or not questions:
        return None
    payload = {"model": MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/professorpalmer/Puppetmaster",
            "X-OpenRouter-Title": "Puppetmaster Jev",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None
    except Exception:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        return None
    return body


def parse_noul(answers: Any, key: str) -> Optional[float]:
    """Return a 0..1 noul or None when the answer row is missing or malformed."""
    if not isinstance(answers, dict):
        return None
    row = answers.get(key)
    if not isinstance(row, dict):
        return None
    raw = row.get("noul")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value
