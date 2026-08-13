"""The SCORE/RATING contract that every aggregate feature depends on."""

import unittest

from tests.support import install_stubs

install_stubs()

from utils.score_parser import (  # noqa: E402
    GOVERNANCE_TO_NUMERIC,
    parse_scores,
    split_sections,
)
from tests.support import build_report  # noqa: E402


class TestSplitSections(unittest.TestCase):
    def test_splits_numbered_sections(self):
        sections = split_sections(build_report())
        assert set(sections) == set(range(1, 11)), sorted(sections)
        assert "COMPANY OVERVIEW" in sections[1]
        assert "decelerating" in sections[8]

    def test_missing_sections_are_absent_not_empty(self):
        sections = split_sections("## SECTION 3: MARGIN\nbody")
        assert list(sections) == [3]


class TestParseScores(unittest.TestCase):
    def test_parses_every_dimension(self):
        scores = parse_scores(build_report(revenue=5, margins=2, moat=13, governance="ADEQUATE"))
        assert scores.revenue_quality == 5
        assert scores.margins == 2
        assert scores.moat == 13
        assert scores.governance == "ADEQUATE"
        assert scores.composite == 3.90

    def test_composite_ignores_moat_normalization_arithmetic(self):
        # Regression: the first "/5.0" in section 9 is the normalized moat,
        # not the composite. Taking it mis-ranks peers and fakes deltas.
        scores = parse_scores(build_report(composite="4.10", include_normalization=True))
        assert scores.composite == 4.10

    def test_composite_falls_back_to_last_match(self):
        text = "## SECTION 9: COMPOSITE SCORECARD\nnormalized 3.7/5.0\ntotal is 4.2/5.0\n"
        assert parse_scores(text).composite == 4.2

    def test_tolerates_formatting_variants(self):
        text = (
            "## SECTION 2: REVENUE\n**SCORE: 4/5**\n"
            "## SECTION 3: MARGIN\nscore: 3 / 5\n"
            "## SECTION 7: GOVERNANCE\nRATING:  red  flag\n"
        )
        scores = parse_scores(text)
        assert scores.revenue_quality == 4
        assert scores.margins == 3
        assert scores.governance == "RED FLAG"

    def test_missing_scores_are_none_not_zero(self):
        scores = parse_scores("## SECTION 1: OVERVIEW\nno scores here")
        assert scores.composite is None
        assert scores.revenue_quality is None
        assert scores.governance is None

    def test_governance_ratings_all_map_to_numbers(self):
        for rating in ("STRONG", "ADEQUATE", "CONCERNING", "RED FLAG"):
            scores = parse_scores(f"## SECTION 7: GOV\nRATING: {rating}")
            assert scores.governance == rating
            assert rating in GOVERNANCE_TO_NUMERIC


if __name__ == "__main__":
    unittest.main()
