"""Deterministic aggregate statistics across a batch of analyzed companies.

Everything here is computed in Python from the parsed scores, so the sector
summary agent interprets numbers rather than producing them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from utils.score_parser import GOVERNANCE_RATINGS

# (label, ScoreCard attribute, maximum) for each numeric dimension.
DIMENSIONS = (
    ("Revenue Quality", "revenue_quality", 5),
    ("Margin Analysis", "margins", 5),
    ("Balance Sheet", "balance_sheet", 5),
    ("Cash Flow Quality", "cash_flow", 5),
    ("Composite Moat", "moat", 15),
    ("Composite Score", "composite", 5),
)


@dataclass
class DimensionStats:
    """Aggregates for one scored dimension across the batch."""

    label: str
    maximum: int
    values: list[float]
    covered: int      # companies with a parsed score for this dimension
    total: int        # companies in the batch

    @property
    def mean(self) -> float | None:
        return statistics.fmean(self.values) if self.values else None

    @property
    def median(self) -> float | None:
        return statistics.median(self.values) if self.values else None

    @property
    def minimum(self) -> float | None:
        return min(self.values) if self.values else None

    @property
    def maximum_value(self) -> float | None:
        return max(self.values) if self.values else None

    @property
    def spread(self) -> float | None:
        """Max minus min — how tightly the sector clusters on this dimension."""
        if not self.values:
            return None
        return max(self.values) - min(self.values)


def _score_of(filing, attribute: str) -> float | None:
    return getattr(filing.scores, attribute)


def dimension_stats(filings: list) -> list[DimensionStats]:
    """Aggregate every numeric dimension across the batch."""
    stats = []
    for label, attribute, maximum in DIMENSIONS:
        values = [
            value
            for value in (_score_of(f, attribute) for f in filings)
            if value is not None
        ]
        stats.append(
            DimensionStats(
                label=label,
                maximum=maximum,
                values=values,
                covered=len(values),
                total=len(filings),
            )
        )
    return stats


def _leaders(filings: list, attribute: str) -> tuple[str | None, str | None]:
    """Company labels with the highest and lowest score on one dimension."""
    scored = [(f, _score_of(f, attribute)) for f in filings]
    scored = [(f, v) for f, v in scored if v is not None]
    if not scored:
        return None, None
    best = max(scored, key=lambda pair: pair[1])[0].label
    worst = min(scored, key=lambda pair: pair[1])[0].label
    return best, worst


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def build_stats_table(filings: list) -> str:
    """Render the per-dimension sector statistics table."""
    header = (
        "| Dimension | Covered | Mean | Median | Min | Max | Spread "
        "| Highest | Lowest |"
    )
    divider = "|" + "---|" * 9

    rows = []
    for stats, (_, attribute, _maximum) in zip(dimension_stats(filings), DIMENSIONS):
        best, worst = _leaders(filings, attribute)
        mean = stats.mean
        rows.append(
            f"| {stats.label} (of {stats.maximum}) | {stats.covered}/{stats.total} "
            f"| {'n/a' if mean is None else f'{mean:.2f}'} "
            f"| {_format(stats.median)} | {_format(stats.minimum)} "
            f"| {_format(stats.maximum_value)} | {_format(stats.spread)} "
            f"| {best or 'n/a'} | {worst or 'n/a'} |"
        )
    return "\n".join([header, divider] + rows)


def governance_distribution(filings: list) -> dict[str, int]:
    """Count companies per governance rating, including unparsed ones."""
    counts = {rating: 0 for rating in GOVERNANCE_RATINGS}
    counts["NOT RATED"] = 0
    for filing in filings:
        rating = filing.scores.governance
        counts[rating if rating in counts else "NOT RATED"] += 1
    return counts


def build_governance_table(filings: list) -> str:
    """Render the governance rating distribution as a small table."""
    counts = governance_distribution(filings)
    total = len(filings) or 1
    header = "| Governance Rating | Companies | Share |"
    divider = "|---|---|---|"
    rows = [
        f"| {rating} | {count} | {count / total * 100:.0f}% |"
        for rating, count in counts.items()
        if count
    ]
    return "\n".join([header, divider] + rows)
