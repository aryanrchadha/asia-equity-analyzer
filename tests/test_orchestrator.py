"""The multi-agent pipeline itself, driven through a fake API client."""

import unittest

from tests.support import FakeAnthropic, install_stubs, quiet

install_stubs()

import anthropic  # noqa: E402
import agents.orchestrator as orch  # noqa: E402
from prompts.section_prompts import SPECIALISTS  # noqa: E402


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeAnthropic()
        self._real_make_client = orch.make_client
        orch.make_client = lambda: self.client
        self._real_key = orch.ANTHROPIC_API_KEY
        orch.ANTHROPIC_API_KEY = "test-key"

    def tearDown(self):
        orch.make_client = self._real_make_client
        orch.ANTHROPIC_API_KEY = self._real_key

    def run_pipeline(self, text="FILING TEXT"):
        with quiet():
            return orch.analyze_document_multi(text)


class TestSystemPrompt(OrchestratorTest):
    def test_document_block_carries_the_cache_breakpoint(self):
        blocks = orch._build_system("FILING")
        assert "cache_control" not in blocks[0], "static context must precede the breakpoint"
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}
        assert "FILING" in blocks[1]["text"]

    def test_every_specialist_shares_a_byte_identical_prefix(self):
        # A differing prefix silently costs a full re-read per agent.
        self.run_pipeline()
        systems = [call["system"] for call in self.client.calls[:len(SPECIALISTS)]]
        assert all(s == systems[0] for s in systems)


class TestPipeline(OrchestratorTest):
    def test_runs_every_specialist_plus_synthesis(self):
        result = self.run_pipeline()
        assert len(self.client.calls) == len(SPECIALISTS) + 1
        assert len(result.sections) == len(SPECIALISTS) + 1
        assert result.sections[-1].key == "synthesis"

    def test_sections_keep_report_order_not_completion_order(self):
        keys = [s.key for s in self.run_pipeline().sections]
        assert keys == [s.key for s in SPECIALISTS] + ["synthesis"]

    def test_synthesis_receives_specialist_output_not_the_filing(self):
        self.client.responder = lambda kw: f"SECTION FOR {kw['messages'][0]['content'][:20]}"
        self.run_pipeline("SECRET FILING TEXT")
        synthesis = self.client.calls[-1]
        assert "SECRET FILING TEXT" not in synthesis["messages"][0]["content"]
        assert "SECRET FILING TEXT" not in str(synthesis["system"])

    def test_usage_is_summed_across_agents(self):
        result = self.run_pipeline()
        agents = len(SPECIALISTS) + 1
        assert result.input_tokens == 10 * agents
        assert result.output_tokens == 20 * agents
        # One agent warms the cache; the rest read it.
        assert result.cache_creation_tokens == 500
        assert result.cache_read_tokens == 500 * (agents - 1)

    def test_truncated_section_is_flagged(self):
        self.client.stop_reason = "max_tokens"
        assert all(s.truncated for s in self.run_pipeline().sections)

    def test_api_failure_exits_rather_than_returning_partial(self):
        self.client.error = anthropic.RateLimitError("slow down")
        with self.assertRaises(SystemExit) as caught:
            self.run_pipeline()
        assert caught.exception.code == 1


class TestConcurrentOutput(OrchestratorTest):
    """Specialists run in parallel, so their console output must not interleave."""

    def test_each_specialist_emits_exactly_one_locked_write(self):
        # Structural guarantee: one _log call per specialist, so there is no
        # window between writing the text and writing the newline.
        calls = []
        real_log = orch._log
        orch._log = calls.append
        try:
            with quiet():
                self.run_pipeline()
        finally:
            orch._log = real_log
        assert len(calls) == len(SPECIALISTS)
        assert all(message.startswith("   ✅") for message in calls)

    def test_truncation_warning_rides_the_same_write(self):
        self.client.stop_reason = "max_tokens"
        calls = []
        real_log = orch._log
        orch._log = calls.append
        try:
            with quiet():
                self.run_pipeline()
        finally:
            orch._log = real_log
        assert all("cut off" in message for message in calls)
        assert all(message.count("\n") == 1 for message in calls)

    # A timing-based test was tried here and deliberately removed: writes to
    # an in-memory stream don't interleave the way a real one does, so it
    # passed with the lock removed. The two structural tests above are the
    # real protection — they fail if the output goes back to separate prints.


class TestClientGuards(unittest.TestCase):
    def test_missing_key_exits_with_guidance(self):
        real = orch.ANTHROPIC_API_KEY
        orch.ANTHROPIC_API_KEY = None
        try:
            with self.assertRaises(SystemExit):
                orch.make_client()
        finally:
            orch.ANTHROPIC_API_KEY = real

    def test_api_errors_context_converts_to_exit(self):
        for error in (anthropic.APIConnectionError("down"),
                      anthropic.RateLimitError("429"),
                      anthropic.APIStatusError("500")):
            with self.assertRaises(SystemExit):
                with orch.api_errors():
                    raise error


if __name__ == "__main__":
    unittest.main()
