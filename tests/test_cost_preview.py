"""--dry-run: pre-run cost estimates that must never touch the API."""

import os
import sys
import time
import unittest

from tests.support import install_stubs, make_doc, quiet, temp_workspace

install_stubs()

import main  # noqa: E402
from utils.cost_preview import (  # noqa: E402
    CACHE_MIN_TOKENS, NEW_TOKENIZER_FACTOR, Estimate, estimate_group_agent,
    estimate_pipeline, estimate_single, estimate_tokens,
)

ENGLISH = "Revenue grew 12% to HKD 4,182m on subscription strength. " * 400


class TestTokenEstimate(unittest.TestCase):
    def test_latin_text_errs_high(self):
        # ~3.5 chars/token is the low end for English prose, so the estimate
        # should not undercount a typical 4-chars/token document.
        assert estimate_tokens("x" * 3500) == 1000
        assert estimate_tokens(ENGLISH) >= len(ENGLISH) / 4

    def test_cjk_counts_a_token_per_character(self):
        assert estimate_tokens("营业收入同比增长") == 8
        assert estimate_tokens("네이버") == 3 and estimate_tokens("サムスン") == 4

    def test_newer_tokenizer_models_are_scaled_up(self):
        base = estimate_tokens("x" * 35_000, "claude-sonnet-4-6")
        assert estimate_tokens("x" * 35_000, "claude-opus-4-8") == round(base * NEW_TOKENIZER_FACTOR)


class TestPipelineEstimate(unittest.TestCase):
    def test_ceiling_is_never_below_expected(self):
        for model in ("claude-sonnet-4-6", "claude-opus-5", "claude-haiku-4-5", "unknown"):
            e = estimate_pipeline(ENGLISH, model)
            assert 0 < e.expected < e.ceiling, model

    def test_thinking_models_carry_their_larger_budget_into_the_ceiling(self):
        # Opus 5 gets a 16K budget per agent; the preview must reflect that
        # rather than quote the 4K budget it would never actually use.
        sonnet = estimate_pipeline(ENGLISH, "claude-sonnet-4-6").ceiling
        opus5 = estimate_pipeline(ENGLISH, "claude-opus-5").ceiling
        assert opus5 > sonnet * 4

    def test_caching_assumed_only_above_every_models_minimum(self):
        assert not estimate_pipeline("short filing", "claude-sonnet-4-6").cached
        assert estimate_pipeline("x" * (CACHE_MIN_TOKENS * 4), "claude-sonnet-4-6").cached

    def test_cost_grows_with_the_document(self):
        small = estimate_pipeline("x" * 50_000, "claude-sonnet-4-6")
        large = estimate_pipeline("x" * 500_000, "claude-sonnet-4-6")
        assert large.expected > small.expected and large.input_tokens > small.input_tokens

    def test_caching_makes_the_six_readers_cheap(self):
        # Five of six specialists read the document at 0.1x. A 100K-token
        # document should therefore cost far less than six full-price reads.
        doc = "x" * 350_000     # ~100K tokens
        e = estimate_pipeline(doc, "claude-sonnet-4-6")
        six_full_reads = 6 * 100_000 * 3.0 / 1_000_000
        assert e.expected < six_full_reads

    def test_single_agent_is_cheaper_than_the_pipeline(self):
        single = estimate_single(ENGLISH, "claude-sonnet-4-6", 8000)
        pipeline = estimate_pipeline(ENGLISH, "claude-sonnet-4-6")
        assert single.ceiling < pipeline.ceiling

    def test_group_agent_scales_with_filing_count(self):
        few = estimate_group_agent(2, "instructions", "claude-sonnet-4-6")
        many = estimate_group_agent(20, "instructions", "claude-sonnet-4-6")
        assert many.input_tokens > few.input_tokens and many.expected > few.expected

    def test_estimates_add(self):
        total = Estimate(10, 1.0, 2.0) + Estimate(5, 0.5, 1.0, cached=False)
        assert (total.input_tokens, total.expected, total.ceiling, total.cached) == (15, 1.5, 3.0, False)


class TestDisplayWidth(unittest.TestCase):
    def test_cjk_counts_two_columns(self):
        assert main._display_width("abc") == 3
        assert main._display_width("腾讯") == 4
        assert main._display_width("네이버") == 6

    def test_fit_pads_to_terminal_columns_not_characters(self):
        # Regression: padding by character count pushed CJK rows two columns
        # right per character, breaking the table.
        for name in ("acme.pdf", "腾讯控股年报2024.txt", "サムスン2024.pdf"):
            assert main._display_width(main._fit(name, 34)) == 34, name

    def test_fit_truncates_without_overflowing(self):
        for name in ("a" * 50, "腾" * 30):
            fitted = main._fit(name, 20)
            assert main._display_width(fitted) == 20 and fitted.rstrip().endswith("...")


class DryRunTest(unittest.TestCase):
    def setUp(self):
        temp_workspace()
        self.api_calls = []

        def forbidden(*args, **kwargs):
            self.api_calls.append(args)
            raise AssertionError("a dry run must never call the API")

        self.saved = (main.analyze_document_multi, main.analyze_document, main.load_document)
        main.analyze_document_multi = forbidden
        main.analyze_document = forbidden
        main.load_document = lambda filepath=None: (
            None if filepath and "missing" in filepath
            else make_doc(os.path.basename(filepath or "latest.pdf"), ENGLISH)
        )
        os.makedirs("filings")
        settled = time.time() - 100
        for name in ("alpha.pdf", "beta.pdf", "gamma.pdf"):
            with open(f"filings/{name}", "w") as f:
                f.write(name)
            os.utime(f"filings/{name}", (settled, settled))

    def tearDown(self):
        main.analyze_document_multi, main.analyze_document, main.load_document = self.saved

    def run_cli(self, *argv):
        sys.argv = ["main.py", *argv, "--dry-run"]
        with quiet() as out:
            main.main()
        return out.getvalue()


class TestDryRun(DryRunTest):
    def test_every_mode_previews_without_calling_the_api(self):
        for argv in (("--file", "a.pdf"), ("--single", "--file", "a.pdf"),
                     ("--compare", "filings/alpha.pdf", "filings/beta.pdf"),
                     ("--peers", "filings/alpha.pdf", "filings/beta.pdf"),
                     ("--batch", "filings"),
                     ("--watch-dir", "filings", "--ledger-path", "l.json")):
            out = self.run_cli(*argv)
            assert "Dry Run" in out and "Total" in out, argv
        assert self.api_calls == []

    def test_nothing_is_written(self):
        self.run_cli("--batch", "filings", "--html", "--watch", "--watchlist-path", "w.json")
        self.run_cli("--watch-dir", "filings", "--ledger-path", "l.json")
        assert not os.path.exists("output")
        assert not os.path.exists("w.json")
        assert not os.path.exists("l.json"), "the ledger must stay read-only"

    def test_group_modes_include_their_final_agent(self):
        assert "Sector agent" in self.run_cli("--batch", "filings")
        assert "Peer agent" in self.run_cli("--peers", "filings/alpha.pdf", "filings/beta.pdf")
        assert "Comparison agent" in self.run_cli("--compare", "filings/alpha.pdf", "filings/beta.pdf")

    def test_batch_limit_is_applied_and_stated(self):
        out = self.run_cli("--batch", "filings", "--batch-limit", "2")
        assert "Total (2 filings)" in out and "1 filing(s) beyond --batch-limit 2" in out

    def test_a_doomed_group_run_exits_nonzero(self):
        # --compare/--peers stop at the first unloadable file, so the preview
        # should fail the same way — useful when scripting.
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("--compare", "filings/alpha.pdf", "missing.pdf")
        assert caught.exception.code == 1

    def test_batch_skips_unreadable_files_like_the_real_run(self):
        real_loader = main.load_document
        main.load_document = lambda filepath=None: (
            None if "beta" in filepath else real_loader(filepath)
        )
        out = self.run_cli("--batch", "filings")   # does not raise
        assert "Total (2 filings)" in out and "beta.pdf" in out

    def test_watch_dir_preview_respects_the_ledger(self):
        from utils.file_ledger import empty_ledger, file_digest, mark_analyzed, save_ledger
        ledger = empty_ledger()
        mark_analyzed(ledger, file_digest("filings/alpha.pdf"), "alpha.pdf", "r.md", recorded_at="t")
        save_ledger(ledger, "l.json")
        out = self.run_cli("--watch-dir", "filings", "--ledger-path", "l.json")
        assert "Total (2 filings)" in out and "1 file(s) already analyzed" in out

    def test_unknown_model_says_it_is_using_fallback_pricing(self):
        assert "no published pricing on file" in self.run_cli("--file", "a.pdf", "--model", "mystery")

    def test_cannot_combine_with_other_free_modes(self):
        import contextlib
        import io
        for extra in (["--show-watchlist"], ["--export-html", "a.md"]):
            sys.argv = ["main.py", "--dry-run", *extra]
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                main.parse_args()
            assert caught.exception.code == 2


if __name__ == "__main__":
    unittest.main()
