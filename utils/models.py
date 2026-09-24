"""What each Claude model accepts, and what it costs.

Request parameters are not uniform across models. Newer models reject
sampling parameters outright (a 400, not a warning), and several think by
default — which counts against max_tokens and can truncate a specialist's
answer that fit comfortably on older models. Pricing differs too, so a single
set of cost constants mis-states the bill for anything but the default model.

Rows come from Anthropic's published model table. Unknown models fall back to
the conservative behaviour: no sampling parameters sent, config pricing used,
and the cost flagged as uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import (
    CACHE_READ_COST_PER_MILLION,
    CACHE_WRITE_COST_PER_MILLION,
    INPUT_TOKEN_COST_PER_MILLION,
    OUTPUT_TOKEN_COST_PER_MILLION,
)

# The API's own default. Sending it explicitly changes nothing on models that
# accept it and is a hard error on models that don't, so it is never sent.
API_DEFAULT_TEMPERATURE = 1.0

# Output budget for models that think unless told not to. Thinking and the
# visible answer share max_tokens, so a 4,000-token specialist budget that is
# ample without thinking truncates with it. Kept under the ~21K ceiling at
# which the SDK refuses non-streaming requests.
THINKING_OUTPUT_BUDGET = 16_000


@dataclass(frozen=True)
class ModelProfile:
    family: str
    input_per_million: float
    output_per_million: float
    accepts_sampling_params: bool   # temperature / top_p / top_k
    thinks_by_default: bool         # omitting `thinking` still thinks
    # The tokenizer introduced with Opus 4.7 yields up to ~1.35x the tokens
    # for the same text; used only to scale pre-run cost estimates.
    new_tokenizer: bool = False

    @property
    def cache_write_per_million(self) -> float:
        return self.input_per_million * 1.25   # 5-minute TTL

    @property
    def cache_read_per_million(self) -> float:
        return self.input_per_million * 0.10


# Longest prefix wins, so "claude-opus-4-8" never matches "claude-opus-4".
_PROFILES = {
    "claude-fable-5": ModelProfile("Fable 5", 10.0, 50.0, False, True, True),
    "claude-mythos-5": ModelProfile("Mythos 5", 10.0, 50.0, False, True, True),
    # Opus 5's tokenizer is assumed to match Opus 4.8's; if it doesn't, the
    # only effect is a pre-run estimate that errs high.
    "claude-opus-5": ModelProfile("Opus 5", 5.0, 25.0, False, True, True),
    "claude-opus-4-8": ModelProfile("Opus 4.8", 5.0, 25.0, False, False, True),
    "claude-opus-4-7": ModelProfile("Opus 4.7", 5.0, 25.0, False, False, True),
    "claude-opus-4-6": ModelProfile("Opus 4.6", 5.0, 25.0, True, False),
    "claude-opus-4-5": ModelProfile("Opus 4.5", 5.0, 25.0, True, False),
    "claude-sonnet-5": ModelProfile("Sonnet 5", 3.0, 15.0, False, True, True),
    "claude-sonnet-4-6": ModelProfile("Sonnet 4.6", 3.0, 15.0, True, False),
    "claude-haiku-4-5": ModelProfile("Haiku 4.5", 1.0, 5.0, True, False),
}


def profile_for(model: str | None) -> ModelProfile | None:
    """The profile for a model ID, tolerating date suffixes and Bedrock prefixes."""
    if not model:
        return None
    name = model.lower().removeprefix("anthropic.")
    matches = [prefix for prefix in _PROFILES if name.startswith(prefix)]
    return _PROFILES[max(matches, key=len)] if matches else None


def request_params(model: str, max_tokens: int, temperature: float | None) -> dict:
    """Model-appropriate `messages.create` parameters.

    - temperature is sent only when it differs from the API default AND the
      model is known to accept it; an unknown model never gets it.
    - models that think by default get enough room to think and still answer.
    """
    profile = profile_for(model)
    params: dict = {"model": model, "max_tokens": max_tokens}

    if profile is not None and profile.thinks_by_default:
        params["max_tokens"] = max(max_tokens, THINKING_OUTPUT_BUDGET)

    if (
        temperature is not None
        and temperature != API_DEFAULT_TEMPERATURE
        and profile is not None
        and profile.accepts_sampling_params
    ):
        params["temperature"] = temperature
    return params


def temperature_ignored(model: str, temperature: float | None) -> bool:
    """True when a configured non-default temperature won't be sent."""
    if temperature is None or temperature == API_DEFAULT_TEMPERATURE:
        return False
    profile = profile_for(model)
    return profile is None or not profile.accepts_sampling_params


def pricing_for(model: str | None, fallback: tuple[float, float]) -> tuple[float, float, bool]:
    """(input $/M, output $/M, known) — `known` is False when using the fallback."""
    profile = profile_for(model)
    if profile is None:
        return fallback[0], fallback[1], False
    return profile.input_per_million, profile.output_per_million, True


def pricing_known(model: str | None) -> bool:
    """False when a cost estimate has to fall back to the config constants."""
    return profile_for(model) is not None


def estimate_cost(
    input_tokens: float,
    output_tokens: float,
    cache_creation_tokens: float = 0,
    cache_read_tokens: float = 0,
    model: str | None = None,
) -> float:
    """Estimate USD cost, including prompt-cache writes (1.25x) and reads (0.1x).

    Uses the model's published pricing when it's in the table above, and the
    config constants otherwise — callers should flag the latter via
    pricing_known() rather than present it as the bill.
    """
    profile = profile_for(model)
    if profile is not None:
        input_rate, output_rate = profile.input_per_million, profile.output_per_million
        write_rate = profile.cache_write_per_million
        read_rate = profile.cache_read_per_million
    else:
        input_rate, output_rate = INPUT_TOKEN_COST_PER_MILLION, OUTPUT_TOKEN_COST_PER_MILLION
        write_rate, read_rate = CACHE_WRITE_COST_PER_MILLION, CACHE_READ_COST_PER_MILLION
    return (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_creation_tokens * write_rate
        + cache_read_tokens * read_rate
    ) / 1_000_000
