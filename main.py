#!/usr/bin/env python3
"""
Asia Equity Analyzer — CLI entry point.

Scans input/ for the most recent annual report (PDF or text),
runs it through a Claude-powered financial analyst agent, and
writes a structured investment analysis to output/.

Usage:
    python main.py
    python main.py --file path/to/report.pdf
    python main.py --model claude-opus-4-8 --max-tokens 12000
"""

import argparse
import sys
import time

from config import (
    INPUT_TOKEN_COST_PER_MILLION,
    MAX_TOKENS,
    MODEL,
    OUTPUT_TOKEN_COST_PER_MILLION,
    TEMPERATURE,
)
from utils.document_loader import load_document
from utils.report_writer import write_report
from agents.analyst import analyze_document


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
        help="Maximum number of output tokens.",
    )
    return parser.parse_args()


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate the API cost in USD."""
    input_cost = (input_tokens / 1_000_000) * INPUT_TOKEN_COST_PER_MILLION
    output_cost = (output_tokens / 1_000_000) * OUTPUT_TOKEN_COST_PER_MILLION
    return input_cost + output_cost


def main():
    args = parse_args()

    print()
    print("=" * 60)
    print("  📊 Asia Equity Analyzer")
    print("  Institutional-grade analysis for Asia ex-Japan equities")
    print("=" * 60)
    print()
    print(f"  Model:      {args.model}")
    print(f"  Max tokens: {args.max_tokens:,}")
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
    analysis = analyze_document(
        doc_info.text,
        model=args.model,
        max_tokens=args.max_tokens,
    )
    elapsed = time.time() - start_time
    print(f"   Analysis time: {elapsed:.1f}s")
    print()

    # Step 3: Write report
    cost = estimate_cost(analysis.input_tokens, analysis.output_tokens)
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
    print(f"  Output tokens:  {analysis.output_tokens:,}")
    print(f"  Total tokens:   {analysis.input_tokens + analysis.output_tokens:,}")
    print(f"  Est. cost:      ${cost:.4f}")
    print(f"  Time elapsed:   {elapsed:.1f}s")
    print("─" * 60)
    print()


if __name__ == "__main__":
    main()
