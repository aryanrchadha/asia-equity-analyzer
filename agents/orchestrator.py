"""Multi-agent orchestrator: six parallel section specialists + a synthesis agent.

All specialists share an identical system prefix (base analyst context + the
document, both marked with cache_control), so the document is paid for once
and served from the prompt cache for the remaining agents. Cache entries only
become readable after the first response begins, so the first specialist runs
alone to warm the cache before the other five are dispatched in parallel.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time

import anthropic

from agents.claude_code import ClaudeCodeClient, ClaudeCodeError, cli_available
from config import (
    ANTHROPIC_API_KEY,
    BACKEND,
    BACKENDS,
    CLAUDE_CLI,
    MODEL,
    REQUEST_TIMEOUT,
    SPECIALIST_MAX_TOKENS,
    SYNTHESIS_MAX_TOKENS,
    TEMPERATURE,
)
from utils.models import request_params, temperature_ignored
from prompts.section_prompts import (
    BASE_ANALYST_CONTEXT,
    SPECIALISTS,
    SYNTHESIS_INSTRUCTIONS,
    SpecialistSpec,
)


@dataclass
class SectionResult:
    """Output and usage for one agent in the pipeline."""

    key: str
    title: str
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    model: str
    elapsed_seconds: float
    truncated: bool
    # The model declined (stop_reason "refusal"); text is empty or partial.
    refused: bool = False


@dataclass
class MultiAnalysisResult:
    """Combined report and aggregated usage across all agents."""

    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    model: str
    sections: list = field(default_factory=list)

    @property
    def incomplete_sections(self) -> list[tuple[str, str]]:
        """(agent title, reason) for every agent whose output is not whole."""
        problems = []
        for section in self.sections:
            if section.refused:
                problems.append((section.title, "declined by the model"))
            elif section.truncated:
                problems.append((section.title, "cut off at the output-token limit"))
        return problems


# print() writes the text and the trailing newline as separate calls, so two
# specialists finishing together can interleave into one garbled line. The
# specialists run concurrently, so their console output is serialised here.
_print_lock = threading.Lock()


def _log(message: str) -> None:
    """Write a whole message from a worker thread without interleaving."""
    with _print_lock:
        print(message)


class PipelineError(SystemExit):
    """The pipeline could not produce an analysis.

    Subclasses SystemExit so a one-off run still exits cleanly with code 1.
    Batch and watch runs read `systemic` to tell a failure caused by this
    filing apart from one that would hit every filing — missing or invalid
    credentials, rate limits, outages, a mistyped model. Counting the latter
    against a file would, in watch mode, permanently abandon every filing in
    the inbox over a configuration mistake.
    """

    def __init__(self, reason: str, systemic: bool, needs_restart: bool = False):
        super().__init__(1)
        self.reason = reason
        self.systemic = systemic
        # The key and model are read once at startup, so a missing or rejected
        # key or an unknown model won't clear up while the process runs —
        # unlike a rate limit or outage, retrying later can't help.
        self.needs_restart = needs_restart


# Statuses that would fail for any filing: credentials (401/403), an unknown
# model (404), timeouts (408), rate limits (429), and server trouble (5xx).
# Anything else — typically a 400 for an oversized or malformed request — is
# attributed to the filing.
_SYSTEMIC_STATUSES = {401, 403, 404, 408, 429}
_RESTART_STATUSES = {401, 403, 404}


_backend = BACKEND


def set_backend(name: str) -> None:
    """Choose where model calls go for the rest of the process."""
    global _backend
    if name not in BACKENDS:
        raise ValueError(f"unknown backend {name!r}; choose from {', '.join(BACKENDS)}")
    _backend = name


def current_backend() -> str:
    return _backend


def uses_plan_usage() -> bool:
    """True when calls draw on a Claude plan, so dollar figures aren't billed."""
    return _backend == "claude-code"


def has_credentials() -> bool:
    """Whether the selected backend can be reached at all (checked before any work)."""
    if _backend == "claude-code":
        return cli_available(CLAUDE_CLI)
    return bool(ANTHROPIC_API_KEY)


def credentials_help() -> list[str]:
    """What to tell the user when has_credentials() is False."""
    if _backend == "claude-code":
        return [
            f"❌ The Claude Code CLI ('{CLAUDE_CLI}') was not found on PATH.",
            "   Install it and log in once with your Claude account:",
            "     npm install -g @anthropic-ai/claude-code && claude",
            "   Or point CLAUDE_CLI at the binary, or use --backend api with an API key.",
        ]
    return [
        "❌ ANTHROPIC_API_KEY is not set. Add it to your .env file:",
        '   echo "ANTHROPIC_API_KEY=sk-ant-..." > .env',
        "   Or use --backend claude-code to run on your Claude plan instead.",
    ]


def make_client():
    """Build the client for the selected backend, exiting with guidance if unusable."""
    if not has_credentials():
        for line in credentials_help():
            print(line)
        reason = ("Claude Code CLI not found" if _backend == "claude-code"
                  else "no API key configured")
        raise PipelineError(reason, systemic=True, needs_restart=True)
    if _backend == "claude-code":
        return ClaudeCodeClient(CLAUDE_CLI, timeout=REQUEST_TIMEOUT)
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=REQUEST_TIMEOUT)


def _status_error(status: int, reason: str) -> PipelineError:
    return PipelineError(
        reason,
        systemic=status >= 500 or status in _SYSTEMIC_STATUSES,
        needs_restart=status in _RESTART_STATUSES,
    )


@contextmanager
def api_errors():
    """Convert API failures into a PipelineError with a user-facing message."""
    try:
        yield
    except ClaudeCodeError as e:
        print(f"❌ {e.reason}")
        if e.status is not None:
            raise _status_error(e.status, f"API error {e.status}") from None
        if e.kind in ("missing", "auth"):
            if e.kind == "auth":
                print("   Log in to Claude Code once by running `claude` in a terminal.")
            raise PipelineError("Claude Code is not logged in" if e.kind == "auth"
                                else "Claude Code CLI not found",
                                systemic=True, needs_restart=True) from None
        if e.kind == "too_long":
            raise PipelineError("filing too long for the model", systemic=False) from None
        raise PipelineError("Claude Code call failed", systemic=True) from None
    except anthropic.APIConnectionError as e:
        print(f"❌ API connection error: {e}")
        raise PipelineError("could not reach the API", systemic=True)
    except anthropic.RateLimitError as e:
        print(f"❌ Rate limit exceeded: {e}")
        raise PipelineError("rate limited", systemic=True)
    except anthropic.APIStatusError as e:
        print(f"❌ API error (status {e.status_code}): {e.message}")
        raise _status_error(e.status_code, f"API error {e.status_code}")


def _build_system(document_text: str) -> list:
    return [
        {
            "type": "text",
            "text": BASE_ANALYST_CONTEXT,
        },
        {
            "type": "text",
            "text": f"THE COMPANY FILING UNDER ANALYSIS:\n\n{document_text}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _call(
    client,
    system,
    user_content: str,
    model: str,
    max_tokens: int,
    key: str,
    title: str,
) -> SectionResult:
    start = time.time()
    response = client.messages.create(
        **request_params(model, max_tokens, TEMPERATURE),
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    elapsed = time.time() - start

    text = "".join(block.text for block in response.content if block.type == "text")
    usage = response.usage
    return SectionResult(
        key=key,
        title=title,
        text=text.strip(),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        model=response.model,
        elapsed_seconds=elapsed,
        truncated=response.stop_reason == "max_tokens",
        refused=response.stop_reason == "refusal",
    )


def _run_specialist(
    client,
    spec: SpecialistSpec,
    system,
    model: str,
) -> SectionResult:
    result = _call(
        client,
        system,
        spec.instructions,
        model,
        SPECIALIST_MAX_TOKENS,
        spec.key,
        spec.title,
    )
    cache_note = ""
    if result.cache_read_tokens:
        cache_note = f", cache read {result.cache_read_tokens:,}"
    lines = [
        f"   ✅ {spec.title} ({result.elapsed_seconds:.1f}s, "
        f"out {result.output_tokens:,}{cache_note})"
    ]
    if result.refused:
        lines.append(
            f"   ⚠️  {spec.title} was declined by the model; its section will be "
            "missing from the report."
        )
    elif result.truncated:
        lines.append(
            f"   ⚠️  {spec.title} was cut off at the output-token limit. "
            "Consider raising SPECIALIST_MAX_TOKENS in config.py."
        )
    _log("\n".join(lines))
    return result


def analyze_document_multi(document_text: str, model: str = MODEL) -> MultiAnalysisResult:
    """Run the six-specialist parallel analysis plus the synthesis pass.

    Raises:
        SystemExit: If the API key is missing or any API call fails.
    """
    client = make_client()
    system = _build_system(document_text)

    print(f"🤖 Running {len(SPECIALISTS)} specialist agents on {model}...")
    print(f"   Document length: {len(document_text):,} characters")
    if temperature_ignored(model, TEMPERATURE):
        print(f"   ℹ️  TEMPERATURE={TEMPERATURE} is not sent: {model} rejects sampling parameters.")

    sections: list[SectionResult] = []
    with api_errors():
        # Warm the prompt cache with the first specialist, then fan out. The
        # remaining five reuse the cached document instead of re-paying for it.
        sections.append(_run_specialist(client, SPECIALISTS[0], system, model))

        rest = SPECIALISTS[1:]
        with ThreadPoolExecutor(max_workers=len(rest)) as pool:
            futures = {
                pool.submit(_run_specialist, client, spec, system, model): spec
                for spec in rest
            }
            results_by_key = {}
            for future in as_completed(futures):
                result = future.result()
                results_by_key[result.key] = result
        # Preserve report section order regardless of completion order
        sections.extend(results_by_key[spec.key] for spec in rest)

        print("🧩 Synthesizing tensions, scorecard, and bottom line...")
        specialist_report = "\n\n".join(s.text for s in sections)
        synthesis = _call(
            client,
            [{"type": "text", "text": BASE_ANALYST_CONTEXT}],
            SYNTHESIS_INSTRUCTIONS + specialist_report,
            model,
            SYNTHESIS_MAX_TOKENS,
            "synthesis",
            "Synthesis (Tensions, Scorecard, Bottom Line)",
        )
        print(f"   ✅ {synthesis.title} ({synthesis.elapsed_seconds:.1f}s)")
        if synthesis.refused:
            print("   ⚠️  Synthesis was declined by the model; sections 8-10 will be missing.")
        elif synthesis.truncated:
            print(
                "   ⚠️  Synthesis was cut off at the output-token limit. "
                "Consider raising SYNTHESIS_MAX_TOKENS in config.py."
            )
        sections.append(synthesis)

    combined_text = "\n\n".join(s.text for s in sections)
    if not combined_text.strip():
        # Every agent declined. A report containing only a stats table would
        # read as a finished analysis, so write nothing and say why.
        print("❌ The model declined every part of this analysis; no report written.")
        raise PipelineError("declined by the model", systemic=False)
    return MultiAnalysisResult(
        text=combined_text,
        input_tokens=sum(s.input_tokens for s in sections),
        output_tokens=sum(s.output_tokens for s in sections),
        cache_creation_tokens=sum(s.cache_creation_tokens for s in sections),
        cache_read_tokens=sum(s.cache_read_tokens for s in sections),
        model=sections[0].model,
        sections=sections,
    )
