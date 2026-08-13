"""Ledger of filings already analyzed, so a watched directory isn't re-billed.

Files are keyed by a SHA-256 of their contents rather than by path. That gets
the semantics right in both directions: re-downloading or renaming the same
filing does not trigger a second (paid) analysis, while an amended filing that
reuses its filename is correctly treated as new work.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime

from config import LEDGER_PATH, WATCH_MAX_ATTEMPTS, WATCH_SETTLE_SECONDS
from utils.json_store import StoreError, empty_store, load_store, save_store

SCHEMA_VERSION = 1

# The ledger's own name for a malformed store; callers catch this.
LedgerError = StoreError

_CHUNK = 1024 * 1024


def empty_ledger() -> dict:
    return empty_store("files", SCHEMA_VERSION)


def load_ledger(path: str = LEDGER_PATH) -> dict:
    """Read the ledger, returning an empty one if the file doesn't exist.

    Raises:
        LedgerError: If the file exists but isn't a valid ledger. Falling back
            to an empty ledger here would re-analyze — and re-bill — every
            filing in the watched directory.
    """
    return load_store(path, "files", SCHEMA_VERSION)


def save_ledger(data: dict, path: str = LEDGER_PATH) -> None:
    """Write the ledger atomically."""
    save_store(data, path)


def file_digest(path: str) -> str:
    """SHA-256 of a file's contents, read in chunks so large PDFs stay cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_stable(path: str, settle_seconds: float = WATCH_SETTLE_SECONDS) -> bool:
    """True once a file has stopped changing.

    A filing that is still being copied or downloaded would otherwise be
    analyzed half-written — producing a garbage report at full API cost — so
    a file is only eligible once its mtime is at least `settle_seconds` old.
    """
    try:
        return (time.time() - os.path.getmtime(path)) >= settle_seconds
    except OSError:
        return False


def entry_for(data: dict, digest: str) -> dict | None:
    return data["files"].get(digest)


def status_of(
    data: dict, digest: str, max_attempts: int = WATCH_MAX_ATTEMPTS
) -> str:
    """One of 'new', 'done', or 'exhausted' for a given file digest."""
    entry = entry_for(data, digest)
    if entry is None:
        return "new"
    if entry.get("status") == "analyzed":
        return "done"
    if entry.get("attempts", 0) >= max_attempts:
        return "exhausted"
    return "new"


def mark_analyzed(
    data: dict,
    digest: str,
    filename: str,
    report_path: str,
    recorded_at: str | None = None,
) -> None:
    """Record a successful analysis. Mutates `data`; caller saves it."""
    data["files"][digest] = {
        "status": "analyzed",
        "filename": filename,
        "report_path": report_path,
        "analyzed_at": recorded_at or datetime.now().isoformat(timespec="seconds"),
        "attempts": entry_for(data, digest).get("attempts", 0) + 1
        if entry_for(data, digest)
        else 1,
    }


def mark_failed(
    data: dict,
    digest: str,
    filename: str,
    reason: str,
    recorded_at: str | None = None,
) -> int:
    """Record a failed attempt and return the new attempt count.

    Failures are counted rather than treated as final so a transient error
    (rate limit, network) retries on the next poll — but the count is what
    stops a permanently broken file from being retried, and re-billed,
    forever.
    """
    entry = entry_for(data, digest) or {}
    attempts = entry.get("attempts", 0) + 1
    data["files"][digest] = {
        "status": "failed",
        "filename": filename,
        "reason": reason,
        "attempts": attempts,
        "last_attempt_at": recorded_at or datetime.now().isoformat(timespec="seconds"),
    }
    return attempts


def pending_files(
    paths: list[str],
    data: dict,
    settle_seconds: float = WATCH_SETTLE_SECONDS,
    max_attempts: int = WATCH_MAX_ATTEMPTS,
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Split candidate paths into work to do and files to leave alone.

    Returns:
        (pending, waiting, duplicates) where pending is a list of
        (path, digest) ready to analyze, waiting is the paths still settling,
        and duplicates are paths whose content matches another file already
        queued in this same pass. Files already analyzed, or whose attempts
        are exhausted, appear in none of the three.

    The in-pass duplicate check matters: two copies of one filing landing
    together are both absent from the ledger, so without it each would be
    analyzed — and billed — separately.
    """
    pending, waiting, duplicates = [], [], []
    queued: set[str] = set()
    for path in paths:
        if not is_stable(path, settle_seconds):
            waiting.append(path)
            continue
        try:
            digest = file_digest(path)
        except OSError:
            waiting.append(path)
            continue
        if status_of(data, digest, max_attempts) != "new":
            continue
        if digest in queued:
            duplicates.append(path)
            continue
        queued.add(digest)
        pending.append((path, digest))
    return pending, waiting, duplicates


def ledger_summary(data: dict) -> tuple[int, int, int]:
    """(analyzed, failed, exhausted-and-given-up) counts, for reporting."""
    analyzed = failed = exhausted = 0
    for entry in data["files"].values():
        if entry.get("status") == "analyzed":
            analyzed += 1
        else:
            failed += 1
            if entry.get("attempts", 0) >= WATCH_MAX_ATTEMPTS:
                exhausted += 1
    return analyzed, failed, exhausted
