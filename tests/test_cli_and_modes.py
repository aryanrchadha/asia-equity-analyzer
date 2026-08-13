"""CLI contracts and end-to-end runs through main() with the API stubbed.

The validation tests matter because the flags interact: several combinations
would otherwise fail late, after the expensive part of a run.
"""

import io
import json
import os
import sys
import time
import unittest

from tests.support import (
    install_stubs, make_analysis, make_doc, quiet, temp_workspace,
)

install_stubs()

import main  # noqa: E402
from agents.orchestrator import SectionResult  # noqa: E402


def stub_section(text="## SECTION A: SHAPE\nbody"):
    return SectionResult(
        key="x", title="X", text=text, input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0, model="claude-sonnet-4-6",
        elapsed_seconds=0.1, truncated=False,
    )


class CliTest(unittest.TestCase):
    def parse(self, *argv):
        sys.argv = ["main.py", *argv]
        return main.parse_args()

    def assert_rejected(self, *argv):
        sys.argv = ["main.py", *argv]
        with self.assertRaises(SystemExit) as caught:
            with quiet():
                import contextlib
                with contextlib.redirect_stderr(io.StringIO()):
                    main.parse_args()
        assert caught.exception.code == 2


class TestArgumentValidation(CliTest):
    def test_group_modes_are_mutually_exclusive(self):
        self.assert_rejected("--compare", "a.pdf", "b.pdf", "--peers", "c.pdf", "d.pdf")
        self.assert_rejected("--batch", "d", "--peers", "c.pdf", "d.pdf")
        self.assert_rejected("--watch-dir", "d", "--batch", "e")

    def test_group_modes_need_at_least_two_filings(self):
        self.assert_rejected("--compare", "only.pdf")
        self.assert_rejected("--peers", "only.pdf")

    def test_group_modes_reject_single_agent_and_file(self):
        for mode in (("--compare", "a.pdf", "b.pdf"), ("--peers", "a.pdf", "b.pdf"),
                     ("--batch", "d"), ("--watch-dir", "d")):
            self.assert_rejected(*mode, "--single")
            self.assert_rejected(*mode, "--file", "x.pdf")

    def test_watch_needs_the_multi_agent_pipeline(self):
        # Only its prompts emit the SCORE lines the watchlist stores.
        self.assert_rejected("--watch", "--single")

    def test_named_watch_is_rejected_where_companies_differ(self):
        self.assert_rejected("--peers", "a.pdf", "b.pdf", "--watch", "acme")
        self.assert_rejected("--batch", "d", "--watch", "acme")
        self.assert_rejected("--watch-dir", "d", "--watch", "acme")
        assert self.parse("--batch", "d", "--watch").watch == ""

    def test_bare_watch_defaults_to_deriving_the_name(self):
        assert self.parse("--watch").watch == ""
        assert self.parse("--watch", "tencent").watch == "tencent"

    def test_numeric_bounds(self):
        self.assert_rejected("--alert-threshold", "-1")
        self.assert_rejected("--batch", "d", "--batch-limit", "1")
        self.assert_rejected("--watch-dir", "d", "--poll-interval", "0")

    def test_flags_that_require_their_mode(self):
        self.assert_rejected("--batch-limit", "5")
        self.assert_rejected("--once")

    def test_standalone_modes_refuse_to_be_combined(self):
        self.assert_rejected("--show-watchlist", "--file", "a.pdf")
        self.assert_rejected("--show-watchlist", "--batch", "d")
        self.assert_rejected("--export-html", "a.md", "--html")
        self.assert_rejected("--export-html", "a.md", "--watch")

    def test_html_composes_with_every_analysis_mode(self):
        assert self.parse("--file", "a.pdf", "--html").html
        assert self.parse("--batch", "d", "--html", "--watch").html


class ModeTest(unittest.TestCase):
    """Runs main() end to end with the pipeline stubbed."""

    def setUp(self):
        temp_workspace()
        self.analyses = []
        main.load_document = lambda filepath=None: make_doc(
            os.path.basename(filepath) if filepath else "solo.pdf"
        )

        def fake_pipeline(text, model=None):
            self.analyses.append(text)
            return make_analysis()

        main.analyze_document_multi = fake_pipeline
        main.analyze_trajectory = lambda *a, **k: stub_section()
        main.analyze_peers = lambda *a, **k: stub_section()
        main.analyze_sector = lambda *a, **k: stub_section()

        os.makedirs("filings", exist_ok=True)
        settled = time.time() - 100
        for name in ("alpha.pdf", "beta.pdf"):
            with open(f"filings/{name}", "w") as f:
                f.write(name)
            os.utime(f"filings/{name}", (settled, settled))

    def run_cli(self, *argv):
        sys.argv = ["main.py", *argv]
        with quiet() as out:
            main.main()
        return out.getvalue()

    def reports(self, suffix=".md"):
        return sorted(f for f in os.listdir("output") if f.endswith(suffix))


class TestAnalysisModes(ModeTest):
    def test_single_filing_writes_one_report(self):
        self.run_cli("--file", "solo.pdf")
        assert len(self.reports()) == 1
        assert len(self.analyses) == 1

    def test_compare_writes_a_report_per_period_plus_the_comparison(self):
        self.run_cli("--compare", "filings/alpha.pdf", "filings/beta.pdf")
        assert len(self.analyses) == 2
        assert any(r.startswith("comparison_") for r in self.reports())

    def test_peers_writes_the_ranked_report(self):
        self.run_cli("--peers", "filings/alpha.pdf", "filings/beta.pdf")
        assert any(r.startswith("peers_") for r in self.reports())

    def test_batch_writes_the_sector_report(self):
        self.run_cli("--batch", "filings")
        assert any(r.startswith("sector_") for r in self.reports())

    def test_batch_limit_drops_are_reported_not_silent(self):
        for extra in ("gamma.pdf", "delta.pdf"):
            with open(f"filings/{extra}", "w") as f:
                f.write(extra)
            settled = time.time() - 100
            os.utime(f"filings/{extra}", (settled, settled))
        out = self.run_cli("--batch", "filings", "--batch-limit", "2")
        assert len(self.analyses) == 2
        assert "beyond --batch-limit 2" in out
        sector = [r for r in self.reports() if r.startswith("sector_")][0]
        with open(f"output/{sector}") as f:
            body = f.read()
        assert "## Skipped Filings (2)" in body

    def test_group_modes_fail_before_spending_on_a_bad_path(self):
        main.load_document = lambda filepath=None: None
        with self.assertRaises(SystemExit):
            self.run_cli("--peers", "filings/alpha.pdf", "missing.pdf")
        assert self.analyses == [], "no filing should be analyzed before validation"


class TestHtmlAndWatchlistIntegration(ModeTest):
    def test_html_flag_produces_a_page_per_report(self):
        self.run_cli("--batch", "filings", "--html")
        assert {r[:-3] for r in self.reports()} == {r[:-5] for r in self.reports(".html")}

    def test_no_html_without_the_flag(self):
        self.run_cli("--file", "solo.pdf")
        assert self.reports(".html") == []

    def test_watch_records_scores_and_alerts_on_the_next_run(self):
        self.run_cli("--file", "solo.pdf", "--watch", "acme", "--watchlist-path", "w.json")
        main.analyze_document_multi = lambda text, model=None: make_analysis(
            __import__("tests.support", fromlist=["x"]).build_report(composite="2.50")
        )
        out = self.run_cli("--file", "solo.pdf", "--watch", "acme", "--watchlist-path", "w.json")
        assert "composite deteriorated 3.9 → 2.5" in out
        with open("w.json") as f:
            assert len(json.load(f)["companies"]["acme"]["entries"]) == 2

    def test_peers_records_each_company_separately(self):
        self.run_cli("--peers", "filings/alpha.pdf", "filings/beta.pdf",
                     "--watch", "--watchlist-path", "w.json")
        with open("w.json") as f:
            assert sorted(json.load(f)["companies"]) == ["alpha", "beta"]

    def test_show_watchlist_makes_no_api_calls(self):
        self.run_cli("--file", "solo.pdf", "--watch", "x", "--watchlist-path", "w.json")
        self.analyses.clear()
        out = self.run_cli("--show-watchlist", "--watchlist-path", "w.json")
        assert "| x |" in out and self.analyses == []

    def test_export_html_makes_no_api_calls(self):
        self.run_cli("--file", "solo.pdf")
        report = f"output/{self.reports()[0]}"
        self.analyses.clear()
        self.run_cli("--export-html", report)
        assert os.path.exists(report[:-3] + ".html") and self.analyses == []

    def test_export_html_reports_missing_files(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--export-html", "output/nope.md")
        assert caught.exception.code == 1


class TestWatchDir(ModeTest):
    def test_analyzes_new_filings_then_leaves_them_alone(self):
        out = self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
        assert len(self.analyses) == 2 and "Analyzed this run:  2" in out
        self.analyses.clear()
        self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
        assert self.analyses == [], "already-analyzed filings must not be re-billed"

    def test_duplicate_content_is_analyzed_once(self):
        with open("filings/alpha_copy.pdf", "w") as f:
            f.write("alpha.pdf")
        settled = time.time() - 100
        os.utime("filings/alpha_copy.pdf", (settled, settled))
        out = self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
        assert len(self.analyses) == 2
        assert "1 duplicate(s)" in out

    def test_unsettled_files_are_left_for_the_next_pass(self):
        with open("filings/landing.pdf", "w") as f:
            f.write("still copying")
        out = self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
        assert "1 file(s) still being written" in out
        assert len(self.analyses) == 2

    def test_failures_retry_then_give_up(self):
        def boom(text, model=None):
            self.analyses.append(text)
            raise SystemExit(1)

        main.analyze_document_multi = boom
        for attempt in (1, 2, 3):
            self.analyses.clear()
            out = self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
            assert len(self.analyses) == 2
            assert ("will retry" in out) if attempt < 3 else ("giving up" in out)
        self.analyses.clear()
        self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "l.json")
        assert self.analyses == [], "exhausted filings must not be retried forever"

    def test_corrupt_ledger_aborts_instead_of_re_billing(self):
        with open("bad.json", "w") as f:
            f.write("{not json")
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--watch-dir", "filings", "--once", "--ledger-path", "bad.json")
        assert caught.exception.code == 1
        assert self.analyses == []
        with open("bad.json") as f:
            assert f.read() == "{not json"

    def test_empty_inbox_is_not_reported_as_an_error(self):
        # Regression: an empty inbox is a watcher's normal steady state, but
        # every poll logged "No .pdf or .txt files found" — 1,440 error lines
        # a day at the default interval, for a healthy setup.
        os.makedirs("empty_inbox")
        out = self.run_cli("--watch-dir", "empty_inbox", "--once", "--ledger-path", "l.json")
        assert "No .pdf or .txt files found" not in out
        assert "❌" not in out

    def test_missing_directory_still_fails_loudly_at_startup(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--watch-dir", "nope", "--once", "--ledger-path", "l.json")
        assert caught.exception.code == 1

    def test_html_export_failure_does_not_kill_the_watcher(self):
        # Regression: an OSError from the export propagated out of the loop,
        # killing an unattended watcher after the analysis was already paid
        # for — and before the ledger recorded it, so a restart re-billed it.
        def failing_export(path, output_path=None):
            raise OSError(28, "No space left on device")

        real_export = main.export_markdown_file
        main.export_markdown_file = failing_export
        try:
            out = self.run_cli("--watch-dir", "filings", "--once", "--html",
                               "--ledger-path", "l.json")
        finally:
            main.export_markdown_file = real_export
        assert "HTML export failed" in out
        assert len(self.analyses) == 2, "analysis should still have run"
        with open("l.json") as f:
            assert len(json.load(f)["files"]) == 2, "paid-for work must be recorded"
        assert len([f for f in os.listdir("output") if f.endswith(".md")]) == 2

    def test_continuous_mode_stops_cleanly_on_interrupt(self):
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            if len(slept) >= 2:
                raise KeyboardInterrupt

        real_sleep = main.time.sleep
        main.time.sleep = fake_sleep
        try:
            out = self.run_cli("--watch-dir", "filings", "--poll-interval", "0.01",
                               "--ledger-path", "l.json")
        finally:
            main.time.sleep = real_sleep
        assert "Stopped." in out and "WATCH SUMMARY" in out


if __name__ == "__main__":
    unittest.main()
