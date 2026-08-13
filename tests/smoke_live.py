#!/usr/bin/env python3
"""Live contract test: does the real model emit what the parsers require?

Everything downstream of the specialists — comparison deltas, peer ranking,
sector statistics, watchlist alerts — reads scores out of the report text by
looking for `SCORE: X/N` and `RATING: X` lines. The stubbed suite proves the
parsers work; only this proves the prompts actually produce what they parse.

This costs real money, so it is NOT part of `unittest discover`. Run it
deliberately:

    python tests/smoke_live.py                    # ~4KB fixture filing
    python tests/smoke_live.py --file real.pdf    # a filing of your own

Exits 0 if the contract holds, 1 if any part of it is violated, and 2 if it
could not run (no credentials, no SDK).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample_filing.txt")

PASS = "✅"
FAIL = "❌"


def preflight() -> None:
    """Exit(2) with actionable guidance unless a live run is actually possible."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("❌ The `anthropic` package is not installed.")
        print("   pip install -r requirements.txt")
        sys.exit(2)

    from config import ANTHROPIC_API_KEY

    if not ANTHROPIC_API_KEY:
        print("❌ No API key found.")
        print("   Set ANTHROPIC_API_KEY, or put it in a .env file:")
        print('   echo "ANTHROPIC_API_KEY=sk-ant-..." > .env')
        sys.exit(2)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"   {PASS if ok else FAIL} {label}{f' — {detail}' if detail else ''}")
    return ok


def verify_contract(report: str) -> bool:
    """Check the report against everything the downstream parsers assume."""
    from utils.score_parser import GOVERNANCE_RATINGS, parse_scores, split_sections

    print("\n📋 Section structure")
    sections = split_sections(report)
    found = sorted(sections)
    ok = check("all ten numbered sections present", found == list(range(1, 11)),
               f"found {found}" if found != list(range(1, 11)) else "")

    print("\n📋 Score lines the parsers depend on")
    scores = parse_scores(report)
    dimensions = [
        ("Section 2 revenue score (SCORE: X/5)", scores.revenue_quality, 5),
        ("Section 3 margin score (SCORE: X/5)", scores.margins, 5),
        ("Section 4 balance sheet score (SCORE: X/5)", scores.balance_sheet, 5),
        ("Section 5 cash flow score (SCORE: X/5)", scores.cash_flow, 5),
        ("Section 6 moat score (SCORE: X/15)", scores.moat, 15),
    ]
    for label, value, maximum in dimensions:
        parsed = value is not None
        in_range = parsed and 0 <= value <= maximum
        ok &= check(label, in_range,
                    "not found" if not parsed else f"out of range: {value}")

    ok &= check(
        "Section 7 governance rating (RATING: X)",
        scores.governance in GOVERNANCE_RATINGS,
        f"got {scores.governance!r}" if scores.governance not in GOVERNANCE_RATINGS else "",
    )
    composite_ok = scores.composite is not None and 0 <= scores.composite <= 5
    ok &= check(
        "Section 9 composite (X.X / 5.0 on the COMPOSITE row)",
        composite_ok,
        "not found" if scores.composite is None else f"out of range: {scores.composite}",
    )

    if composite_ok and all(v is not None for _, v, _ in dimensions):
        # The composite is a weighted blend, so it should sit inside the range
        # of its inputs. Well outside means the synthesis agent invented it.
        parts = [scores.revenue_quality, scores.margins, scores.balance_sheet,
                 scores.cash_flow, scores.moat / 3]
        ok &= check(
            "composite is consistent with the dimension scores",
            min(parts) - 1 <= scores.composite <= max(parts) + 1,
            f"composite {scores.composite} vs dimensions {min(parts)}-{max(parts)}",
        )

    print("\n📋 Downstream consumers")
    from agents.comparator import build_ranking_table, make_filing_analysis
    from agents.orchestrator import MultiAnalysisResult
    from utils.sector_stats import build_stats_table

    analysis = MultiAnalysisResult(
        text=report, input_tokens=0, output_tokens=0, cache_creation_tokens=0,
        cache_read_tokens=0, model="live",
    )
    filing = make_filing_analysis("smoke.pdf", analysis, "output/smoke.md")
    ok &= check("ranking table renders without n/a cells",
                "n/a" not in build_ranking_table([filing]))
    ok &= check("sector statistics cover every dimension",
                "0/1" not in build_stats_table([filing]))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default=FIXTURE, help="Filing to analyze.")
    parser.add_argument("--model", default=None, help="Override the configured model.")
    args = parser.parse_args()

    preflight()

    from config import MODEL
    from agents.orchestrator import analyze_document_multi
    from utils.document_loader import load_document
    from utils.report_writer import write_report

    model = args.model or MODEL
    print("=" * 60)
    print("  🔬 Live contract smoke test")
    print("=" * 60)
    print(f"  Filing: {args.file}")
    print(f"  Model:  {model}")
    print("  This makes real API calls and costs real money.\n")

    doc = load_document(filepath=args.file)
    if doc is None:
        return 2

    started = time.time()
    analysis = analyze_document_multi(doc.text, model=model)
    elapsed = time.time() - started

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from main import estimate_cost

    cost = estimate_cost(analysis.input_tokens, analysis.output_tokens,
                         analysis.cache_creation_tokens, analysis.cache_read_tokens)
    report_path = write_report(
        analysis_text=analysis.text,
        source_filename=f"SMOKE_{doc.filename}",
        input_tokens=analysis.input_tokens,
        output_tokens=analysis.output_tokens,
        elapsed_seconds=elapsed,
        estimated_cost=cost,
        page_count=doc.page_count,
        original_chars=doc.original_chars,
        was_truncated=doc.was_truncated,
        model=analysis.model,
        cache_creation_tokens=analysis.cache_creation_tokens,
        cache_read_tokens=analysis.cache_read_tokens,
    )

    ok = verify_contract(analysis.text)

    print()
    print("─" * 60)
    print(f"  Report:     {report_path}")
    print(f"  Model:      {analysis.model}")
    print(f"  Est. cost:  ${cost:.4f}")
    print(f"  Elapsed:    {elapsed:.1f}s")
    print(f"  Contract:   {'HOLDS' if ok else 'VIOLATED'}")
    print("─" * 60)
    if not ok:
        print("\n  A violated contract means the aggregate features (comparison")
        print("  deltas, peer ranking, sector stats, watchlist alerts) will show")
        print("  n/a or wrong values. Read the report above, then tighten the")
        print("  offending prompt in prompts/section_prompts.py.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
