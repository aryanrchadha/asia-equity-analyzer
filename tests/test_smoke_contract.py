"""Tests for the live smoke test's own contract checker.

smoke_live.py can't run here (it needs credentials), but its verifier is pure
logic over report text, so it can be exercised offline. Without this, a
smoke test that silently passes everything would look like good news.
"""

import importlib.util
import os
import unittest

from tests.support import build_report, install_stubs, quiet

install_stubs()

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_live.py")
_spec = importlib.util.spec_from_file_location("smoke_live", _PATH)
smoke_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke_live)


class TestContractVerifier(unittest.TestCase):
    def verify(self, report):
        with quiet() as out:
            result = smoke_live.verify_contract(report)
        return result, out.getvalue()

    def test_well_formed_report_passes(self):
        ok, _ = self.verify(build_report())
        assert ok is True

    def test_missing_governance_rating_fails(self):
        ok, out = self.verify(build_report().replace("RATING: STRONG", "Board looks fine."))
        assert ok is False and "❌ Section 7 governance" in out

    def test_missing_score_line_fails(self):
        report = build_report().replace("SCORE: 3/5\n\n## SECTION 4", "\n\n## SECTION 4")
        ok, _ = self.verify(report)
        assert ok is False

    def test_missing_section_fails(self):
        report = build_report().replace("## SECTION 8: KEY FINANCIAL TENSIONS", "## Tensions")
        ok, out = self.verify(report)
        assert ok is False and "all ten numbered sections" in out

    def test_composite_inconsistent_with_dimensions_fails(self):
        # Catches a synthesis agent inventing a composite instead of
        # weighting the specialists' scores.
        ok, out = self.verify(build_report(
            composite="1.00", revenue=5, margins=5, balance=5, cash_flow=5, moat=15))
        assert ok is False and "composite is consistent" in out

    def test_out_of_range_score_fails(self):
        ok, out = self.verify(build_report().replace("SCORE: 4/5", "SCORE: 9/5"))
        assert ok is False and "out of range" in out

    def test_fixture_filing_exists_and_is_substantive(self):
        # The smoke test defaults to this filing; it needs enough financial
        # detail for all six specialists to have something to score.
        with open(smoke_live.FIXTURE) as f:
            text = f.read()
        assert len(text) > 2000
        for marker in ("Revenue", "Gross margin", "cash", "Board", "IFRS"):
            assert marker in text, marker


if __name__ == "__main__":
    unittest.main()
