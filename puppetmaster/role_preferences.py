"""User-local per-role routing preference. Taste, not evidence."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from puppetmaster.community_observations import puppetmaster_home
from puppetmaster.model_registry import ModelSpec, model_id_allowed


_STORE_NAME = "role-preferences.json"
VALID_MODES = frozenset({"soft", "strict"})


@dataclass(frozen=True)
class RolePreference:
    """Declared order for one routing role."""

    preferred: tuple[str, ...]
    mode: str = "soft"

    def __post_init__(self) -> None:
        mode = (self.mode or "soft").strip().lower() or "soft"
        if mode not in VALID_MODES:
            object.__setattr__(self, "mode", "soft")
        else:
            object.__setattr__(self, "mode", mode)


def default_role_preferences_path() -> Path:
    return puppetmaster_home() / _STORE_NAME


def _preference_from_raw(raw: Any) -> Optional[RolePreference]:
    if not isinstance(raw, dict):
        return None
    preferred_raw = raw.get("preferred") or []
    if not isinstance(preferred_raw, list):
        return None
    preferred = tuple(
        str(item).strip() for item in preferred_raw if str(item).strip()
    )
    mode = str(raw.get("mode") or "soft").strip().lower() or "soft"
    if mode not in VALID_MODES:
        mode = "soft"
    return RolePreference(preferred=preferred, mode=mode)


def load_role_preferences(
    path: Optional[Path] = None,
) -> dict[str, RolePreference]:
    """Missing or corrupt files are empty. Never raise on the hot path."""
    resolved = path or default_role_preferences_path()
    try:
        if not resolved.is_file():
            return {}
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    roles = raw.get("roles", raw)
    if not isinstance(roles, dict):
        return {}
    out: dict[str, RolePreference] = {}
    for role, body in roles.items():
        key = str(role or "").strip()
        parsed = _preference_from_raw(body)
        if key and parsed is not None:
            out[key] = parsed
    return out


def apply_strict(
    candidates: Iterable[ModelSpec],
    preferred: Iterable[str],
) -> list[ModelSpec]:
    allowed = [item for item in preferred if str(item).strip()]
    if not allowed:
        return []
    return [spec for spec in candidates if model_id_allowed(spec, allowed)]


def first_soft_preferred(
    candidates: Iterable[ModelSpec],
    preferred: Iterable[str],
) -> Optional[ModelSpec]:
    pool = list(candidates)
    for name in preferred:
        needle = str(name).strip()
        if not needle:
            continue
        for spec in pool:
            if model_id_allowed(spec, [needle]):
                return spec
    return None
