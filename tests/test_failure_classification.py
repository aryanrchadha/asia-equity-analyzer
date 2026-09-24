"""Failures that would hit every filing must not be blamed on one filing.

Regression tests for a watch-mode bug: with no API key configured, every
filing "failed", and each failure counted against that file's retry budget.
After three polls every filing in the inbox was permanently abandoned. Adding
the key then produced "Analyzed this run: 0" — the files were never retried.
"""

import json
import os
import time
import unittest

from tests.support import install_stubs, quiet

install_stubs()

import anthropic  # noqa: E402

import agents.orchestrator as orch  # noqa: E402
import main  # noqa: E402
from agents.orchestrator import PipelineError  # noqa: E402
from tests.test_cli_and_modes import ModeTest  # noqa: E402
from utils.file_ledger import clear_failures, empty_ledger, mark_analyzed, mark_failed  # noqa: E402


def classify(error) -> PipelineError:
    try:
        with quiet(), orch.api_errors():
            raise error
    except PipelineError as e:
        return e
    raise AssertionError("api_errors() did not convert the error")


class TestClassification(unittest.TestCase):
    def test_config_and_outage_errors_are_systemic(self):
        for error in (anthropic.APIConnectionError("down"),
                      anthropic.RateLimitError("429", status_code=429),
                      anthropic.APIStatusError("bad key", status_code=401),
                      anthropic.APIStatusError("forbidden", status_code=403),
                      anthropic.APIStatusError("no such model", status_code=404),
                      anthropic.APIStatusError("overloaded", status_code=529),
                      anthropic.APIStatusError("server", status_code=500)):
            assert classify(error).systemic, error

    def test_a_bad_request_is_blamed_on_the_filing(self):
        # A 400 is typically an oversized or malformed document.
        assert not classify(anthropic.APIStatusError("too long", status_code=400)).systemic

    def test_missing_key_is_systemic(self):
        real = orch.ANTHROPIC_API_KEY
        orch.ANTHROPIC_API_KEY = None
        try:
            with quiet(), self.assertRaises(PipelineError) as caught:
                orch.make_client()
        finally:
            orch.ANTHROPIC_API_KEY = real
        assert caught.exception.systemic and caught.exception.code == 1


class TestWatchModeFailures(ModeTest):
    ARGS = ("--watch-dir", "filings", "--once", "--ledger-path", "l.json")

    def fail_with(self, error):
        def pipeline(text, model=None):
            self.analyses.append(text)
            raise error
        main.analyze_document_multi = pipeline

    def ledger(self):
        if not os.path.exists("l.json"):
            return {}
        with open("l.json") as f:
            return json.load(f)["files"]

    def test_systemic_failure_is_not_counted_against_filings(self):
        self.fail_with(PipelineError("no API key configured", systemic=True))
        for _ in range(5):
            self.analyses.clear()
            out = self.run_cli(*self.ARGS)
            assert "Pausing this pass" in out
            assert len(self.analyses) == 1, "the rest of the queue should not be attempted"
        assert "giving up" not in out
        assert self.ledger() == {}, "nothing about the filings should be recorded"

    def test_filings_are_analyzed_once_the_problem_is_fixed(self):
        self.fail_with(PipelineError("rate limited", systemic=True))
        for _ in range(5):
            self.run_cli(*self.ARGS)
        main.analyze_document_multi = self.working_pipeline
        out = self.run_cli(*self.ARGS)
        assert "Analyzed this run:  2" in out

    def test_file_specific_failure_still_counts(self):
        self.fail_with(PipelineError("API error 400", systemic=False))
        out = self.run_cli(*self.ARGS)
        assert "will retry" in out and len(self.analyses) == 2
        assert all(e["attempts"] == 1 for e in self.ledger().values())

    def test_retry_failed_clears_failures_but_keeps_successes(self):
        self.fail_with(PipelineError("API error 400", systemic=False))
        for _ in range(3):
            self.run_cli(*self.ARGS)
        self.analyses.clear()
        self.run_cli(*self.ARGS)
        assert self.analyses == [], "exhausted filings are left alone by default"

        main.analyze_document_multi = self.working_pipeline
        out = self.run_cli(*self.ARGS, "--retry-failed")
        assert "Cleared 2 recorded failure(s)" in out
        assert "Analyzed this run:  2" in out

        self.analyses.clear()
        out = self.run_cli(*self.ARGS, "--retry-failed")
        assert "Cleared 0 recorded failure(s)" in out
        assert self.analyses == [], "successful analyses must never be re-billed"

    def test_retry_failed_requires_watch_dir(self):
        with quiet(), self.assertRaises(SystemExit) as caught:
            self.run_cli("--file", "solo.pdf", "--retry-failed")
        assert caught.exception.code == 2


class TestBatchFailures(ModeTest):
    def test_systemic_failure_stops_the_batch_immediately(self):
        def pipeline(text, model=None):
            self.analyses.append(text)
            raise PipelineError("API error 401", systemic=True)
        main.analyze_document_multi = pipeline
        with self.assertRaises(SystemExit):
            self.run_cli("--batch", "filings")
        assert len(self.analyses) == 1, "a bad key would fail every filing; stop at the first"

    def test_file_specific_failure_moves_on(self):
        settled = time.time() - 100
        with open("filings/gamma.pdf", "w") as f:
            f.write("gamma.pdf")
        os.utime("filings/gamma.pdf", (settled, settled))
        calls = []

        def pipeline(text, model=None):
            calls.append(text)
            if len(calls) == 1:
                raise PipelineError("API error 400", systemic=False)
            return self.working_pipeline(text, model)
        main.analyze_document_multi = pipeline
        out = self.run_cli("--batch", "filings")
        assert len(calls) == 3, "one bad filing must not stop the batch"
        assert "API error 400" in out


class TestFailFastCredentials(ModeTest):
    def test_no_key_exits_before_any_work(self):
        main.has_credentials = lambda: False
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
        assert caught.exception.code == 1
        assert self.analyses == []
        assert not os.path.exists("l.json"), "no ledger should be created"

    def test_dry_run_still_works_without_a_key(self):
        main.has_credentials = lambda: False
        out = self.run_cli("--dry-run", "--batch", "filings")
        assert "alpha.pdf" in out and self.analyses == []


class TestClearFailures(unittest.TestCase):
    def test_only_failures_are_removed(self):
        ledger = empty_ledger()
        mark_analyzed(ledger, "a", "a.pdf", "output/a.md")
        mark_failed(ledger, "b", "b.pdf", "API error 400")
        mark_failed(ledger, "c", "c.pdf", "could not be loaded")
        assert clear_failures(ledger) == 2
        assert list(ledger["files"]) == ["a"]


if __name__ == "__main__":
    unittest.main()
