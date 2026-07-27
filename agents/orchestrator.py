"""Multi-agent orchestrator: six parallel section specialists + a synthesis agent.

All specialists share an identical system prefix (base analyst context + the
document, both marked with cache_control), so the document is paid for once
and served from the prompt cache for the remaining agents. Cache entries only
become readable after the first response begins, so the first specialist runs
alone to warm the cache before the other five are dispatched in parallel.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
import time

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    MODEL,
    REQUEST_TIMEOUT,
    SPECIALIST_MAX_TOKENS,
    SYNTHESIS_MAX_TOKENS,
    TEMPERATURE,
)
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
    client: anthropic.Anthropic,
    system,
    user_content: str,
    model: str,
    max_tokens: int,
    key: str,
    title: str,
) -> SectionResult:
    start = time.time()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=TEMPERATURE,
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
    )


def _run_specialist(
    client: anthropic.Anthropic,
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
    print(
        f"   ✅ {spec.title} ({result.elapsed_seconds:.1f}s, "
        f"out {result.output_tokens:,}{cache_note})"
    )
    if result.truncated:
        print(
            f"   ⚠️  {spec.title} was cut off at the {SPECIALIST_MAX_TOKENS:,}-token limit. "
            "Consider raising SPECIALIST_MAX_TOKENS in config.py."
        )
    return result


def analyze_document_multi(document_text: str, model: str = MODEL) -> MultiAnalysisResult:
    """Run the six-specialist parallel analysis plus the synthesis pass.

    Raises:
        SystemExit: If the API key is missing or any API call fails.
    """
    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        raise SystemExit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=REQUEST_TIMEOUT)
    system = _build_system(document_text)

    print(f"🤖 Running {len(SPECIALISTS)} specialist agents on {model}...")
    print(f"   Document length: {len(document_text):,} characters")

    sections: list[SectionResult] = []
    try:
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
        if synthesis.truncated:
            print(
                f"   ⚠️  Synthesis was cut off at the {SYNTHESIS_MAX_TOKENS:,}-token limit. "
                "Consider raising SYNTHESIS_MAX_TOKENS in config.py."
            )
        sections.append(synthesis)
    except anthropic.APIConnectionError as e:
        print(f"❌ API connection error: {e}")
        raise SystemExit(1)
    except anthropic.RateLimitError as e:
        print(f"❌ Rate limit exceeded: {e}")
        raise SystemExit(1)
    except anthropic.APIStatusError as e:
        print(f"❌ API error (status {e.status_code}): {e.message}")
        raise SystemExit(1)

    combined_text = "\n\n".join(s.text for s in sections)
    return MultiAnalysisResult(
        text=combined_text,
        input_tokens=sum(s.input_tokens for s in sections),
        output_tokens=sum(s.output_tokens for s in sections),
        cache_creation_tokens=sum(s.cache_creation_tokens for s in sections),
        cache_read_tokens=sum(s.cache_read_tokens for s in sections),
        model=sections[0].model,
        sections=sections,
    )
