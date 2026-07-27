#!/usr/bin/env python3
"""
Asia Equity Analyzer — CLI entry point.

Scans input/ for the most recent annual report (PDF or text),
runs it through a Claude-powered financial analyst agent, and
writes a structured investment analysis to output/.

By default the analysis runs as six parallel section-specialist agents plus a
synthesis agent. Use --single for the original single-agent mode.

Usage:
    python main.py
    python main.py --file path/to/report.pdf
    python main.py --single --model claude-opus-4-8 --max-tokens 12000
"""

import argparse
import sys
import time

from config import (
    CACHE_READ_COST_PER_MILLION,
    CACHE_WRITE_COST_PER_MILLION,
    INPUT_TOKEN_COST_PER_MILLION,
    MAX_TOKENS,
    MODEL,
    OUTPUT_TOKEN_COST_PER_MILLION,
    TEMPERATURE,
)
from utils.document_loader import load_document
from utils.report_writer import write_report
from agents.analyst import analyze_document
from agents.orchestrator import analyze_document_multi


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Asia Equity Analyzer — institutional-grade analysis for Asia ex-Japan equities",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-f", "--file",
        metavar="PATH",
        default=None,
        help="Path to a PDF or .txt annual report. Defaults to the most recent file in input/.",
    )
    parser.add_argument(
        "-m", "--model",
        metavar="MODEL_ID",
        default=MODEL,
        help="Claude model ID to use.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        metavar="N",
        default=MAX_TOKENS,
        help="Maximum number of output tokens (single-agent mode only).",
    )
    parser.add_argument(
        "--single",
        action="store_true",
        help="Use the original single-agent analysis instead of the six-agent pipeline.",
    )
    return parser.parse_args()


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate the API cost in USD, including prompt-cache writes and reads."""
    return (
        (input_tokens / 1_000_000) * INPUT_TOKEN_COST_PER_MILLION
        + (output_tokens / 1_000_000) * OUTPUT_TOKEN_COST_PER_MILLION
        + (cache_creation_tokens / 1_000_000) * CACHE_WRITE_COST_PER_MILLION
        + (cache_read_tokens / 1_000_000) * CACHE_READ_COST_PER_MILLION
    )


def main():
    args = parse_args()

    print()
    print("=" * 60)
    print("  📊 Asia Equity Analyzer")
    print("  Institutional-grade analysis for Asia ex-Japan equities")
    print("=" * 60)
    print()
    mode = "single agent" if args.single else "6 specialists + synthesis"
    print(f"  Model:      {args.model}")
    print(f"  Mode:       {mode}")
    print(f"  Temp:       {TEMPERATURE}")
    print()

    # Step 1: Load document
    doc_info = load_document(filepath=args.file)
    if doc_info is None:
        print("\n💡 Drop a PDF or .txt annual report into the input/ folder and try again.")
        sys.exit(1)
    print()

    # Step 2: Analyze
    start_time = time.time()
    if args.single:
        analysis = analyze_document(
            doc_info.text,
            model=args.model,
            max_tokens=args.max_tokens,
        )
        cache_creation = cache_read = 0
        agent_stats = None
    else:
        analysis = analyze_document_multi(doc_info.text, model=args.model)
        cache_creation = analysis.cache_creation_tokens
        cache_read = analysis.cache_read_tokens
        agent_stats = [
            {
                "title": s.title,
                "input_tokens": s.input_tokens + s.cache_creation_tokens + s.cache_read_tokens,
                "output_tokens": s.output_tokens,
                "elapsed_seconds": s.elapsed_seconds,
            }
            for s in analysis.sections
        ]
    elapsed = time.time() - start_time
    print(f"   Analysis time: {elapsed:.1f}s")
    print()

    # Step 3: Write report
    cost = estimate_cost(
        analysis.input_tokens, analysis.output_tokens, cache_creation, cache_read
    )
    pricing_uncertain = analysis.model != MODEL
    if pricing_uncertain:
        print(
            f"⚠️  Cost estimate uses {MODEL} pricing (${INPUT_TOKEN_COST_PER_MILLION}/M in, "
            f"${OUTPUT_TOKEN_COST_PER_MILLION}/M out) but the response came from {analysis.model}. "
            "The estimate below may not reflect actual billed cost."
        )
    output_path = write_report(
        analysis_text=analysis.text,
        source_filename=doc_info.filename,
        input_tokens=analysis.input_tokens,
        output_tokens=analysis.output_tokens,
        elapsed_seconds=elapsed,
        estimated_cost=cost,
        page_count=doc_info.page_count,
        original_chars=doc_info.original_chars,
        was_truncated=doc_info.was_truncated,
        model=analysis.model,
        pricing_uncertain=pricing_uncertain,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        agent_stats=agent_stats,
    )

    # Step 4: Summary
    print()
    print("─" * 60)
    print("  📋 SUMMARY")
    print("─" * 60)
    print(f"  Source file:    {doc_info.filename}")
    if doc_info.page_count:
        print(f"  Pages:          {doc_info.page_count:,}")
    if doc_info.was_truncated:
        print(f"  Characters:     {len(doc_info.text):,} of {doc_info.original_chars:,} (truncated)")
    else:
        print(f"  Characters:     {doc_info.original_chars:,}")
    print(f"  Output file:    {output_path}")
    print(f"  Model:          {analysis.model}")
    print(f"  Input tokens:   {analysis.input_tokens:,}")
    if cache_creation or cache_read:
        print(f"  Cache written:  {cache_creation:,}")
        print(f"  Cache read:     {cache_read:,}")
    print(f"  Output tokens:  {analysis.output_tokens:,}")
    total = analysis.input_tokens + cache_creation + cache_read + analysis.output_tokens
    print(f"  Total tokens:   {total:,}")
    print(f"  Est. cost:      ${cost:.4f}")
    print(f"  Time elapsed:   {elapsed:.1f}s")
    print("─" * 60)
    print()


if __name__ == "__main__":
    main()
