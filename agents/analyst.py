"""Financial analyst agent: sends document text to Claude for analysis."""

from __future__ import annotations

from dataclasses import dataclass

from config import MAX_TOKENS, MODEL, TEMPERATURE
from prompts.financial_analysis import FINANCIAL_ANALYSIS_PROMPT
from agents.orchestrator import PipelineError, api_errors, make_client
from utils.models import request_params, temperature_ignored


@dataclass
class AnalysisResult:
    """Container for the analysis response and metadata."""

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None = None

    @property
    def incomplete_sections(self) -> list[tuple[str, str]]:
        """Same shape as MultiAnalysisResult.incomplete_sections."""
        if self.stop_reason == "refusal":
            return [("Analyst", "declined by the model")]
        if self.stop_reason == "max_tokens":
            return [("Analyst", "cut off at the output-token limit")]
        return []


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
        PipelineError: If the API call fails or the model declines outright.
    """
    client = make_client()

    user_message = (
        "Analyze the following company filing and produce your full "
        "investment analysis report:\n\n"
        f"{document_text}"
    )

    print(f"🤖 Sending to {model}...")
    print(f"   Document length: {len(document_text):,} characters")
    print(f"   Max output tokens: {max_tokens:,}")
    params = request_params(model, max_tokens, TEMPERATURE)
    if params["max_tokens"] != max_tokens:
        print(f"   Output budget raised to {params['max_tokens']:,} ({model} thinks by default)")
    if temperature_ignored(model, TEMPERATURE):
        print(f"   ℹ️  TEMPERATURE={TEMPERATURE} is not sent: {model} rejects sampling parameters.")

    with api_errors():
        response = client.messages.create(
            **params,
            system=FINANCIAL_ANALYSIS_PROMPT,
            messages=[
                {"role": "user", "content": user_message}
            ],
        )

    # Extract text from response content blocks
    analysis_text = ""
    for block in response.content:
        if block.type == "text":
            analysis_text += block.text

    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens

    stop_reason = response.stop_reason
    if stop_reason == "refusal":
        if not analysis_text.strip():
            # Nothing to write: a report with only a stats table would look
            # like a finished analysis.
            print("❌ The model declined this request and produced no analysis.")
            raise PipelineError("declined by the model", systemic=False)
        print("⚠️  Warning: The model declined partway through; the analysis is incomplete.")
    elif stop_reason == "max_tokens":
        print(
            f"⚠️  Warning: Response was cut off at the max_tokens limit "
            f"({params['max_tokens']:,}). "
            "Consider increasing MAX_TOKENS in config.py or via --max-tokens."
        )

    print("✅ Analysis complete.")
    print(f"   Input tokens:  {input_tokens:,}")
    print(f"   Output tokens: {output_tokens:,}")
    print(f"   Stop reason:   {stop_reason}")

    return AnalysisResult(
        text=analysis_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=response.model,
        stop_reason=stop_reason,
    )
