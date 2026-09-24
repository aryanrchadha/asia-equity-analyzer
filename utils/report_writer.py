"""Report writer: saves analysis output as a timestamped markdown file."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

from config import MAX_DOCUMENT_CHARS, MODEL, OUTPUT_DIR


def _plan_usage() -> bool:
    """True when the run drew on a Claude plan, so the dollar figure wasn't billed."""
    from agents.orchestrator import uses_plan_usage  # lazy: agents import utils

    return uses_plan_usage()


def _cost_banner(cost: float) -> str:
    if _plan_usage():
        return f"API-equivalent: ${cost:.4f} (Claude plan)"
    return f"Cost: ${cost:.4f}"


# Leaves room for the prefix ("comparison_"), timestamp, and extension inside
# the 255-byte filename limit common to ext4, APFS, and NTFS.
MAX_NAME_BYTES = 120


def _truncate_to_bytes(name: str, limit: int) -> str:
    """Trim to `limit` UTF-8 bytes without splitting a character."""
    encoded = name.encode("utf-8")
    if len(encoded) <= limit:
        return name
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip("_")


def sanitize_filename(name: str) -> str:
    """Convert a source filename into a clean company name for the output file.

    Word characters are kept as-is rather than stripped to ASCII. This tool
    covers Greater China, Korea and Southeast Asia, so CJK filenames are
    routine — and reducing them to their digits made every "<company>2024.pdf"
    collapse to "2024", which silently overwrote reports and merged unrelated
    companies into one watchlist history.
    """
    # Remove extension
    name = os.path.splitext(name)[0]
    # Replace anything that isn't a word character (unicode-aware) or digit
    name = re.sub(r"[^\w]+", "_", name, flags=re.UNICODE)
    # Collapse multiple underscores and strip leading/trailing
    name = re.sub(r"_+", "_", name).strip("_")
    # Keep the whole path within the filesystem's limit; an over-long name
    # would otherwise raise OSError after the analysis had already been paid for.
    return _truncate_to_bytes(name.lower(), MAX_NAME_BYTES) or "report"


def _escape_table_cell(text: str) -> str:
    """Escape pipe characters so they don't split a Markdown table row into extra cells."""
    return text.replace("|", "\\|")


def _safe_code_span(text: str) -> str:
    """Wrap text in a CommonMark-safe inline code span.

    Backslash escapes are not processed inside code spans, so a literal
    backtick in `text` can't just be escaped — it would prematurely close
    the span. Per the CommonMark spec, the fix is to use a longer run of
    backticks as the delimiter than any backtick run inside the content,
    padding with a space if the content starts or ends with a backtick.
    """
    if not text:
        return "``"
    runs = re.findall(r"`+", text)
    fence = "`" * ((max(len(r) for r in runs) if runs else 0) + 1)
    pad = " " if text[0] == "`" or text[-1] == "`" else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def write_report(
    analysis_text: str,
    source_filename: str,
    input_tokens: int,
    output_tokens: int,
    elapsed_seconds: float,
    estimated_cost: float,
    page_count: int = 0,
    original_chars: int = 0,
    was_truncated: bool = False,
    model: str = MODEL,
    pricing_uncertain: bool = False,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    agent_stats: list | None = None,
    incomplete_sections: list | None = None,
) -> str:
    """Write the analysis to a timestamped markdown file in the output directory.

    Args:
        analysis_text: The full analysis markdown from the agent.
        source_filename: Original input filename.
        input_tokens: Number of input tokens used.
        output_tokens: Number of output tokens generated.
        elapsed_seconds: Wall-clock time for the API call.
        estimated_cost: Estimated USD cost of the API call.
        page_count: Number of pages in the source PDF (0 for text files).
        original_chars: Total characters before truncation.
        was_truncated: Whether the document was truncated before sending.
        model: The model ID that produced the response.
        pricing_uncertain: True if `model` differs from the model the cost
            constants in config.py are calibrated for, meaning the cost
            estimate may not reflect actual billed cost.
        cache_creation_tokens: Prompt-cache write tokens (multi-agent mode).
        cache_read_tokens: Prompt-cache read tokens (multi-agent mode).
        agent_stats: Optional per-agent usage dicts (title, input_tokens,
            output_tokens, elapsed_seconds) for the multi-agent pipeline.
        incomplete_sections: (agent title, reason) for agents whose output
            was truncated or declined. Rendered as a warning at the top of
            the report — the console message alone is gone once the run
            ends, and the saved report must not look finished when it isn't.

    Returns:
        Path to the written report file.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now()
    timestamp_str = timestamp.strftime("%Y-%m-%d_%H%M%S")
    date_display = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    company_name = sanitize_filename(source_filename)
    output_filename = f"{company_name}_{timestamp_str}.md"
    output_path = os.path.join(OUTPUT_DIR, output_filename)

    total_tokens = input_tokens + cache_creation_tokens + cache_read_tokens + output_tokens

    # Build character / page detail line
    chars_sent = min(original_chars, MAX_DOCUMENT_CHARS) if was_truncated else original_chars
    if was_truncated:
        chars_detail = f"{chars_sent:,} of {original_chars:,} *(truncated)*"
    else:
        chars_detail = f"{original_chars:,}"

    # Markdown-safe renderings: table cells need pipes escaped (or the row
    # splits into extra columns) and both contexts need CommonMark-safe code
    # spans (a raw backtick in the filename/model would otherwise prematurely
    # close a naive `{...}` wrapper).
    source_cell = _safe_code_span(_escape_table_cell(source_filename))
    model_cell = _safe_code_span(_escape_table_cell(model))
    source_span = _safe_code_span(source_filename)
    model_span = _safe_code_span(model)
    default_model_span = _safe_code_span(MODEL)

    pages_row = f"| Pages | {page_count:,} |\n" if page_count else ""
    cache_rows = ""
    if cache_creation_tokens or cache_read_tokens:
        cache_rows = (
            f"| Cache Write Tokens | {cache_creation_tokens:,} |\n"
            f"| Cache Read Tokens | {cache_read_tokens:,} |\n"
        )
    cost_label = (
        "API-Equivalent Cost (not billed — ran on your Claude plan)"
        if _plan_usage()
        else "Estimated API Cost"
    ) + (" ⚠️" if pricing_uncertain else "")
    cost_footnote = (
        f"\n*⚠️ No published pricing on file for {model_span}; the estimate uses the "
        f"config constants (calibrated for {default_model_span}) and may not match the bill.*\n"
        if pricing_uncertain
        else ""
    )

    incomplete_block = ""
    if incomplete_sections:
        items = "".join(
            f"> - **{_escape_table_cell(title)}** — {reason}\n"
            for title, reason in incomplete_sections
        )
        incomplete_block = (
            "> ⚠️ **This analysis is incomplete.** Scores from the affected "
            "sections may be missing, and any comparison built on this report "
            "will show them as n/a.\n>\n" + items + "\n"
        )

    stats_table = f"""\
## Processing Statistics

| Parameter | Value |
|-----------|-------|
| Source File | {source_cell} |
{pages_row}| Characters Sent | {chars_detail} |
| Model | {model_cell} |
| Input Tokens | {input_tokens:,} |
{cache_rows}| Output Tokens | {output_tokens:,} |
| Total Tokens | {total_tokens:,} |
| {cost_label} | ${estimated_cost:.4f} |
| Analysis Time | {elapsed_seconds:.1f}s |
{cost_footnote}
---

"""

    if agent_stats:
        agent_rows = "".join(
            f"| {_escape_table_cell(a['title'])} | {a['input_tokens']:,} "
            f"| {a['output_tokens']:,} | {a['elapsed_seconds']:.1f}s |\n"
            for a in agent_stats
        )
        stats_table += f"""\
## Agent Breakdown

| Agent | Input Tokens | Output Tokens | Time |
|-------|--------------|---------------|------|
{agent_rows}
---

"""

    yaml_header = f"""\
---
source_file: {json.dumps(source_filename, ensure_ascii=False)}
analysis_date: {date_display}
model: {json.dumps(model, ensure_ascii=False)}
input_tokens: {input_tokens}
cache_creation_tokens: {cache_creation_tokens}
cache_read_tokens: {cache_read_tokens}
output_tokens: {output_tokens}
total_tokens: {total_tokens}
agent_count: {len(agent_stats) if agent_stats else 1}
page_count: {page_count}
original_chars: {original_chars}
was_truncated: {str(was_truncated).lower()}
elapsed_seconds: {elapsed_seconds:.1f}
estimated_cost_usd: {estimated_cost:.4f}
billed_to: {"claude-plan" if _plan_usage() else "api"}
pricing_uncertain: {str(pricing_uncertain).lower()}
incomplete_sections: {json.dumps([title for title, _ in (incomplete_sections or [])], ensure_ascii=False)}
---

"""

    banner = (
        f"> **Asia Equity Analyzer** — Generated {date_display}\n"
        f"> Source: {source_span} | Model: {model_span} | "
        f"Tokens: {total_tokens:,} | {_cost_banner(estimated_cost)}\n\n---\n\n"
    )

    full_report = yaml_header + banner + incomplete_block + stats_table + analysis_text

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_report)

    print(f"📝 Report written: {output_path}")
    return output_path


def _write_multi_filing_report(
    report_type: str,
    filename_prefix: str,
    banner_title: str,
    filings_heading: str,
    table_heading: str,
    score_table: str,
    narrative_text: str,
    filings: list,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    elapsed_seconds: float,
    estimated_cost: float,
    pricing_uncertain: bool,
    unit_label: str,
    unit_label_plural: str,
    extra_sections: tuple = (),
    extra_yaml: dict | None = None,
) -> str:
    """Shared writer for reports that aggregate several analyzed filings.

    `extra_sections` is a sequence of (heading, markdown) pairs rendered after
    the main table; `extra_yaml` adds scalar fields to the front matter.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now()
    timestamp_str = timestamp.strftime("%Y-%m-%d_%H%M%S")
    date_display = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    base = sanitize_filename(filings[0].filename)
    output_path = os.path.join(OUTPUT_DIR, f"{filename_prefix}_{base}_{timestamp_str}.md")

    total_tokens = input_tokens + cache_creation_tokens + cache_read_tokens + output_tokens
    source_files = [f.filename for f in filings]

    model_span = _safe_code_span(model)
    default_model_span = _safe_code_span(MODEL)
    cost_footnote = (
        f"\n*⚠️ No published pricing on file for {model_span}; the estimate uses the "
        f"config constants (calibrated for {default_model_span}) and may not match the bill.*\n"
        if pricing_uncertain
        else ""
    )

    filing_rows = "".join(
        f"| {i + 1} | {_safe_code_span(_escape_table_cell(f.filename))} "
        f"| {_safe_code_span(_escape_table_cell(f.report_path))} |\n"
        for i, f in enumerate(filings)
    )

    extra_yaml_lines = "".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}\n"
        for key, value in (extra_yaml or {}).items()
    )

    yaml_header = f"""\
---
report_type: {report_type}
source_files: {json.dumps(source_files, ensure_ascii=False)}
{unit_label}_count: {len(filings)}
{extra_yaml_lines}analysis_date: {date_display}
model: {json.dumps(model, ensure_ascii=False)}
input_tokens: {input_tokens}
cache_creation_tokens: {cache_creation_tokens}
cache_read_tokens: {cache_read_tokens}
output_tokens: {output_tokens}
total_tokens: {total_tokens}
elapsed_seconds: {elapsed_seconds:.1f}
estimated_cost_usd: {estimated_cost:.4f}
billed_to: {"claude-plan" if _plan_usage() else "api"}
pricing_uncertain: {str(pricing_uncertain).lower()}
---

"""

    extra_blocks = "".join(
        f"\n## {heading}\n\n{content}\n" for heading, content in extra_sections
    )

    body = f"""\
> **Asia Equity Analyzer — {banner_title}** — Generated {date_display}
> {unit_label_plural}: {len(filings)} | Model: {model_span} | Tokens: {total_tokens:,} | {_cost_banner(estimated_cost)}

---

## {filings_heading}

| # | Source File | Full Report |
|---|-------------|-------------|
{filing_rows}
---

## {table_heading}

{score_table}
{extra_blocks}{cost_footnote}
---

{narrative_text}
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(yaml_header + body)

    print(f"📝 {banner_title} report written: {output_path}")
    return output_path


def write_comparison_report(
    score_table: str,
    trajectory_text: str,
    filings: list,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    elapsed_seconds: float,
    estimated_cost: float,
    pricing_uncertain: bool = False,
) -> str:
    """Write the cross-period comparison report.

    Args:
        score_table: Rendered markdown score-trajectory table.
        trajectory_text: The trend-analysis agent's output.
        filings: FilingAnalysis objects in chronological order.
        model: Model that produced the analyses.
        input_tokens/output_tokens/cache_*: Aggregate usage across all filings
            and the trajectory call.
        elapsed_seconds: Total wall-clock time for the comparison run.
        estimated_cost: Estimated USD cost for the whole run.
        pricing_uncertain: True if `model` differs from the pricing baseline.

    Returns:
        Path to the written comparison report.
    """
    return _write_multi_filing_report(
        report_type="cross_period_comparison",
        filename_prefix="comparison",
        banner_title="Cross-Period Comparison",
        filings_heading="Filings Compared (chronological)",
        table_heading="Score Trajectory",
        score_table=score_table,
        narrative_text=trajectory_text,
        filings=filings,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        elapsed_seconds=elapsed_seconds,
        estimated_cost=estimated_cost,
        pricing_uncertain=pricing_uncertain,
        unit_label="period",
        unit_label_plural="Periods",
    )


def write_batch_report(
    ranking_table: str,
    stats_table: str,
    governance_table: str,
    sector_text: str,
    filings: list,
    skipped: list,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    elapsed_seconds: float,
    estimated_cost: float,
    pricing_uncertain: bool = False,
) -> str:
    """Write the batch sector report.

    Args:
        ranking_table: Companies sorted by composite score.
        stats_table: Per-dimension sector statistics.
        governance_table: Governance rating distribution.
        sector_text: The sector-summary agent's output.
        filings: Successfully analyzed companies, in ranking order.
        skipped: (filename, reason) pairs for filings that were not analyzed.
            Always rendered when non-empty — a sector read over a partial
            batch must say so rather than implying full coverage.

    Returns:
        Path to the written batch report.
    """
    extra_sections = [
        ("Sector Statistics", stats_table),
        ("Governance Distribution", governance_table),
    ]
    if skipped:
        skipped_rows = "".join(
            f"| {_safe_code_span(_escape_table_cell(name))} "
            f"| {_escape_table_cell(reason)} |\n"
            for name, reason in skipped
        )
        extra_sections.append(
            (
                f"Skipped Filings ({len(skipped)})",
                "*These filings are excluded from every table and from the sector "
                "summary below.*\n\n"
                "| Source File | Reason |\n|-------------|--------|\n" + skipped_rows,
            )
        )

    return _write_multi_filing_report(
        report_type="batch_sector_summary",
        filename_prefix="sector",
        banner_title="Sector Batch",
        filings_heading="Companies Analyzed (ranking order)",
        table_heading="Sector Ranking",
        score_table=ranking_table,
        narrative_text=sector_text,
        filings=filings,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        elapsed_seconds=elapsed_seconds,
        estimated_cost=estimated_cost,
        pricing_uncertain=pricing_uncertain,
        unit_label="company",
        unit_label_plural="Companies",
        extra_sections=tuple(extra_sections),
        extra_yaml={
            "skipped_count": len(skipped),
            "skipped_files": [name for name, _ in skipped],
        },
    )


def write_peer_report(
    ranking_table: str,
    peer_text: str,
    filings: list,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    elapsed_seconds: float,
    estimated_cost: float,
    pricing_uncertain: bool = False,
) -> str:
    """Write the peer-comparison report (filings in ranking order, best first)."""
    return _write_multi_filing_report(
        report_type="peer_comparison",
        filename_prefix="peers",
        banner_title="Peer Comparison",
        filings_heading="Companies Compared (ranking order)",
        table_heading="Peer Ranking",
        score_table=ranking_table,
        narrative_text=peer_text,
        filings=filings,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        elapsed_seconds=elapsed_seconds,
        estimated_cost=estimated_cost,
        pricing_uncertain=pricing_uncertain,
        unit_label="company",
        unit_label_plural="Companies",
    )
