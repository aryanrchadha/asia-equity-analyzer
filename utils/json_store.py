"""Durable JSON state files shared by the watchlist and the processed-file ledger.

Both stores hold state that is expensive to rebuild — a watchlist is a score
history you can't reconstruct without re-running the analyses, and a ledger is
the record of what you've already paid to analyze. So both follow the same two
rules: a file that exists but isn't the store we expect is never overwritten,
and writes are atomic so an interrupted run can't truncate one.
"""

from __future__ import annotations

import json
import os
import tempfile


class StoreError(RuntimeError):
    """The file exists but could not be read as the expected store."""


def empty_store(top_key: str, version: int) -> dict:
    return {"version": version, top_key: {}}


def load_store(path: str, top_key: str, version: int) -> dict:
    """Read a JSON store, returning an empty one if the file doesn't exist.

    Raises:
        StoreError: If the file exists but isn't a valid store of this shape.
            Callers must not fall back to an empty store on this error —
            a malformed file is a problem to fix, not history to discard.
    """
    if not os.path.exists(path):
        return empty_store(top_key, version)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise StoreError(f"'{path}' is not valid JSON: {e}") from e

    if not isinstance(data, dict) or not isinstance(data.get(top_key), dict):
        raise StoreError(f"'{path}' is not the expected store (no '{top_key}' map).")
    found = data.get("version")
    if found != version:
        raise StoreError(
            f"'{path}' has schema version {found!r}, expected {version}."
        )
    return data


def save_store(data: dict, path: str) -> None:
    """Write a JSON store atomically via a temp file in the same directory."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
