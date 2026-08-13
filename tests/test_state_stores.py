"""Watchlist and processed-file ledger: alerts, retries, and durability.

Both hold state that is expensive to rebuild — a score history, and the record
of what has already been paid for — so the durability rules are tested as
hard as the behavior.
"""

import json
import os
import time
import unittest

from tests.support import install_stubs, temp_workspace

install_stubs()

from utils.file_ledger import (  # noqa: E402
    LedgerError, empty_ledger, file_digest, is_stable, ledger_summary,
    load_ledger, mark_analyzed, mark_failed, pending_files, save_ledger, status_of,
)
from utils.score_parser import ScoreCard  # noqa: E402
from utils.watchlist import (  # noqa: E402
    WatchlistError, empty_watchlist, format_watchlist_table, load_watchlist,
    record_analysis, save_watchlist,
)


class TestWatchlistAlerts(unittest.TestCase):
    def setUp(self):
        self.data = empty_watchlist()
        record_analysis(self.data, "acme", "fy23.pdf", "r.md", "m",
                        ScoreCard(composite=4.0, governance="STRONG"), recorded_at="t1")

    def record(self, composite=None, governance=None, threshold=0.5):
        return record_analysis(
            self.data, "acme", "fy24.pdf", "r.md", "m",
            ScoreCard(composite=composite, governance=governance),
            recorded_at="t2", threshold=threshold,
        )

    def test_first_entry_never_alerts(self):
        assert record_analysis(empty_watchlist(), "x", "f", "r", "m",
                               ScoreCard(composite=1.0), recorded_at="t") == []

    def test_composite_drop_beyond_threshold_is_adverse(self):
        alerts = self.record(composite=3.2, governance="STRONG")
        assert [a.kind for a in alerts] == ["composite"]
        assert alerts[0].adverse and "-0.8" in alerts[0].message

    def test_composite_rise_is_not_adverse(self):
        alerts = self.record(composite=4.9, governance="STRONG")
        assert alerts[0].kind == "composite" and not alerts[0].adverse

    def test_move_below_threshold_is_silent(self):
        assert self.record(composite=4.2, governance="STRONG") == []

    def test_threshold_is_inclusive(self):
        assert [a.kind for a in self.record(composite=4.5, governance="STRONG")] == ["composite"]

    def test_governance_change_always_alerts(self):
        alerts = self.record(composite=4.0, governance="CONCERNING")
        assert [a.kind for a in alerts] == ["governance"]
        assert alerts[0].adverse and "STRONG → CONCERNING" in alerts[0].message

    def test_governance_upgrade_is_favorable(self):
        self.data["companies"]["acme"]["entries"][0]["scores"]["governance"] = "RED FLAG"
        alerts = self.record(composite=4.0, governance="ADEQUATE")
        assert not alerts[0].adverse

    def test_missing_composite_on_either_side_is_silent(self):
        assert self.record(composite=None, governance="STRONG") == []

    def test_table_flags_only_moves_past_threshold(self):
        self.record(composite=3.2, governance="STRONG")
        table = format_watchlist_table(self.data, threshold=0.5)
        assert "| acme |" in table and "-0.8" in table and "⚠️" in table
        assert format_watchlist_table(empty_watchlist()) == "_Watchlist is empty._"


class TestLedgerIdentity(unittest.TestCase):
    def setUp(self):
        temp_workspace()
        self.old = time.time() - 100

    def write(self, name, body, settled=True):
        with open(name, "w") as f:
            f.write(body)
        if settled:
            os.utime(name, (self.old, self.old))
        return name

    def test_identity_is_content_not_path(self):
        self.write("a.pdf", "SAME")
        self.write("b.pdf", "SAME")
        self.write("c.pdf", "OTHER")
        assert file_digest("a.pdf") == file_digest("b.pdf") != file_digest("c.pdf")

    def test_renamed_copy_of_analyzed_filing_is_not_re_billed(self):
        self.write("a.pdf", "SAME")
        self.write("b.pdf", "SAME")
        ledger = empty_ledger()
        mark_analyzed(ledger, file_digest("a.pdf"), "a.pdf", "r.md", recorded_at="t")
        pending, _, _ = pending_files(["a.pdf", "b.pdf"], ledger, settle_seconds=10)
        assert pending == []

    def test_amended_filing_reusing_its_name_is_new_work(self):
        self.write("a.pdf", "ORIGINAL")
        ledger = empty_ledger()
        mark_analyzed(ledger, file_digest("a.pdf"), "a.pdf", "r.md", recorded_at="t")
        self.write("a.pdf", "AMENDED")
        pending, _, _ = pending_files(["a.pdf"], ledger, settle_seconds=10)
        assert len(pending) == 1

    def test_duplicates_within_one_pass_are_analyzed_once(self):
        # Regression: neither copy is in the ledger yet, so without an in-pass
        # check both would be analyzed and billed separately.
        self.write("a.pdf", "SAME")
        self.write("b.pdf", "SAME")
        pending, _, duplicates = pending_files(["a.pdf", "b.pdf"], empty_ledger(), settle_seconds=10)
        assert len(pending) == 1 and duplicates == ["b.pdf"]

    def test_unsettled_files_wait_for_the_next_pass(self):
        self.write("fresh.pdf", "STILL COPYING", settled=False)
        pending, waiting, _ = pending_files(["fresh.pdf"], empty_ledger(), settle_seconds=10)
        assert pending == [] and waiting == ["fresh.pdf"]
        assert not is_stable("fresh.pdf", 10)
        assert not is_stable("missing.pdf", 10)


class TestLedgerRetries(unittest.TestCase):
    def setUp(self):
        temp_workspace()
        self.ledger = empty_ledger()
        self.digest = "abc123"

    def test_failures_retry_until_the_attempt_limit(self):
        for expected in (1, 2):
            assert mark_failed(self.ledger, self.digest, "f.pdf", "boom", recorded_at="t") == expected
            assert status_of(self.ledger, self.digest, 3) == "new"
        assert mark_failed(self.ledger, self.digest, "f.pdf", "boom", recorded_at="t") == 3
        assert status_of(self.ledger, self.digest, 3) == "exhausted"

    def test_recovery_after_failures_records_success(self):
        mark_failed(self.ledger, self.digest, "f.pdf", "boom", recorded_at="t")
        mark_analyzed(self.ledger, self.digest, "f.pdf", "r.md", recorded_at="t")
        assert status_of(self.ledger, self.digest, 3) == "done"
        assert ledger_summary(self.ledger) == (1, 0, 0)

    def test_summary_counts_exhausted_separately(self):
        for _ in range(3):
            mark_failed(self.ledger, self.digest, "f.pdf", "boom", recorded_at="t")
        assert ledger_summary(self.ledger) == (0, 1, 1)


class TestStoreDurability(unittest.TestCase):
    """Both stores must refuse to clobber a file they don't recognize."""

    def setUp(self):
        temp_workspace()

    def test_missing_file_yields_empty_store(self):
        assert load_watchlist("absent.json") == empty_watchlist()
        assert load_ledger("absent.json") == empty_ledger()

    def test_round_trip(self):
        data = empty_watchlist()
        record_analysis(data, "x", "f", "r", "m", ScoreCard(composite=3.0), recorded_at="t")
        save_watchlist(data, "w.json")
        assert load_watchlist("w.json")["companies"] == data["companies"]

    def test_malformed_files_raise_and_are_left_intact(self):
        cases = {
            "bad.json": "{not json",
            "foreign.json": json.dumps({"hello": "world"}),
            "future.json": json.dumps({"version": 99, "companies": {}, "files": {}}),
        }
        for name, body in cases.items():
            with open(name, "w") as f:
                f.write(body)
        for name, body in cases.items():
            with self.assertRaises(WatchlistError):
                load_watchlist(name)
            with self.assertRaises(LedgerError):
                load_ledger(name)
            with open(name) as f:
                assert f.read() == body, f"{name} must not be rewritten"

    def test_writes_leave_no_temp_files_behind(self):
        save_ledger(empty_ledger(), "l.json")
        save_watchlist(empty_watchlist(), "w.json")
        assert not [f for f in os.listdir(".") if f.endswith(".tmp")]

    def test_store_is_created_in_a_missing_directory(self):
        save_ledger(empty_ledger(), "nested/deep/l.json")
        assert os.path.exists("nested/deep/l.json")


if __name__ == "__main__":
    unittest.main()
