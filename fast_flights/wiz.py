"""Helpers for reading Google's positional WizJSON.

The format strips trailing nulls to save wire bytes — so any leaf or branch
beyond the last non-null field is just absent. Reading `arr[idx]` directly
crashes; `at(arr, idx)` returns the default instead.
"""
from __future__ import annotations

from typing import Any


_MISSING = object()


def at(arr: Any, *path: int, default: Any = None) -> Any:
    """Safe positional access into nested WizJSON arrays.

    `at(payload, 7, 1, 1)` returns `payload[7][1][1]` if every intermediate
    step exists and is a list; otherwise returns `default`.

    Treats four conditions as "missing":
      - intermediate is None (Google explicitly nulled it)
      - intermediate is not a list (e.g., a stray int sentinel where a list was expected)
      - index is out of bounds (trailing-null truncation)
      - leaf value is `_MISSING` sentinel (never naturally occurs)
    """
    cur = arr
    for idx in path:
        if not isinstance(cur, list) or idx >= len(cur):
            return default
        cur = cur[idx]
        if cur is None:
            # Don't bail early — caller might want None vs default distinction
            # at the leaf, but intermediate Nones can't be indexed further.
            if idx is path[-1]:
                return None
            return default
    return cur


def at_or(arr: Any, *path: int, default: Any) -> Any:
    """Like `at` but coalesces None to default. Use when None is meaningless."""
    v = at(arr, *path, default=_MISSING)
    return default if (v is _MISSING or v is None) else v


def each(arr: Any, *path: int):
    """Iterate over the list at `arr[path]`, or empty if missing/None."""
    v = at(arr, *path)
    if isinstance(v, list):
        yield from v
