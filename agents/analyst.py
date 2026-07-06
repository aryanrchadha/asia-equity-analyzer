"""Financial analyst agent: sends document text to Claude for analysis."""

from dataclasses import dataclass

import anthropic

from config import ANTHROPIC_API_KEY, MAX_TOKENS, MODEL, REQUEST_TIMEOUT, TEMPERATURE
from prompts.financial_analysis import FINANCIAL_ANALYSIS_PROMPT


@dataclass
class AnalysisResult:
    """Container for the analysis response and metadata."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str


def analyze_document(
    document_text: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> AnalysisResult:
    """Send the document text to Claude for financial analysis.

    Args:
        document_text: Extracted text from the company filing.
        model: Claude model ID to use; defaults to the value in config.
        max_tokens: Maximum tokens for the response; defaults to config value.

    Returns:
        AnalysisResult with the analysis text and token usage.

    Raises:
        SystemExit: If the API call fails.
    """
    if not ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        raise SystemExit(1)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=REQUEST_TIMEOUT)

    user_message = (
        "Analyze the following company filing and produce your full "
        "investment analysis report:\n\n"
        f"{document_text}"
    )

    print(f"🤖 Sending to {model}...")
    print(f"   Document length: {len(document_text):,} characters")
    print(f"   Max output tokens: {max_tokens:,}")
    print(f"   Temperature: {TEMPERATURE}")

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
            system=FINANCIAL_ANALYSIS_PROMPT,
            messages=[
                {"role": "user", "content": user_message}
            ],
        )
    except anthropic.APIConnectionError as e:
        print(f"❌ API connection error: {e}")
        raise SystemExit(1)
    except anthropic.RateLimitError as e:
        print(f"❌ Rate limit exceeded: {e}")
        raise SystemExit(1)
    except anthropic.APIStatusError as e:
        print(f"❌ API error (status {e.status_code}): {e.message}")
        raise SystemExit(1)

    # Extract text from response content blocks
    analysis_text = ""
    for block in response.content:
        if block.type == "text":
            analysis_text += block.text

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    stop_reason = response.stop_reason
    if stop_reason == "max_tokens":
        print(
            f"⚠️  Warning: Response was cut off at the max_tokens limit ({max_tokens:,}). "
            "Consider increasing MAX_TOKENS in config.py or via --max-tokens."
        )

    print(f"✅ Analysis complete.")
    print(f"   Input tokens:  {input_tokens:,}")
    print(f"   Output tokens: {output_tokens:,}")
    print(f"   Stop reason:   {stop_reason}")

    return AnalysisResult(
        text=analysis_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=response.model,
    )
