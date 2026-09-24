"""Pre-run cost estimates, computed locally with no API calls.

Batch and watch runs can spend real money across a directory of filings, so
--dry-run answers "what will this cost?" before anything is sent. Text
extraction is local and free; tokens are estimated from the extracted text,
and the pipeline's shape (one cache write, five cache reads, a synthesis pass,
and an optional group agent) is priced with the model's own rates.

These are estimates, deliberately biased high: characters-per-token is set
conservatively, and the ceiling assumes every agent spends its entire output
budget. Actual tokenization and response length will differ.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import SPECIALIST_MAX_TOKENS, SYNTHESIS_MAX_TOKENS
from prompts.section_prompts import BASE_ANALYST_CONTEXT, SPECIALISTS, SYNTHESIS_INSTRUCTIONS
from utils.models import THINKING_OUTPUT_BUDGET, estimate_cost, profile_for, request_params

# English financial prose runs ~3.5-4 characters per token; the low end is used
# so estimates err high. CJK characters are roughly a token each.
CHARS_PER_TOKEN = 3.5
NEW_TOKENIZER_FACTOR = 1.35

# Share of its output budget a typical agent actually uses. A guess, and
# labelled as one wherever the expected figure is shown; the ceiling doesn't
# depend on it.
TYPICAL_OUTPUT_SHARE = 0.4

# The largest minimum cacheable prefix across current models. Below it, the
# estimate assumes no caching (every agent pays full price for the document)
# rather than guessing which side of a model's own minimum a filing falls.
CACHE_MIN_TOKENS = 4096

# What each filing contributes to a comparison/peer/sector agent's input: its
# tensions and bottom line (plus overview for peers), not the full report.
GROUP_EXCERPT_TOKENS = 1500

_WIDE_CHARS = re.compile(
    "[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]"
)


@dataclass
class Estimate:
    input_tokens: int   # estimated prompt size (document + instructions)
    expected: float     # USD at typical output length
    ceiling: float      # USD if every agent uses its full output budget
    cached: bool = True

    def __add__(self, other: "Estimate") -> "Estimate":
        return Estimate(
            self.input_tokens + other.input_tokens,
            self.expected + other.expected,
            self.ceiling + other.ceiling,
            self.cached and other.cached,
        )


def estimate_tokens(text: str, model: str | None = None) -> int:
    """Rough token count for `text` on `model`, erring high."""
    wide = len(_WIDE_CHARS.findall(text))
    tokens = wide + (len(text) - wide) / CHARS_PER_TOKEN
    profile = profile_for(model)
    if profile is not None and profile.new_tokenizer:
        tokens *= NEW_TOKENIZER_FACTOR
    return int(round(tokens))


def _budget(model: str, configured: int) -> int:
    """The output budget the request will actually carry for this model."""
    from agents.orchestrator import uses_plan_usage  # lazy: agents import utils

    budget = request_params(model, configured, None)["max_tokens"]
    if uses_plan_usage():
        # The Claude Code CLI thinks on every model (see agents/claude_code.py).
        budget = max(budget, THINKING_OUTPUT_BUDGET)
    return budget


def estimate_pipeline(document_text: str, model: str) -> Estimate:
    """Six specialists sharing a cached document, then a synthesis pass."""
    base = estimate_tokens(BASE_ANALYST_CONTEXT, model)
    prefix = base + estimate_tokens(document_text, model)
    instructions = sum(estimate_tokens(s.instructions, model) for s in SPECIALISTS)
    synthesis_instructions = estimate_tokens(SYNTHESIS_INSTRUCTIONS, model)
    specialist_budget = _budget(model, SPECIALIST_MAX_TOKENS)
    synthesis_budget = _budget(model, SYNTHESIS_MAX_TOKENS)
    agents = len(SPECIALISTS)
    cached = prefix >= CACHE_MIN_TOKENS

    def cost(share: float) -> float:
        specialist_output = specialist_budget * share
        if cached:
            # The first specialist writes the cache; the rest read it.
            write, read, uncached = prefix, prefix * (agents - 1), 0
        else:
            write, read, uncached = 0, 0, prefix * agents
        # The specialists' output becomes the synthesis agent's input.
        synthesis_input = base + synthesis_instructions + specialist_output * agents
        return estimate_cost(
            uncached + instructions + synthesis_input,
            specialist_output * agents + synthesis_budget * share,
            write,
            read,
            model=model,
        )

    return Estimate(prefix, cost(TYPICAL_OUTPUT_SHARE), cost(1.0), cached)


def estimate_single(document_text: str, model: str, max_tokens: int) -> Estimate:
    """The original one-call analysis (--single)."""
    from prompts.financial_analysis import FINANCIAL_ANALYSIS_PROMPT

    prompt = estimate_tokens(FINANCIAL_ANALYSIS_PROMPT + document_text, model)
    budget = _budget(model, max_tokens)

    def cost(share: float) -> float:
        return estimate_cost(prompt, budget * share, model=model)

    return Estimate(prompt, cost(TYPICAL_OUTPUT_SHARE), cost(1.0), cached=False)


def estimate_group_agent(filing_count: int, instructions: str, model: str) -> Estimate:
    """The comparison, peer, or sector agent that runs after the filings."""
    prompt = (
        estimate_tokens(BASE_ANALYST_CONTEXT, model)
        + estimate_tokens(instructions, model)
        + filing_count * GROUP_EXCERPT_TOKENS
    )
    budget = _budget(model, SYNTHESIS_MAX_TOKENS)

    def cost(share: float) -> float:
        return estimate_cost(prompt, budget * share, model=model)

    return Estimate(prompt, cost(TYPICAL_OUTPUT_SHARE), cost(1.0), cached=False)
