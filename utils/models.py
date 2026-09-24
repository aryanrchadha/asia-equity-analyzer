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

    @property
    def cache_write_per_million(self) -> float:
        return self.input_per_million * 1.25   # 5-minute TTL

    @property
    def cache_read_per_million(self) -> float:
        return self.input_per_million * 0.10


# Longest prefix wins, so "claude-opus-4-8" never matches "claude-opus-4".
_PROFILES = {
    "claude-fable-5": ModelProfile("Fable 5", 10.0, 50.0, False, True),
    "claude-mythos-5": ModelProfile("Mythos 5", 10.0, 50.0, False, True),
    "claude-opus-5": ModelProfile("Opus 5", 5.0, 25.0, False, True),
    "claude-opus-4-8": ModelProfile("Opus 4.8", 5.0, 25.0, False, False),
    "claude-opus-4-7": ModelProfile("Opus 4.7", 5.0, 25.0, False, False),
    "claude-opus-4-6": ModelProfile("Opus 4.6", 5.0, 25.0, True, False),
    "claude-opus-4-5": ModelProfile("Opus 4.5", 5.0, 25.0, True, False),
    "claude-sonnet-5": ModelProfile("Sonnet 5", 3.0, 15.0, False, True),
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
