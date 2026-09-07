"""Strict JSON for byte-bounded metadata and cursor input boundaries."""
import json
import math

MAX_INTEGER = 2**63 - 1


def _integer(text):
    # Reject before int conversion, including on Python without digit limits.
    if len(text) > 20:
        raise ValueError("metadata integer out of range")
    value = int(text)
    if not -2**63 <= value <= MAX_INTEGER:
        raise ValueError("metadata integer out of range")
    return value


def _float(text):
    value = float(text)
    if not math.isfinite(value) or not -MAX_INTEGER <= value <= MAX_INTEGER:
        raise ValueError("metadata number out of range")
    return value


def _constant(text):
    raise ValueError("nonfinite metadata number")


def loads(text):
    """Reject excessive depth and unsafe numbers as ValueError, never coerce."""
    try:
        value = json.loads(text, parse_int=_integer, parse_float=_float,
                           parse_constant=_constant)
    except RecursionError as exc:
        raise ValueError("metadata JSON nesting exceeds limit") from exc
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError("metadata JSON nesting exceeds limit")
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
    return value
