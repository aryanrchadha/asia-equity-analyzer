"""Model-aware request parameters and pricing.

Regression tests for requests that newer models reject: every call used to
send temperature=1.0, which Opus 4.7+, Opus 5, and Fable 5 answer with a 400 —
so even the usage example in main.py's own docstring failed on its first call.
"""

import unittest

from tests.support import FakeAnthropic, install_stubs, quiet

install_stubs()

import agents.analyst as analyst  # noqa: E402
import agents.orchestrator as orch  # noqa: E402
import main  # noqa: E402
from prompts.section_prompts import SPECIALISTS  # noqa: E402
from utils.models import (  # noqa: E402
    THINKING_OUTPUT_BUDGET, pricing_for, profile_for, request_params, temperature_ignored,
)

REJECTS_SAMPLING = ("claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
                    "claude-sonnet-5", "claude-fable-5")
ACCEPTS_SAMPLING = ("claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4-6")


class TestProfileLookup(unittest.TestCase):
    def test_exact_ids(self):
        assert profile_for("claude-opus-4-8").family == "Opus 4.8"
        assert profile_for("claude-sonnet-4-6").family == "Sonnet 4.6"

    def test_dated_snapshots_and_bedrock_prefixes(self):
        assert profile_for("claude-haiku-4-5-20251001").family == "Haiku 4.5"
        assert profile_for("anthropic.claude-opus-5").family == "Opus 5"
        assert profile_for("CLAUDE-SONNET-4-6").family == "Sonnet 4.6"

    def test_longest_prefix_wins(self):
        # "claude-opus-4-8" must not resolve through a shorter opus prefix.
        assert profile_for("claude-opus-4-8").family == "Opus 4.8"
        assert profile_for("claude-opus-4-6").family == "Opus 4.6"

    def test_unknown_models(self):
        assert profile_for("claude-2.1") is None
        assert profile_for("gpt-4") is None
        assert profile_for("") is None and profile_for(None) is None


class TestRequestParams(unittest.TestCase):
    def test_default_temperature_is_never_sent(self):
        # 1.0 is the API default: sending it changes nothing where accepted
        # and is a hard 400 where not.
        for model in REJECTS_SAMPLING + ACCEPTS_SAMPLING + ("mystery-model",):
            assert "temperature" not in request_params(model, 4000, 1.0), model

    def test_custom_temperature_reaches_models_that_accept_it(self):
        for model in ACCEPTS_SAMPLING:
            assert request_params(model, 4000, 0.3)["temperature"] == 0.3, model

    def test_custom_temperature_is_withheld_from_models_that_reject_it(self):
        for model in REJECTS_SAMPLING:
            assert "temperature" not in request_params(model, 4000, 0.3), model
            assert temperature_ignored(model, 0.3), model

    def test_unknown_models_get_the_conservative_request(self):
        assert "temperature" not in request_params("mystery-model", 4000, 0.3)
        assert temperature_ignored("mystery-model", 0.3)

    def test_thinking_models_get_room_to_think_and_still_answer(self):
        # Thinking shares max_tokens with the answer, so 4,000 truncates.
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5"):
            assert request_params(model, 4000, 1.0)["max_tokens"] == THINKING_OUTPUT_BUDGET

    def test_budget_is_never_lowered(self):
        assert request_params("claude-opus-5", 20_000, 1.0)["max_tokens"] == 20_000
        assert request_params("claude-sonnet-4-6", 4000, 1.0)["max_tokens"] == 4000

    def test_thinking_budget_stays_under_the_non_streaming_ceiling(self):
        # The SDK refuses non-streaming requests it expects to exceed ~10
        # minutes (~21K output tokens); the pipeline does not stream.
        assert THINKING_OUTPUT_BUDGET <= 20_000


class TestPricing(unittest.TestCase):
    def test_known_models_use_their_own_rates(self):
        assert pricing_for("claude-opus-4-8", (3.0, 15.0)) == (5.0, 25.0, True)
        assert pricing_for("claude-haiku-4-5-20251001", (3.0, 15.0)) == (1.0, 5.0, True)

    def test_unknown_models_fall_back_and_say_so(self):
        assert pricing_for("mystery-model", (3.0, 15.0)) == (3.0, 15.0, False)
        assert not main.pricing_known("mystery-model")

    def test_estimate_prices_opus_at_opus_rates(self):
        # 1M input + 1M output on Opus 4.8 is $5 + $25, not the Sonnet $3 + $15.
        assert main.estimate_cost(1_000_000, 1_000_000, model="claude-opus-4-8") == 30.0
        assert main.estimate_cost(1_000_000, 1_000_000, model="claude-sonnet-4-6") == 18.0

    def test_cache_rates_derive_from_the_model(self):
        cost = main.estimate_cost(0, 0, 1_000_000, 1_000_000, model="claude-opus-4-8")
        assert abs(cost - (5.0 * 1.25 + 5.0 * 0.10)) < 1e-9

    def test_unknown_model_uses_the_config_constants(self):
        from config import (CACHE_READ_COST_PER_MILLION, CACHE_WRITE_COST_PER_MILLION,
                            INPUT_TOKEN_COST_PER_MILLION, OUTPUT_TOKEN_COST_PER_MILLION)
        expected = (INPUT_TOKEN_COST_PER_MILLION + OUTPUT_TOKEN_COST_PER_MILLION
                    + CACHE_WRITE_COST_PER_MILLION + CACHE_READ_COST_PER_MILLION)
        cost = main.estimate_cost(1_000_000, 1_000_000, 1_000_000, 1_000_000, model="x")
        assert abs(cost - expected) < 1e-9


class PipelineRequestTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeAnthropic()
        self.patches = [(orch, "make_client", lambda: self.client),
                        (orch, "ANTHROPIC_API_KEY", "k"),
                        (analyst, "ANTHROPIC_API_KEY", "k")]
        self.saved = [(mod, name, getattr(mod, name)) for mod, name, _ in self.patches]
        for mod, name, value in self.patches:
            setattr(mod, name, value)
        self.real_anthropic = analyst.anthropic.Anthropic
        analyst.anthropic.Anthropic = lambda **kw: self.client

    def tearDown(self):
        for mod, name, value in self.saved:
            setattr(mod, name, value)
        analyst.anthropic.Anthropic = self.real_anthropic


class TestPipelineRequests(PipelineRequestTest):
    def test_no_pipeline_call_sends_temperature_to_opus(self):
        real = orch.TEMPERATURE
        orch.TEMPERATURE = 0.3   # even a deliberately configured value
        try:
            with quiet() as out:
                orch.analyze_document_multi("doc", model="claude-opus-4-8")
        finally:
            orch.TEMPERATURE = real
        assert len(self.client.calls) == len(SPECIALISTS) + 1
        assert all("temperature" not in call for call in self.client.calls)
        assert "is not sent" in out.getvalue()

    def test_pipeline_budgets_thinking_models(self):
        with quiet():
            orch.analyze_document_multi("doc", model="claude-opus-5")
        assert all(c["max_tokens"] >= THINKING_OUTPUT_BUDGET for c in self.client.calls)

    def test_pipeline_still_sends_custom_temperature_where_accepted(self):
        real = orch.TEMPERATURE
        orch.TEMPERATURE = 0.3
        try:
            with quiet():
                orch.analyze_document_multi("doc", model="claude-sonnet-4-6")
        finally:
            orch.TEMPERATURE = real
        assert all(call.get("temperature") == 0.3 for call in self.client.calls)

    def test_single_agent_mode_uses_the_same_rules(self):
        self.client.responder = "## SECTION 1: X\nbody"
        with quiet():
            analyst.analyze_document("doc", model="claude-opus-4-8", max_tokens=8000)
        assert "temperature" not in self.client.calls[0]


class TestRefusals(PipelineRequestTest):
    def test_a_declined_specialist_is_flagged_not_silently_empty(self):
        # Only the margins specialist is declined, read from the real
        # stop_reason on its response.
        def stop_reason(kwargs):
            return "refusal" if "SECTION 3: MARGIN" in kwargs["messages"][0]["content"] else "end_turn"

        def responder(kwargs):
            return "" if stop_reason(kwargs) == "refusal" else "section text"

        self.client.stop_reason = stop_reason
        self.client.responder = responder
        with quiet() as out:
            result = orch.analyze_document_multi("doc")
        assert result.incomplete_sections == [("Margin Analysis", "declined by the model")]
        assert "declined by the model" in out.getvalue()
        assert [s.refused for s in result.sections].count(True) == 1

    def test_every_agent_declining_writes_nothing(self):
        self.client.stop_reason = "refusal"
        self.client.responder = ""
        with quiet() as out:
            with self.assertRaises(SystemExit) as caught:
                orch.analyze_document_multi("doc")
        assert caught.exception.code == 1
        assert "no report written" in out.getvalue()

    def test_single_agent_refusal_with_no_text_exits(self):
        self.client.stop_reason = "refusal"
        self.client.responder = ""
        with quiet():
            with self.assertRaises(SystemExit):
                analyst.analyze_document("doc")

    def test_single_agent_partial_refusal_keeps_the_text(self):
        self.client.stop_reason = "refusal"
        self.client.responder = "## SECTION 1: X\npartial"
        with quiet() as out:
            result = analyst.analyze_document("doc")
        assert result.text.startswith("## SECTION 1")
        assert result.incomplete_sections == [("Analyst", "declined by the model")]
        assert "incomplete" in out.getvalue()

    def test_truncation_is_reported_as_incomplete(self):
        self.client.stop_reason = "max_tokens"
        self.client.responder = "section text"
        with quiet():
            result = orch.analyze_document_multi("doc")
        assert len(result.incomplete_sections) == len(SPECIALISTS) + 1
        assert all(r == "cut off at the output-token limit"
                   for _, r in result.incomplete_sections)


class TestIncompleteReports(unittest.TestCase):
    def setUp(self):
        from tests.support import temp_workspace
        temp_workspace()

    def write(self, **kwargs):
        from utils.report_writer import write_report
        with quiet():
            path = write_report(analysis_text="## SECTION 1: X\nbody", source_filename="a.pdf",
                                input_tokens=1, output_tokens=1, elapsed_seconds=1.0,
                                estimated_cost=0.1, **kwargs)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_incomplete_sections_are_stated_in_the_saved_report(self):
        body = self.write(incomplete_sections=[("Margin Analysis", "declined by the model")])
        assert "This analysis is incomplete" in body
        assert "**Margin Analysis** — declined by the model" in body
        import yaml
        front = yaml.safe_load(body.split("---")[1])
        assert front["incomplete_sections"] == ["Margin Analysis"]

    def test_complete_reports_carry_no_warning(self):
        body = self.write()
        assert "incomplete" not in body.split("---", 2)[2]

    def test_unknown_model_pricing_footnote(self):
        body = self.write(model="mystery-model", pricing_uncertain=True)
        assert "No published pricing on file" in body


if __name__ == "__main__":
    unittest.main()
