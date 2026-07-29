"""Report writer: saves analysis output as a timestamped markdown file."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

from config import MAX_DOCUMENT_CHARS, MODEL, OUTPUT_DIR


def sanitize_filename(name: str) -> str:
    """Convert a source filename into a clean company name for the output file."""
    # Remove extension
    name = os.path.splitext(name)[0]
    # Replace non-alphanumeric characters with underscores
    name = re.sub(r"[^a-zA-Z0-9]+", "_", name)
    # Collapse multiple underscores and strip leading/trailing
    name = re.sub(r"_+", "_", name).strip("_")
    return name.lower() or "report"


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
    cost_label = "Estimated API Cost" + (" ⚠️" if pricing_uncertain else "")
    cost_footnote = (
        f"\n*⚠️ Cost estimate uses {default_model_span} pricing constants but the response "
        f"came from {model_span} — actual cost may differ.*\n"
        if pricing_uncertain
        else ""
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
pricing_uncertain: {str(pricing_uncertain).lower()}
---

"""

    banner = (
        f"> **Asia Equity Analyzer** — Generated {date_display}\n"
        f"> Source: {source_span} | Model: {model_span} | "
        f"Tokens: {total_tokens:,} | Cost: ${estimated_cost:.4f}\n\n---\n\n"
    )

    full_report = yaml_header + banner + stats_table + analysis_text

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_report)

    print(f"📝 Report written: {output_path}")
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
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now()
    timestamp_str = timestamp.strftime("%Y-%m-%d_%H%M%S")
    date_display = timestamp.strftime("%Y-%m-%d %H:%M:%S")

    company = sanitize_filename(filings[0].filename)
    output_path = os.path.join(OUTPUT_DIR, f"comparison_{company}_{timestamp_str}.md")

    total_tokens = input_tokens + cache_creation_tokens + cache_read_tokens + output_tokens
    source_files = [f.filename for f in filings]

    model_span = _safe_code_span(model)
    default_model_span = _safe_code_span(MODEL)
    cost_footnote = (
        f"\n*⚠️ Cost estimate uses {default_model_span} pricing constants but the responses "
        f"came from {model_span} — actual cost may differ.*\n"
        if pricing_uncertain
        else ""
    )

    filing_rows = "".join(
        f"| {i + 1} | {_safe_code_span(_escape_table_cell(f.filename))} "
        f"| {_safe_code_span(_escape_table_cell(f.report_path))} |\n"
        for i, f in enumerate(filings)
    )

    yaml_header = f"""\
---
report_type: cross_period_comparison
source_files: {json.dumps(source_files, ensure_ascii=False)}
period_count: {len(filings)}
analysis_date: {date_display}
model: {json.dumps(model, ensure_ascii=False)}
input_tokens: {input_tokens}
cache_creation_tokens: {cache_creation_tokens}
cache_read_tokens: {cache_read_tokens}
output_tokens: {output_tokens}
total_tokens: {total_tokens}
elapsed_seconds: {elapsed_seconds:.1f}
estimated_cost_usd: {estimated_cost:.4f}
pricing_uncertain: {str(pricing_uncertain).lower()}
---

"""

    body = f"""\
> **Asia Equity Analyzer — Cross-Period Comparison** — Generated {date_display}
> Periods: {len(filings)} | Model: {model_span} | Tokens: {total_tokens:,} | Cost: ${estimated_cost:.4f}

---

## Filings Compared (chronological)

| # | Source File | Full Report |
|---|-------------|-------------|
{filing_rows}
---

## Score Trajectory

{score_table}
{cost_footnote}
---

{trajectory_text}
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(yaml_header + body)

    print(f"📝 Comparison report written: {output_path}")
    return output_path
