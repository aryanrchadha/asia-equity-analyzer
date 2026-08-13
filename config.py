"""Configuration settings for the Asia Equity Analyzer."""

import os
from dotenv import load_dotenv

load_dotenv()

# API Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Model Configuration
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000

# Multi-agent pipeline (six parallel section specialists + synthesis)
SPECIALIST_MAX_TOKENS = 4000   # per-specialist output cap
SYNTHESIS_MAX_TOKENS = 5000    # synthesis agent output cap

# API Request Parameters
TEMPERATURE = 1.0       # Claude default; lower values (e.g. 0.3) reduce variance in scores
REQUEST_TIMEOUT = 600   # Seconds; large annual reports can take several minutes

# Directory Configuration
INPUT_DIR = "input"
OUTPUT_DIR = "output"

# Cost Estimation (per 1M tokens, approximate USD pricing for Claude Sonnet 4)
INPUT_TOKEN_COST_PER_MILLION = 3.00
OUTPUT_TOKEN_COST_PER_MILLION = 15.00
CACHE_WRITE_COST_PER_MILLION = 3.75   # 1.25x input price (5-minute TTL cache writes)
CACHE_READ_COST_PER_MILLION = 0.30    # 0.1x input price

# Document Processing
MAX_DOCUMENT_CHARS = 180_000

# Watchlist (persistent score history across runs)
WATCHLIST_PATH = "watchlist.json"
WATCHLIST_ALERT_THRESHOLD = 0.5   # composite move (out of 5.0) that triggers an alert

# Directory watch (--watch-dir): analyze filings as they land
LEDGER_PATH = "processed.json"    # which filings have already been analyzed
WATCH_POLL_SECONDS = 60           # how often to rescan the directory
WATCH_SETTLE_SECONDS = 10         # a file must be unmodified this long before analysis
WATCH_MAX_ATTEMPTS = 3            # give up on a file after this many failed attempts
