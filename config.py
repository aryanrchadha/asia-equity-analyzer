"""Configuration settings for the Asia Equity Analyzer."""

import os
from dotenv import load_dotenv

load_dotenv()

# API Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Model Configuration
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000

# API Request Parameters
TEMPERATURE = 1.0       # Claude default; lower values (e.g. 0.3) reduce variance in scores
REQUEST_TIMEOUT = 600   # Seconds; large annual reports can take several minutes

# Directory Configuration
INPUT_DIR = "input"
OUTPUT_DIR = "output"

# Cost Estimation (per 1M tokens, approximate USD pricing for Claude Sonnet 4)
INPUT_TOKEN_COST_PER_MILLION = 3.00
OUTPUT_TOKEN_COST_PER_MILLION = 15.00

# Document Processing
MAX_DOCUMENT_CHARS = 180_000
