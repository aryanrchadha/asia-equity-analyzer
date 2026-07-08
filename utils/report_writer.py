"""Report writer: saves analysis output as a timestamped markdown file."""

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
    return name.lower()


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

    total_tokens = input_tokens + output_tokens

    # Build character / page detail line
    chars_sent = min(original_chars, MAX_DOCUMENT_CHARS) if was_truncated else original_chars
    if was_truncated:
        chars_detail = f"{chars_sent:,} of {original_chars:,} *(truncated)*"
    else:
        chars_detail = f"{original_chars:,}"

    pages_row = f"| Pages | {page_count:,} |\n" if page_count else ""
    cost_label = "Estimated API Cost" + (" ⚠️" if pricing_uncertain else "")
    cost_footnote = (
        f"\n*⚠️ Cost estimate uses `{MODEL}` pricing constants but the response "
        f"came from `{model}` — actual cost may differ.*\n"
        if pricing_uncertain
        else ""
    )

    stats_table = f"""\
## Processing Statistics

| Parameter | Value |
|-----------|-------|
| Source File | `{source_filename}` |
{pages_row}| Characters Sent | {chars_detail} |
| Model | `{model}` |
| Input Tokens | {input_tokens:,} |
| Output Tokens | {output_tokens:,} |
| Total Tokens | {total_tokens:,} |
| {cost_label} | ${estimated_cost:.4f} |
| Analysis Time | {elapsed_seconds:.1f}s |
{cost_footnote}
---

"""

    yaml_header = f"""\
---
source_file: {json.dumps(source_filename, ensure_ascii=False)}
analysis_date: {date_display}
model: {json.dumps(model, ensure_ascii=False)}
input_tokens: {input_tokens}
output_tokens: {output_tokens}
total_tokens: {total_tokens}
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
        f"> Source: `{source_filename}` | Model: `{model}` | "
        f"Tokens: {total_tokens:,} | Cost: ${estimated_cost:.4f}\n\n---\n\n"
    )

    full_report = yaml_header + banner + stats_table + analysis_text

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_report)

    print(f"📝 Report written: {output_path}")
    return output_path
