"""Shared test scaffolding: dependency stubs, fakes, and report builders.

Import this FIRST in every test module. Installing the stubs is an import-time
side effect, so it has to happen before anything imports the app modules that
pull in `anthropic`, `fitz`, or `dotenv`.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class _StubAPIError(Exception):
    """Stand-in for the anthropic exception hierarchy."""

    def __init__(self, message="stub error", status_code=500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def install_stubs() -> None:
    """Install stand-ins for the third-party packages the app imports.

    The suite never touches the network; live behavior is covered separately
    by tests/smoke_live.py, which needs real credentials.
    """
    if "anthropic" not in sys.modules:
        anthropic = types.ModuleType("anthropic")
        anthropic.Anthropic = FakeAnthropic
        anthropic.APIConnectionError = type("APIConnectionError", (_StubAPIError,), {})
        anthropic.RateLimitError = type("RateLimitError", (_StubAPIError,), {})
        anthropic.APIStatusError = type("APIStatusError", (_StubAPIError,), {})
        sys.modules["anthropic"] = anthropic

    if "fitz" not in sys.modules:
        sys.modules["fitz"] = types.ModuleType("fitz")

    if "dotenv" not in sys.modules:
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *a, **k: None
        sys.modules["dotenv"] = dotenv


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=50, cache_creation=0, cache_read=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class FakeResponse:
    def __init__(self, text, model="claude-sonnet-4-6", stop_reason="end_turn", usage=None):
        self.content = [FakeBlock(text)]
        self.model = model
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


class FakeMessages:
    def __init__(self, client):
        self._client = client

    def create(self, **kwargs):
        self._client.calls.append(kwargs)
        if self._client.error is not None:
            raise self._client.error
        responder = self._client.responder
        text = responder(kwargs) if callable(responder) else responder
        # First call pays the cache write; later calls read it, mirroring how
        # the orchestrator warms the cache with one specialist before fanning out.
        first = len(self._client.calls) == 1
        usage = FakeUsage(
            input_tokens=10,
            output_tokens=20,
            cache_creation=500 if first else 0,
            cache_read=0 if first else 500,
        )
        return FakeResponse(text, model=self._client.model, usage=usage,
                            stop_reason=self._client.stop_reason)


class FakeAnthropic:
    """Records every request and returns canned text."""

    def __init__(self, api_key=None, timeout=None, **kwargs):
        self.api_key = api_key
        self.timeout = timeout
        self.calls = []
        self.responder = "stub response"
        self.error = None
        self.model = "claude-sonnet-4-6"
        self.stop_reason = "end_turn"
        self.messages = FakeMessages(self)


# --------------------------------------------------------------------------
# Report builders
# --------------------------------------------------------------------------


def build_report(
    composite="3.90",
    revenue=4,
    margins=3,
    balance=4,
    cash_flow=3,
    moat=10,
    governance="STRONG",
    include_normalization=True,
) -> str:
    """A report body in the exact shape the specialists are told to emit."""
    normalization = (
        f"| Composite Moat | {moat}/15 (normalized: 3.3/5.0) | 20% | 0.67 |\n"
        if include_normalization
        else ""
    )
    return f"""## SECTION 1: COMPANY OVERVIEW
Listed in Hong Kong. Free float 45%. Reporting under IFRS.

## SECTION 2: REVENUE QUALITY & GROWTH (Score 1-5)
Revenue grew 12% with moderate concentration.
SCORE: {revenue}/5

## SECTION 3: MARGIN ANALYSIS (Score 1-5)
Gross margin stable at 43%.
SCORE: {margins}/5

## SECTION 4: BALANCE SHEET & LIQUIDITY (Score 1-5)
Net cash position of HKD 12.4bn.
SCORE: {balance}/5

## SECTION 5: CASH FLOW QUALITY (Score 1-5)
OCF/NI of 1.1x.
SCORE: {cash_flow}/5

## SECTION 6: COMPETITIVE MOAT ASSESSMENT
### Composite Moat Score
SCORE: {moat}/15

## SECTION 7: GOVERNANCE & MANAGEMENT
Founder-led with meaningful insider ownership.
RATING: {governance}

## SECTION 8: KEY FINANCIAL TENSIONS
Growth is decelerating while capex rises.

## SECTION 9: COMPOSITE SCORECARD

| Dimension | Score | Weight | Weighted Score |
|-----------|-------|--------|----------------|
| Revenue Quality | {revenue}/5 | 20% | 0.80 |
{normalization}| **COMPOSITE** | | **100%** | **{composite} / 5.0** |

## SECTION 10: WHAT THIS MEANS — BOTTOM LINE
A cash-generative platform with a decelerating growth profile.
"""


def make_analysis(text=None, model="claude-sonnet-4-6", sections=None):
    """A MultiAnalysisResult carrying `text`, with one agent's stats."""
    from agents.orchestrator import MultiAnalysisResult, SectionResult

    text = build_report() if text is None else text
    section = SectionResult(
        key="overview", title="Overview", text=text, input_tokens=10, output_tokens=20,
        cache_creation_tokens=5, cache_read_tokens=15, model=model,
        elapsed_seconds=0.5, truncated=False,
    )
    return MultiAnalysisResult(
        text=text, input_tokens=10, output_tokens=20, cache_creation_tokens=5,
        cache_read_tokens=15, model=model, sections=sections or [section],
    )


def make_doc(filename="tencent_fy2024.pdf", text="filing text"):
    from utils.document_loader import DocumentInfo

    return DocumentInfo(
        text=text, filename=filename, filepath=f"input/{filename}",
        page_count=3, original_chars=len(text), was_truncated=False,
    )


def make_filing(name="tencent", composite="3.90", **kwargs):
    """A FilingAnalysis with parsed scores, as the group modes build them."""
    from agents.comparator import make_filing_analysis

    analysis = make_analysis(build_report(composite=composite, **kwargs))
    return make_filing_analysis(f"{name}.pdf", analysis, f"output/{name}.md")


@contextlib.contextmanager
def quiet():
    """Swallow the CLI's console output so test results stay readable."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def temp_workspace() -> str:
    """A scratch directory, entered as the working directory."""
    path = tempfile.mkdtemp(prefix="aea-test-")
    os.chdir(path)
    return path
