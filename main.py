#!/usr/bin/env python3
"""
Asia Equity Analyzer — CLI entry point.

Scans input/ for the most recent annual report (PDF or text),
runs it through a Claude-powered financial analyst agent, and
writes a structured investment analysis to output/.

By default the analysis runs as six parallel section-specialist agents plus a
synthesis agent. Use --single for the original single-agent mode, or --compare
to analyze multiple filings of the same company and track score deltas, or
--peers to rank multiple companies in the same sector, or --batch to screen a
whole directory. Add --watch to record scores to the persistent watchlist and
get alerted on material moves.

Usage:
    python main.py
    python main.py --file path/to/report.pdf
    python main.py --single --model claude-opus-4-8 --max-tokens 12000
    python main.py --compare fy2022.pdf fy2023.pdf fy2024.pdf
    python main.py --peers tencent.pdf alibaba.pdf netease.pdf
    python main.py --file tencent_fy2024.pdf --watch tencent
    python main.py --batch input/semis --batch-limit 10
    python main.py --show-watchlist
"""

import argparse
import os
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
    WATCHLIST_ALERT_THRESHOLD,
    WATCHLIST_PATH,
)
from utils.document_loader import find_documents, load_document
from utils.report_writer import (
    sanitize_filename,
    write_batch_report,
    write_comparison_report,
    write_peer_report,
    write_report,
)
from utils.score_parser import parse_scores
from utils.sector_stats import build_governance_table, build_stats_table
from utils.watchlist import (
    WatchlistError,
    format_watchlist_table,
    load_watchlist,
    record_analysis,
    save_watchlist,
)
from agents.analyst import analyze_document
from agents.comparator import (
    analyze_peers,
    analyze_sector,
    analyze_trajectory,
    build_ranking_table,
    build_score_table,
    make_filing_analysis,
    rank_peers,
)
from agents.orchestrator import analyze_document_multi

# A run of back-to-back analysis failures in batch mode means something
# systemic is wrong (credentials, quota, network) rather than one bad file.
MAX_CONSECUTIVE_FAILURES = 3


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
    parser.add_argument(
        "--batch",
        metavar="DIR",
        default=None,
        help="Analyze every .pdf/.txt filing in a directory (one company each) "
        "and produce a ranked sector summary with aggregate statistics.",
    )
    parser.add_argument(
        "--batch-limit",
        type=int,
        metavar="N",
        default=None,
        help="Cap how many filings a --batch run analyzes. Dropped filings are "
        "listed in the report, never silently omitted.",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const="",
        metavar="COMPANY",
        default=None,
        help="Record this run's scores to the watchlist and alert on material "
        "moves. Pass a company name to keep filings with different filenames "
        "under one entry; defaults to the filename.",
    )
    parser.add_argument(
        "--show-watchlist",
        action="store_true",
        help="Print the watchlist and exit. Makes no API calls.",
    )
    parser.add_argument(
        "--watchlist-path",
        metavar="PATH",
        default=WATCHLIST_PATH,
        help="Path to the watchlist JSON store.",
    )
    parser.add_argument(
        "--alert-threshold",
        type=float,
        metavar="POINTS",
        default=WATCHLIST_ALERT_THRESHOLD,
        help="Composite move (out of 5.0) that triggers a watchlist alert.",
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
    group_modes = [
        name
        for name, value in (
            ("--compare", args.compare), ("--peers", args.peers), ("--batch", args.batch)
        )
        if value
    ]
    if len(group_modes) > 1:
        parser.error(f"{' and '.join(group_modes)} are mutually exclusive.")
    if args.batch:
        if args.single:
            parser.error("--batch uses the multi-agent pipeline; drop --single.")
        if args.file:
            parser.error("--batch scans a directory; drop --file.")
    if args.batch_limit is not None:
        if not args.batch:
            parser.error("--batch-limit only applies to --batch.")
        if args.batch_limit < 2:
            parser.error("--batch-limit must be at least 2 to summarize a sector.")
    if args.show_watchlist and (
        args.compare
        or args.peers
        or args.batch
        or args.file
        or args.single
        or args.watch is not None
    ):
        parser.error("--show-watchlist prints the watchlist and exits; run it on its own.")
    if args.watch is not None:
        if args.single:
            parser.error(
                "--watch needs the multi-agent pipeline: only its prompts emit the "
                "explicit SCORE lines the watchlist records. Drop --single."
            )
        if args.watch and (args.peers or args.batch):
            flag = "--peers" if args.peers else "--batch"
            parser.error(
                f"{flag} analyzes different companies; each is named from its "
                "filename. Use --watch without a company name."
            )
    if args.alert_threshold < 0:
        parser.error("--alert-threshold must be zero or positive.")
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


def _multi_agent_stats(analysis) -> list:
    """Per-agent usage rows for the report's Agent Breakdown table."""
    return [
        {
            "title": s.title,
            "input_tokens": s.input_tokens + s.cache_creation_tokens + s.cache_read_tokens,
            "output_tokens": s.output_tokens,
            "elapsed_seconds": s.elapsed_seconds,
        }
        for s in analysis.sections
    ]


def _write_filing_report(doc_info, analysis, elapsed_seconds: float) -> str:
    """Write one multi-agent filing report and return its path."""
    cost = estimate_cost(
        analysis.input_tokens,
        analysis.output_tokens,
        analysis.cache_creation_tokens,
        analysis.cache_read_tokens,
    )
    return write_report(
        analysis_text=analysis.text,
        source_filename=doc_info.filename,
        input_tokens=analysis.input_tokens,
        output_tokens=analysis.output_tokens,
        elapsed_seconds=elapsed_seconds,
        estimated_cost=cost,
        page_count=doc_info.page_count,
        original_chars=doc_info.original_chars,
        was_truncated=doc_info.was_truncated,
        model=analysis.model,
        pricing_uncertain=analysis.model != MODEL,
        cache_creation_tokens=analysis.cache_creation_tokens,
        cache_read_tokens=analysis.cache_read_tokens,
        agent_stats=_multi_agent_stats(analysis),
    )


def _analyze_filings(paths: list, model: str, skip_failures: bool = False) -> tuple:
    """Run the multi-agent pipeline on each filing and write its report.

    All filings are loaded and validated up front, so a bad path fails fast
    instead of surfacing only after earlier filings have burned API spend.

    With `skip_failures` (batch mode), a filing that can't be loaded or
    analyzed is recorded and the run continues — one unreadable PDF shouldn't
    discard a directory's worth of work. A run of consecutive analysis
    failures indicates a systemic problem (bad key, exhausted quota) rather
    than bad documents, so the batch stops rather than burning the remainder.

    Returns:
        (filings, skipped) where skipped is a list of (filename, reason).
    """
    documents = []
    skipped = []
    for path in paths:
        doc_info = load_document(filepath=path)
        if doc_info is None:
            if not skip_failures:
                sys.exit(1)
            skipped.append((os.path.basename(path), "could not be loaded"))
            continue
        documents.append(doc_info)
    print()

    filings = []
    consecutive_failures = 0
    for index, doc_info in enumerate(documents, start=1):
        print(f"📂 Filing {index}/{len(documents)}: {doc_info.filename}")
        filing_start = time.time()
        try:
            analysis = analyze_document_multi(doc_info.text, model=model)
        except SystemExit:
            # The pipeline exits on API failure; in batch mode that's one
            # filing lost, not the whole run.
            if not skip_failures:
                raise
            print(f"   ⏭️  Skipping '{doc_info.filename}' after the error above.\n")
            skipped.append((doc_info.filename, "analysis failed"))
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                remaining = documents[index:]
                print(
                    f"❌ Stopping the batch after {consecutive_failures} consecutive "
                    f"failures — this looks systemic, not document-specific."
                )
                if remaining:
                    print(f"   {len(remaining)} filing(s) were not attempted.")
                skipped.extend(
                    (d.filename, "not attempted (batch stopped early)") for d in remaining
                )
                break
            continue

        consecutive_failures = 0
        filing_elapsed = time.time() - filing_start
        report_path = _write_filing_report(doc_info, analysis, filing_elapsed)
        filings.append(make_filing_analysis(doc_info.filename, analysis, report_path))
        print()
    return filings, skipped


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


def _record_watchlist(args, records: list) -> None:
    """Append records to the watchlist and print any alerts.

    `records` is a list of (company, source_file, report_path, model, scores).
    Watchlist problems are reported but never abort the run — the analysis
    reports are already written and the API spend is already incurred.
    """
    try:
        data = load_watchlist(args.watchlist_path)
    except WatchlistError as e:
        print(f"⚠️  Watchlist not updated: {e}")
        print("   Fix or move the file and re-run; the reports above are unaffected.")
        return

    alerts = []
    for company, source_file, report_path, model, scores in records:
        alerts.extend(
            record_analysis(
                data,
                company=company,
                source_file=source_file,
                report_path=report_path,
                model=model,
                scores=scores,
                threshold=args.alert_threshold,
            )
        )

    try:
        save_watchlist(data, args.watchlist_path)
    except OSError as e:
        print(f"⚠️  Could not write watchlist '{args.watchlist_path}': {e}")
        return

    names = ", ".join(company for company, *_ in records)
    print(f"👁️  Watchlist updated ({args.watchlist_path}): {names}")
    if alerts:
        print()
        print(f"🔔 Watchlist alerts (threshold ±{args.alert_threshold:g}):")
        for alert in alerts:
            marker = "⚠️ " if alert.adverse else "✅"
            print(f"   {marker} {alert.company}: {alert.message}")
    print()


def run_show_watchlist(args) -> None:
    """Print the watchlist table and exit without making any API calls."""
    try:
        data = load_watchlist(args.watchlist_path)
    except WatchlistError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  👁️  Asia Equity Analyzer — Watchlist")
    print(f"  {args.watchlist_path}")
    print("=" * 60)
    print()
    print(format_watchlist_table(data, threshold=args.alert_threshold))
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
    filings, _ = _analyze_filings(args.compare, args.model)
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
    if args.watch is not None:
        # Every filing is the same company, so they share one watchlist entry
        # and are recorded in the chronological order they were given.
        company = args.watch or sanitize_filename(filings[0].filename)
        _record_watchlist(
            args,
            [
                (company, f.filename, f.report_path, f.analysis.model, f.scores)
                for f in filings
            ],
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
    filings, _ = _analyze_filings(args.peers, args.model)
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
    if args.watch is not None:
        # Each filing is a different company, so each gets its own entry named
        # from its filename (--watch takes no name in peers mode).
        _record_watchlist(
            args,
            [
                (f.label, f.filename, f.report_path, f.analysis.model, f.scores)
                for f in filings
            ],
        )


def run_batch(args) -> None:
    """Analyze every filing in a directory, then summarize the sector."""
    paths = find_documents(args.batch)
    if paths is None:
        sys.exit(1)

    dropped = []
    if args.batch_limit is not None and len(paths) > args.batch_limit:
        dropped = [
            (os.path.basename(p), f"dropped by --batch-limit {args.batch_limit}")
            for p in paths[args.batch_limit:]
        ]
        paths = paths[: args.batch_limit]

    print()
    print("=" * 60)
    print("  📊 Asia Equity Analyzer — Sector Batch")
    print(f"  {len(paths)} filings from {args.batch}")
    print("=" * 60)
    print()
    print(f"  Model:      {args.model}")
    print("  Mode:       6 specialists + synthesis, per company")
    print()
    if dropped:
        print(
            f"⚠️  {len(dropped)} filing(s) beyond --batch-limit {args.batch_limit} "
            "will not be analyzed; they are listed in the report."
        )
        print()

    start_time = time.time()
    filings, skipped = _analyze_filings(paths, args.model, skip_failures=True)
    skipped = skipped + dropped

    if len(filings) < 2:
        print(
            f"❌ Only {len(filings)} filing(s) analyzed successfully — a sector "
            "summary needs at least 2."
        )
        for name, reason in skipped:
            print(f"   • {name}: {reason}")
        if filings:
            print(f"\n   The individual report was still written: {filings[0].report_path}")
        sys.exit(1)

    ranked = rank_peers(filings)
    sector = analyze_sector(ranked, model=args.model, skipped=skipped)
    elapsed = time.time() - start_time

    usage = _aggregate_usage(filings, sector)
    total_cost = estimate_cost(*usage)
    pricing_uncertain = _warn_pricing(sector)

    output_path = write_batch_report(
        ranking_table=build_ranking_table(ranked),
        stats_table=build_stats_table(ranked),
        governance_table=build_governance_table(ranked),
        sector_text=sector.text,
        filings=ranked,
        skipped=skipped,
        model=sector.model,
        input_tokens=usage[0],
        output_tokens=usage[1],
        cache_creation_tokens=usage[2],
        cache_read_tokens=usage[3],
        elapsed_seconds=elapsed,
        estimated_cost=total_cost,
        pricing_uncertain=pricing_uncertain,
    )
    _print_group_summary(
        "SECTOR SUMMARY", "Company report:", ranked, output_path,
        sector.model, usage, total_cost, elapsed,
    )
    if skipped:
        print(f"  ⚠️  {len(skipped)} filing(s) not included:")
        for name, reason in skipped:
            print(f"     • {name}: {reason}")
        print()

    if args.watch is not None:
        _record_watchlist(
            args,
            [
                (f.label, f.filename, f.report_path, f.analysis.model, f.scores)
                for f in filings
            ],
        )


def main():
    args = parse_args()

    if args.show_watchlist:
        run_show_watchlist(args)
        return
    if args.compare:
        run_comparison(args)
        return
    if args.peers:
        run_peers(args)
        return
    if args.batch:
        run_batch(args)
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
        agent_stats = _multi_agent_stats(analysis)
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

    # Step 5: Watchlist
    if args.watch is not None:
        company = args.watch or sanitize_filename(doc_info.filename)
        _record_watchlist(
            args,
            [(
                company,
                doc_info.filename,
                output_path,
                analysis.model,
                parse_scores(analysis.text),
            )],
        )


if __name__ == "__main__":
    main()
