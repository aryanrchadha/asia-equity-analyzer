# Asia Equity Analyzer 📊

An AI-powered CLI tool that produces institutional-grade investment analysis for Asia ex-Japan equities. Drop a company's annual report into `input/`, run one command, and get a comprehensive sell-side research report in `output/`.

## What It Does

The analyzer reads a company's annual report (PDF or text) and generates a structured 10-section investment analysis covering:

| Section | What It Covers |
|---------|---------------|
| Company Overview | Business, geography, listing, ownership structure |
| Revenue Quality & Growth | CAGR, concentration, recurring vs. one-time, FX impact |
| Margin Analysis | Gross/operating margins, GAAP vs. non-GAAP, peer comparison |
| Balance Sheet & Liquidity | Leverage, working capital, dilution, structural risk |
| Cash Flow Quality | Cash conversion, FCF, capex intensity, capital allocation |
| Competitive Moat | 5-dimension moat scoring + AI disruption overlay |
| Governance & Management | Insider alignment, controlling shareholder risk, board quality |
| Key Financial Tensions | Where the numbers tell conflicting stories |
| Composite Scorecard | Weighted quantitative score out of 5.0 |
| Bottom Line | Single-paragraph investment thesis |

Built for analysts covering **Greater China, Southeast Asia, Korea, Taiwan, and India**.

## Setup

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com/)

### Installation

```bash
# Clone or download this project
cd asia-equity-analyzer

# Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set your API key
echo "ANTHROPIC_API_KEY=sk-ant-your-key-here" > .env
```

## Usage

1. **Drop a file** into the `input/` folder — either a PDF annual report or a `.txt` file with the filing text.

2. **Run the analyzer:**

```bash
python main.py
```

3. **Check the output** — a timestamped markdown file will appear in `output/`:

```
output/tencent_annual_report_2024_2026-04-06_142315.md
```

That's it. No arguments, no flags. It processes the most recent file in `input/`.

By default the analysis runs as a **multi-agent pipeline**: six section specialists analyze the filing in parallel (Overview & Revenue, Margins, Balance Sheet, Cash Flow, Moat, Governance), then a synthesis agent reads their output and writes the Key Tensions, Composite Scorecard, and Bottom Line sections. The filing is sent once and shared across agents via prompt caching, so the parallel agents read the document from cache at ~10% of the input price.

To use the original single-agent mode instead:

```bash
python main.py --single
```

### Example Console Output

```
============================================================
  📊 Asia Equity Analyzer
  Institutional-grade analysis for Asia ex-Japan equities
============================================================

📄 Loading: tencent_annual_report_2024.pdf
   Characters extracted: 142,387

🤖 Sending to claude-sonnet-4-20250514...
   Document length: 142,387 characters
✅ Analysis complete.
   Input tokens:  38,412
   Output tokens: 7,891
   Analysis time: 24.3s

────────────────────────────────────────────────────────────
  📋 SUMMARY
────────────────────────────────────────────────────────────
  Source file:    tencent_annual_report_2024.pdf
  Output file:    output/tencent_annual_report_2024_2026-04-06_142315.md
  Model:          claude-sonnet-4-20250514
  Input tokens:   38,412
  Output tokens:  7,891
  Total tokens:   46,303
  Est. cost:      $0.2339
  Time elapsed:   24.3s
────────────────────────────────────────────────────────────
```

### Example Output Structure

The generated markdown report includes a YAML front-matter block and then 10 structured sections:

```markdown
---
source_file: tencent_annual_report_2024.pdf
analysis_date: 2026-04-06 14:23:15
model: claude-sonnet-4-20250514
input_tokens: 38,412
output_tokens: 7,891
total_tokens: 46,303
---

## SECTION 1: COMPANY OVERVIEW
...

## SECTION 9: COMPOSITE SCORECARD

| Dimension | Score | Weight | Weighted Score |
|-----------|-------|--------|----------------|
| Revenue Quality | 4/5 | 20% | 0.80 |
| ... | ... | ... | ... |
| **COMPOSITE** | | **100%** | **3.85 / 5.0** |

## SECTION 10: WHAT THIS MEANS — BOTTOM LINE
...
```

## Project Structure

```
asia-equity-analyzer/
├── main.py                  # CLI entry point
├── config.py                # Model settings, dirs, cost constants
├── agents/
│   ├── analyst.py           # Single-agent mode (one Claude API call)
│   └── orchestrator.py      # Multi-agent mode: 6 parallel specialists + synthesis
├── prompts/
│   ├── financial_analysis.py  # Single-agent system prompt
│   └── section_prompts.py     # Specialist + synthesis prompts (multi-agent)
├── utils/
│   ├── document_loader.py   # PDF/text extraction
│   └── report_writer.py     # Markdown report output
├── input/                   # Drop files here
├── output/                  # Reports appear here
├── .env                     # API key (gitignored)
├── .gitignore
├── requirements.txt
└── README.md
```

## Cost Estimates

Using Claude Sonnet 4 pricing ($3/M input, $15/M output):

| Report Size | Input Tokens | Output Tokens | Approx. Cost |
|-------------|-------------|---------------|--------------|
| Short (30 pages) | ~15,000 | ~6,000 | ~$0.14 |
| Medium (80 pages) | ~40,000 | ~8,000 | ~$0.24 |
| Long (200+ pages) | ~80,000 | ~8,000 | ~$0.36 |

## Limitations

- Documents over 180,000 characters are truncated (later sections may be cut)
- Analysis quality depends on the quality of the source document
- Financial figures are extracted as-is — no independent verification
- The tool analyzes one filing at a time; cross-period comparisons are limited to what's in the single report

## Multi-Agent Architecture

The default pipeline decomposes the analysis into **six parallel specialist agents** plus a synthesis pass:

| Agent | Sections |
|-------|----------|
| Overview & Revenue | 1. Company Overview, 2. Revenue Quality & Growth |
| Margins | 3. Margin Analysis |
| Balance Sheet | 4. Balance Sheet & Liquidity |
| Cash Flow | 5. Cash Flow Quality |
| Moat | 6. Competitive Moat Assessment |
| Governance | 7. Governance & Management |
| Synthesis | 8. Key Tensions, 9. Composite Scorecard, 10. Bottom Line |

How it runs:

1. The first specialist runs alone to **warm the prompt cache** — the shared analyst context and the full filing are marked with `cache_control`.
2. The remaining five specialists run **in parallel**, reading the cached document at ~10% of the input token price.
3. The synthesis agent receives the six specialist outputs (not the raw filing) and produces the cross-cutting sections, using the scores stated by the specialists.

Each report includes an **Agent Breakdown** table with per-agent token usage and timing, and the cost estimate accounts for cache writes (1.25×) and cache reads (0.1×).

## Roadmap

- ✅ **Week 2** — six parallel financial analysis agents with specialized prompts, prompt caching, and a synthesis pass
- 🗓️ **Week 3** — cross-period comparison: analyze multiple filings of the same company and track score deltas over time

## License

MIT
