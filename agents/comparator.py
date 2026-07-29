"""Cross-period comparison: score deltas plus a trend-analysis agent.

Each filing is analyzed independently by the multi-agent pipeline; this module
compares the finished reports. The score table is built deterministically from
the explicit "SCORE: X/N" lines (see utils/score_parser.py) — only the trend
narrative comes from a model call.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from config import ANTHROPIC_API_KEY, MODEL, REQUEST_TIMEOUT, SYNTHESIS_MAX_TOKENS
from agents.orchestrator import MultiAnalysisResult, SectionResult, _call
from prompts.comparison import COMPARISON_INSTRUCTIONS
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


def analyze_trajectory(
    filings: list[FilingAnalysis], model: str = MODEL
) -> SectionResult:
    """Run the trend-analysis agent over the per-period analyses.

    Raises:
        SystemExit: If the API key is missing or the API call fails.
    """
    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        raise SystemExit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=REQUEST_TIMEOUT)

    user_content = (
        COMPARISON_INSTRUCTIONS
        + "SCORE TRAJECTORY TABLE:\n\n"
        + build_score_table(filings)
        + "\n\nPER-PERIOD ANALYSES (chronological, earliest first):\n\n"
        + "\n\n".join(_period_context(f) for f in filings)
    )

    print("📈 Analyzing cross-period trajectory...")
    try:
        result = _call(
            client,
            [{"type": "text", "text": BASE_ANALYST_CONTEXT}],
            user_content,
            model,
            SYNTHESIS_MAX_TOKENS,
            "trajectory",
            "Cross-Period Trajectory",
        )
    except anthropic.APIConnectionError as e:
        print(f"❌ API connection error: {e}")
        raise SystemExit(1)
    except anthropic.RateLimitError as e:
        print(f"❌ Rate limit exceeded: {e}")
        raise SystemExit(1)
    except anthropic.APIStatusError as e:
        print(f"❌ API error (status {e.status_code}): {e.message}")
        raise SystemExit(1)

    print(f"   ✅ {result.title} ({result.elapsed_seconds:.1f}s)")
    if result.truncated:
        print(
            f"   ⚠️  Trajectory analysis was cut off at the {SYNTHESIS_MAX_TOKENS:,}-token "
            "limit. Consider raising SYNTHESIS_MAX_TOKENS in config.py."
        )
    return result


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
