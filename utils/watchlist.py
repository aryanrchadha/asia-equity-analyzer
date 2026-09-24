"""Persistent watchlist: score history across runs, with move alerts.

Each analyzed filing can be recorded against a company key. On the next run
for that company the new composite is compared against the previous entry,
and a move at or beyond the alert threshold is surfaced to the console.

The store is a single JSON file so it can be inspected, diffed, and checked
into version control alongside the reports it references.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from config import WATCHLIST_ALERT_THRESHOLD, WATCHLIST_PATH
from utils.json_store import StoreError, empty_store, load_store, save_store
from utils.report_writer import _escape_table_cell
from utils.score_parser import GOVERNANCE_TO_NUMERIC, ScoreCard

SCHEMA_VERSION = 1

# Governance ratings at or below this numeric value are treated as adverse, so
# a move into one is worth flagging even without a composite move.
ADVERSE_GOVERNANCE = {"CONCERNING", "RED FLAG"}


# The watchlist's own name for a malformed store; callers catch this.
WatchlistError = StoreError


@dataclass
class Alert:
    """A material move detected against the previous recorded entry."""

    company: str
    kind: str      # "composite" | "governance"
    message: str
    adverse: bool  # True when the move is in the unfavorable direction


def empty_watchlist() -> dict:
    return empty_store("companies", SCHEMA_VERSION)


def load_watchlist(path: str = WATCHLIST_PATH) -> dict:
    """Read the watchlist, returning an empty store if the file doesn't exist.

    Raises:
        WatchlistError: If the file exists but isn't a valid watchlist.
    """
    return load_store(path, "companies", SCHEMA_VERSION)


def save_watchlist(data: dict, path: str = WATCHLIST_PATH) -> None:
    """Write the watchlist atomically so an interrupted run can't corrupt it."""
    save_store(data, path)


def scores_to_dict(scores: ScoreCard) -> dict:
    """Serialize a ScoreCard for storage."""
    return {
        "revenue_quality": scores.revenue_quality,
        "margins": scores.margins,
        "balance_sheet": scores.balance_sheet,
        "cash_flow": scores.cash_flow,
        "moat": scores.moat,
        "governance": scores.governance,
        "composite": scores.composite,
    }


def _detect_alerts(
    company: str, previous: dict | None, current: dict, threshold: float
) -> list[Alert]:
    """Compare a new entry against the previous one for the same company."""
    if previous is None:
        return []

    alerts: list[Alert] = []
    old_scores = previous.get("scores", {})
    new_scores = current["scores"]

    old_composite = old_scores.get("composite")
    new_composite = new_scores.get("composite")
    if old_composite is not None and new_composite is not None:
        delta = new_composite - old_composite
        if abs(delta) >= threshold:
            direction = "improved" if delta > 0 else "deteriorated"
            alerts.append(
                Alert(
                    company=company,
                    kind="composite",
                    message=(
                        f"composite {direction} {old_composite:g} → {new_composite:g} "
                        f"({delta:+g}, threshold ±{threshold:g})"
                    ),
                    adverse=delta < 0,
                )
            )

    old_gov = old_scores.get("governance")
    new_gov = new_scores.get("governance")
    if old_gov and new_gov and old_gov != new_gov:
        old_rank = GOVERNANCE_TO_NUMERIC.get(old_gov)
        new_rank = GOVERNANCE_TO_NUMERIC.get(new_gov)
        adverse = new_gov in ADVERSE_GOVERNANCE
        if old_rank is not None and new_rank is not None:
            adverse = new_rank < old_rank
        alerts.append(
            Alert(
                company=company,
                kind="governance",
                message=f"governance rating {old_gov} → {new_gov}",
                adverse=adverse,
            )
        )

    return alerts


def record_analysis(
    data: dict,
    company: str,
    source_file: str,
    report_path: str,
    model: str,
    scores: ScoreCard,
    recorded_at: str | None = None,
    threshold: float = WATCHLIST_ALERT_THRESHOLD,
) -> list[Alert]:
    """Append an entry for `company` and return any alerts vs. the prior entry.

    Mutates `data` in place; the caller is responsible for saving it.
    """
    entry = {
        "recorded_at": recorded_at or datetime.now().isoformat(timespec="seconds"),
        "source_file": source_file,
        "report_path": report_path,
        "model": model,
        "scores": scores_to_dict(scores),
    }
    company_record = data["companies"].setdefault(company, {"entries": []})
    entries = company_record["entries"]
    previous = entries[-1] if entries else None
    alerts = _detect_alerts(company, previous, entry, threshold)
    entries.append(entry)
    return alerts


def _format_value(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def format_watchlist_table(data: dict, threshold: float = WATCHLIST_ALERT_THRESHOLD) -> str:
    """Render the watchlist as a markdown table, most recently updated first."""
    companies = data.get("companies", {})
    if not companies:
        return "_Watchlist is empty._"

    header = (
        "| Company | Filings | Latest Composite | Previous | Δ | Governance "
        "| Last Updated | Alert |"
    )
    divider = "|" + "---|" * 8

    rows = []
    for company, record in companies.items():
        entries = record.get("entries", [])
        if not entries:
            continue
        latest = entries[-1]
        previous = entries[-2] if len(entries) > 1 else None
        latest_scores = latest.get("scores", {})
        new_composite = latest_scores.get("composite")
        old_composite = previous.get("scores", {}).get("composite") if previous else None

        if new_composite is not None and old_composite is not None:
            delta = new_composite - old_composite
            delta_text = "unchanged" if delta == 0 else f"{delta:+g}"
            flagged = abs(delta) >= threshold
        else:
            delta_text = "n/a"
            flagged = False

        alert_text = "⚠️" if flagged else ""
        rows.append(
            (
                latest.get("recorded_at", ""),
                f"| {_escape_table_cell(company)} | {len(entries)} | {_format_value(new_composite)} "
                f"| {_format_value(old_composite)} | {delta_text} "
                f"| {_escape_table_cell(_format_value(latest_scores.get('governance')))} "
                f"| {latest.get('recorded_at', 'n/a')} | {alert_text} |",
            )
        )

    rows.sort(key=lambda row: row[0], reverse=True)
    return "\n".join([header, divider] + [row for _, row in rows])
