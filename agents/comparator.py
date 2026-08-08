"""Cross-period comparison: score deltas plus a trend-analysis agent.

Each filing is analyzed independently by the multi-agent pipeline; this module
compares the finished reports. The score table is built deterministically from
the explicit "SCORE: X/N" lines (see utils/score_parser.py) — only the trend
narrative comes from a model call.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import MODEL, SYNTHESIS_MAX_TOKENS
from agents.orchestrator import (
    MultiAnalysisResult,
    SectionResult,
    _call,
    api_errors,
    make_client,
)
from prompts.comparison import COMPARISON_INSTRUCTIONS
from prompts.peer_comparison import PEER_COMPARISON_INSTRUCTIONS
from prompts.section_prompts import BASE_ANALYST_CONTEXT
from utils.score_parser import (
    GOVERNANCE_TO_NUMERIC,
    ScoreCard,
    parse_scores,
    split_sections,
)


@dataclass
class FilingAnalysis:
    """One analyzed filing plus its parsed scores."""

    label: str
    filename: str
    analysis: MultiAnalysisResult
    scores: ScoreCard
    report_path: str


def _format_score(value: float | None, maximum: int) -> str:
    if value is None:
        return "n/a"
    number = f"{value:g}"
    return f"{number}/{maximum}"


def _format_delta(first: float | None, last: float | None) -> str:
    if first is None or last is None:
        return "n/a"
    delta = last - first
    if delta == 0:
        return "unchanged"
    return f"{delta:+g}"


def build_score_table(filings: list[FilingAnalysis]) -> str:
    """Render the period-by-period score table with a first-to-last delta column."""
    labels = [f.label for f in filings]
    header = "| Dimension | " + " | ".join(labels) + " | Δ (first → last) |"
    divider = "|" + "---|" * (len(labels) + 2)

    rows = []
    dimension_maps = [f.scores.numeric_dimensions() for f in filings]
    for dimension in dimension_maps[0]:
        values = [dims[dimension][0] for dims in dimension_maps]
        maximum = dimension_maps[0][dimension][1]
        cells = " | ".join(_format_score(v, maximum) for v in values)
        rows.append(
            f"| {dimension} | {cells} | {_format_delta(values[0], values[-1])} |"
        )

    ratings = [f.scores.governance for f in filings]
    rating_cells = " | ".join(r if r else "n/a" for r in ratings)
    numeric = [GOVERNANCE_TO_NUMERIC.get(r) if r else None for r in ratings]
    rows.append(
        f"| Governance | {rating_cells} | {_format_delta(numeric[0], numeric[-1])} |"
    )

    return "\n".join([header, divider] + rows)


def _period_context(filing: FilingAnalysis) -> str:
    """The tensions and bottom-line sections from one period's report."""
    sections = split_sections(filing.analysis.text)
    parts = [sections.get(8), sections.get(10)]
    body = "\n\n".join(p for p in parts if p) or "(sections 8/10 not found in report)"
    return f"### PERIOD: {filing.label}\n\n{body}"


def _run_comparison_agent(
    user_content: str, model: str, key: str, title: str, start_message: str
) -> SectionResult:
    """Shared plumbing for the single-call comparison agents.

    Raises:
        SystemExit: If the API key is missing or the API call fails.
    """
    client = make_client()

    print(start_message)
    with api_errors():
        result = _call(
            client,
            [{"type": "text", "text": BASE_ANALYST_CONTEXT}],
            user_content,
            model,
            SYNTHESIS_MAX_TOKENS,
            key,
            title,
        )

    print(f"   ✅ {result.title} ({result.elapsed_seconds:.1f}s)")
    if result.truncated:
        print(
            f"   ⚠️  {result.title} was cut off at the {SYNTHESIS_MAX_TOKENS:,}-token "
            "limit. Consider raising SYNTHESIS_MAX_TOKENS in config.py."
        )
    return result


def analyze_trajectory(
    filings: list[FilingAnalysis], model: str = MODEL
) -> SectionResult:
    """Run the trend-analysis agent over the per-period analyses."""
    user_content = (
        COMPARISON_INSTRUCTIONS
        + "SCORE TRAJECTORY TABLE:\n\n"
        + build_score_table(filings)
        + "\n\nPER-PERIOD ANALYSES (chronological, earliest first):\n\n"
        + "\n\n".join(_period_context(f) for f in filings)
    )
    return _run_comparison_agent(
        user_content,
        model,
        "trajectory",
        "Cross-Period Trajectory",
        "📈 Analyzing cross-period trajectory...",
    )


def _composite_sort_key(filing: FilingAnalysis) -> tuple[int, float]:
    """Sort by composite descending, companies without a composite last."""
    composite = filing.scores.composite
    return (0, -composite) if composite is not None else (1, 0.0)


def rank_peers(filings: list[FilingAnalysis]) -> list[FilingAnalysis]:
    """Order companies by composite score, best first (n/a composites last)."""
    return sorted(filings, key=_composite_sort_key)


def build_ranking_table(ranked: list[FilingAnalysis]) -> str:
    """Render the peer ranking table, one row per company, best composite first."""
    header = (
        "| Rank | Company | Revenue | Margins | Balance Sheet | Cash Flow "
        "| Moat | Governance | Composite |"
    )
    divider = "|" + "---|" * 9
    rows = []
    for rank, filing in enumerate(ranked, start=1):
        s = filing.scores
        rows.append(
            f"| {rank} | {filing.label} "
            f"| {_format_score(s.revenue_quality, 5)} "
            f"| {_format_score(s.margins, 5)} "
            f"| {_format_score(s.balance_sheet, 5)} "
            f"| {_format_score(s.cash_flow, 5)} "
            f"| {_format_score(s.moat, 15)} "
            f"| {s.governance or 'n/a'} "
            f"| {_format_score(s.composite, 5)} |"
        )
    return "\n".join([header, divider] + rows)


def _peer_context(filing: FilingAnalysis) -> str:
    """The overview, tensions, and bottom-line sections for one company."""
    sections = split_sections(filing.analysis.text)
    parts = [sections.get(1), sections.get(8), sections.get(10)]
    body = "\n\n".join(p for p in parts if p) or "(sections 1/8/10 not found in report)"
    return f"### COMPANY: {filing.label}\n\n{body}"


def analyze_peers(ranked: list[FilingAnalysis], model: str = MODEL) -> SectionResult:
    """Run the peer-comparison agent over the ranked companies."""
    user_content = (
        PEER_COMPARISON_INSTRUCTIONS
        + "PEER RANKING TABLE (sorted by composite score):\n\n"
        + build_ranking_table(ranked)
        + "\n\nPER-COMPANY ANALYSES (in ranking order):\n\n"
        + "\n\n".join(_peer_context(f) for f in ranked)
    )
    return _run_comparison_agent(
        user_content,
        model,
        "peers",
        "Peer Comparison",
        "🏁 Analyzing peer group...",
    )


def make_filing_analysis(
    filename: str,
    analysis: MultiAnalysisResult,
    report_path: str,
) -> FilingAnalysis:
    """Bundle an analyzed filing with its parsed scores and a period label."""
    from utils.report_writer import sanitize_filename

    scores = parse_scores(analysis.text)
    missing = [
        name for name, (value, _) in scores.numeric_dimensions().items() if value is None
    ]
    if scores.governance is None:
        missing.append("Governance")
    if missing:
        print(
            f"   ⚠️  Could not parse scores from '{filename}': {', '.join(missing)}. "
            "They will show as n/a in the comparison table."
        )
    return FilingAnalysis(
        label=sanitize_filename(filename),
        filename=filename,
        analysis=analysis,
        scores=scores,
        report_path=report_path,
    )
