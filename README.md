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

### Cross-Period Comparison

To track how a company's scores evolve across filings, pass two or more reports of the same company in chronological order (earliest first):

```bash
python main.py --compare input/tencent_fy2022.pdf input/tencent_fy2023.pdf input/tencent_fy2024.pdf
```

Each filing gets its own full multi-agent report, then a comparison agent produces `output/comparison_<company>_<timestamp>.md` containing:

- A **score trajectory table** — every scored dimension per period plus a first-to-last delta, built deterministically from the `SCORE: X/N` lines in each report (no model re-scoring)
- **Thesis evolution** — which tensions resolved, worsened, or persist across every period
- A **trajectory bottom line** — improving/stable/deteriorating call and what the next filing must show

### Peer Comparison

To rank companies in the same sector against each other, pass one filing per company:

```bash
python main.py --peers input/tencent_fy2024.pdf input/alibaba_fy2024.pdf input/netease_fy2024.pdf
```

Each company gets its own full multi-agent report, then a peer agent produces `output/peers_<company>_<timestamp>.md` containing:

- A **peer ranking table** — every scored dimension per company, sorted by composite score (companies with unparseable composites rank last)
- **Sector ranking rationale** — where the composite agrees with the qualitative read and where it misleads; near-ties (within 0.3) get an explicit tiebreak call
- **Relative strengths and weaknesses** per company, and a **top pick / avoid** verdict with comparison caveats (fiscal period mismatches, reporting standards, disclosure gaps)

### Watchlist

Add `--watch` to any analysis run to record its scores to a persistent store and get alerted when a company moves materially:

```bash
python main.py --file input/tencent_fy2024.pdf --watch tencent
python main.py --peers input/tencent.pdf input/alibaba.pdf --watch
python main.py --show-watchlist
```

The company name is optional — pass one (`--watch tencent`) to keep filings whose filenames differ under a single entry, or omit it to name the entry after the file. Scores land in `watchlist.json` (a plain, diffable JSON file you can commit alongside your reports), and each run is compared against that company's previous entry:

- **Composite moves** of at least `--alert-threshold` points (default 0.5 out of 5.0) are flagged, with direction
- **Governance rating changes** are always flagged — a slide into `CONCERNING` or `RED FLAG` is material regardless of the composite

```
🔔 Watchlist alerts (threshold ±0.5):
   ⚠️  tencent: composite deteriorated 4.1 → 3.4 (-0.7, threshold ±0.5)
   ✅ alibaba: governance rating CONCERNING → ADEQUATE
```

`--show-watchlist` prints the current table (companies, filing count, latest and previous composite, delta, governance, last updated) and makes no API calls. Watchlist recording requires the multi-agent pipeline, since only its prompts emit the explicit `SCORE` lines the store reads.

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
│   ├── orchestrator.py      # Multi-agent mode: 6 parallel specialists + synthesis
│   └── comparator.py        # Cross-period comparison: score deltas + trajectory agent
├── prompts/
│   ├── financial_analysis.py  # Single-agent system prompt
│   ├── section_prompts.py     # Specialist + synthesis prompts (multi-agent)
│   └── comparison.py          # Cross-period trajectory prompt
├── utils/
│   ├── document_loader.py   # PDF/text extraction
│   ├── report_writer.py     # Markdown report output
│   ├── score_parser.py      # Extract SCORE/RATING lines from finished reports
│   └── watchlist.py         # Persistent score history + move alerts
├── watchlist.json           # Score history (created on first --watch run)
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
- Cross-period comparison (`--compare`) assumes the filings are for the same company and given in chronological order — it does not verify either

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
- ✅ **Week 3** — cross-period comparison (`--compare`): per-filing multi-agent reports, a deterministic score-delta table, and a trajectory analysis agent
- ✅ **Week 4** — peer comparison (`--peers`): rank companies in the same sector on the composite scorecard with a top pick / avoid verdict
- ✅ **Week 5** — watchlist (`--watch` / `--show-watchlist`): persistent score history with composite-move and governance-change alerts
- 🗓️ **Week 6** — batch mode: analyze a whole directory of filings in one run, with a sector-level summary

## License

MIT
