#!/usr/bin/env python3
"""
Asia Equity Analyzer — CLI entry point.

Scans input/ for the most recent annual report (PDF or text),
runs it through a Claude-powered financial analyst agent, and
writes a structured investment analysis to output/.

By default the analysis runs as six parallel section-specialist agents plus a
synthesis agent. Use --single for the original single-agent mode, or --compare
to analyze multiple filings of the same company and track score deltas, or
--peers to rank multiple companies in the same sector.

Usage:
    python main.py
    python main.py --file path/to/report.pdf
    python main.py --single --model claude-opus-4-8 --max-tokens 12000
    python main.py --compare fy2022.pdf fy2023.pdf fy2024.pdf
    python main.py --peers tencent.pdf alibaba.pdf netease.pdf
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
from utils.report_writer import write_comparison_report, write_peer_report, write_report
from agents.analyst import analyze_document
from agents.comparator import (
    analyze_peers,
    analyze_trajectory,
    build_ranking_table,
    build_score_table,
    make_filing_analysis,
    rank_peers,
)
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
    parser.add_argument(
        "--compare",
        nargs="+",
        metavar="PATH",
        default=None,
        help="Analyze two or more filings of the same company (in chronological "
        "order, earliest first) and produce a cross-period comparison report.",
    )
    parser.add_argument(
        "--peers",
        nargs="+",
        metavar="PATH",
        default=None,
        help="Analyze two or more companies in the same sector (one filing each) "
        "and produce a ranked peer-comparison report.",
    )
    args = parser.parse_args()
    for flag, paths in (("--compare", args.compare), ("--peers", args.peers)):
        if paths is None:
            continue
        if len(paths) < 2:
            parser.error(f"{flag} requires at least two filings.")
        if args.single:
            parser.error(f"{flag} uses the multi-agent pipeline; drop --single.")
        if args.file:
            parser.error(f"{flag} takes its file list directly; drop --file.")
    if args.compare and args.peers:
        parser.error("--compare and --peers are mutually exclusive.")
    return args


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


def _analyze_filings(paths: list, model: str) -> list:
    """Run the multi-agent pipeline on each filing and write its report."""
    filings = []
    for index, path in enumerate(paths, start=1):
        print(f"📂 Filing {index}/{len(paths)}")
        doc_info = load_document(filepath=path)
        if doc_info is None:
            sys.exit(1)

        filing_start = time.time()
        analysis = analyze_document_multi(doc_info.text, model=model)
        filing_elapsed = time.time() - filing_start

        cost = estimate_cost(
            analysis.input_tokens,
            analysis.output_tokens,
            analysis.cache_creation_tokens,
            analysis.cache_read_tokens,
        )
        report_path = write_report(
            analysis_text=analysis.text,
            source_filename=doc_info.filename,
            input_tokens=analysis.input_tokens,
            output_tokens=analysis.output_tokens,
            elapsed_seconds=filing_elapsed,
            estimated_cost=cost,
            page_count=doc_info.page_count,
            original_chars=doc_info.original_chars,
            was_truncated=doc_info.was_truncated,
            model=analysis.model,
            pricing_uncertain=analysis.model != MODEL,
            cache_creation_tokens=analysis.cache_creation_tokens,
            cache_read_tokens=analysis.cache_read_tokens,
            agent_stats=[
                {
                    "title": s.title,
                    "input_tokens": s.input_tokens + s.cache_creation_tokens + s.cache_read_tokens,
                    "output_tokens": s.output_tokens,
                    "elapsed_seconds": s.elapsed_seconds,
                }
                for s in analysis.sections
            ],
        )
        filings.append(make_filing_analysis(doc_info.filename, analysis, report_path))
        print()
    return filings


def _aggregate_usage(filings: list, final_call) -> tuple:
    """Sum token usage across all filings plus the final comparison call."""
    input_tokens = sum(f.analysis.input_tokens for f in filings) + final_call.input_tokens
    output_tokens = sum(f.analysis.output_tokens for f in filings) + final_call.output_tokens
    cache_creation = (
        sum(f.analysis.cache_creation_tokens for f in filings)
        + final_call.cache_creation_tokens
    )
    cache_read = (
        sum(f.analysis.cache_read_tokens for f in filings) + final_call.cache_read_tokens
    )
    return input_tokens, output_tokens, cache_creation, cache_read


def _print_group_summary(
    heading: str,
    row_label: str,
    filings: list,
    output_path: str,
    model: str,
    usage: tuple,
    total_cost: float,
    elapsed: float,
) -> None:
    input_tokens, output_tokens, cache_creation, cache_read = usage
    print()
    print("─" * 60)
    print(f"  📋 {heading}")
    print("─" * 60)
    for f in filings:
        print(f"  {row_label}  {f.report_path}")
    print(f"  Combined:       {output_path}")
    print(f"  Model:          {model}")
    total = input_tokens + cache_creation + cache_read + output_tokens
    print(f"  Total tokens:   {total:,}")
    print(f"  Est. cost:      ${total_cost:.4f}")
    print(f"  Time elapsed:   {elapsed:.1f}s")
    print("─" * 60)
    print()


def _warn_pricing(final_call) -> bool:
    pricing_uncertain = final_call.model != MODEL
    if pricing_uncertain:
        print(
            f"⚠️  Cost estimate uses {MODEL} pricing but the responses came from "
            f"{final_call.model}. The estimate may not reflect actual billed cost."
        )
    return pricing_uncertain


def run_comparison(args) -> None:
    """Analyze each filing with the multi-agent pipeline, then compare periods."""
    print()
    print("=" * 60)
    print("  📊 Asia Equity Analyzer — Cross-Period Comparison")
    print(f"  {len(args.compare)} filings, chronological order as given")
    print("=" * 60)
    print()
    print(f"  Model:      {args.model}")
    print("  Mode:       6 specialists + synthesis, per filing")
    print()

    start_time = time.time()
    filings = _analyze_filings(args.compare, args.model)
    trajectory = analyze_trajectory(filings, model=args.model)
    elapsed = time.time() - start_time

    usage = _aggregate_usage(filings, trajectory)
    total_cost = estimate_cost(*usage)
    pricing_uncertain = _warn_pricing(trajectory)

    output_path = write_comparison_report(
        score_table=build_score_table(filings),
        trajectory_text=trajectory.text,
        filings=filings,
        model=trajectory.model,
        input_tokens=usage[0],
        output_tokens=usage[1],
        cache_creation_tokens=usage[2],
        cache_read_tokens=usage[3],
        elapsed_seconds=elapsed,
        estimated_cost=total_cost,
        pricing_uncertain=pricing_uncertain,
    )
    _print_group_summary(
        "COMPARISON SUMMARY", "Period report:", filings, output_path,
        trajectory.model, usage, total_cost, elapsed,
    )


def run_peers(args) -> None:
    """Analyze each company with the multi-agent pipeline, then rank the group."""
    print()
    print("=" * 60)
    print("  📊 Asia Equity Analyzer — Peer Comparison")
    print(f"  {len(args.peers)} companies, ranked by composite score")
    print("=" * 60)
    print()
    print(f"  Model:      {args.model}")
    print("  Mode:       6 specialists + synthesis, per company")
    print()

    start_time = time.time()
    filings = _analyze_filings(args.peers, args.model)
    ranked = rank_peers(filings)
    peer_analysis = analyze_peers(ranked, model=args.model)
    elapsed = time.time() - start_time

    usage = _aggregate_usage(filings, peer_analysis)
    total_cost = estimate_cost(*usage)
    pricing_uncertain = _warn_pricing(peer_analysis)

    output_path = write_peer_report(
        ranking_table=build_ranking_table(ranked),
        peer_text=peer_analysis.text,
        filings=ranked,
        model=peer_analysis.model,
        input_tokens=usage[0],
        output_tokens=usage[1],
        cache_creation_tokens=usage[2],
        cache_read_tokens=usage[3],
        elapsed_seconds=elapsed,
        estimated_cost=total_cost,
        pricing_uncertain=pricing_uncertain,
    )
    _print_group_summary(
        "PEER SUMMARY", "Company report:", ranked, output_path,
        peer_analysis.model, usage, total_cost, elapsed,
    )


def main():
    args = parse_args()

    if args.compare:
        run_comparison(args)
        return
    if args.peers:
        run_peers(args)
        return

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
