"""Extract dimension scores from a completed analysis report.

The specialist prompts require each scored section to end with an explicit
"SCORE: X/N" line (and Section 7 with "RATING: X"), so cross-period
comparison can read scores deterministically instead of re-asking a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


GOVERNANCE_RATINGS = ("STRONG", "ADEQUATE", "CONCERNING", "RED FLAG")

# Numeric value used when charting/deltaing the qualitative governance rating,
# matching the conversion the scorecard prompt specifies.
GOVERNANCE_TO_NUMERIC = {"STRONG": 5.0, "ADEQUATE": 3.5, "CONCERNING": 2.0, "RED FLAG": 1.0}


@dataclass
class ScoreCard:
    """Scores parsed from one report. None means the score wasn't found."""

    revenue_quality: float | None = None   # /5
    margins: float | None = None           # /5
    balance_sheet: float | None = None     # /5
    cash_flow: float | None = None         # /5
    moat: float | None = None              # /15
    governance: str | None = None          # STRONG / ADEQUATE / CONCERNING / RED FLAG
    composite: float | None = None         # /5.0

    def numeric_dimensions(self) -> dict[str, tuple[float | None, int]]:
        """Map of dimension label -> (score, max) for the numeric dimensions."""
        return {
            "Revenue Quality": (self.revenue_quality, 5),
            "Margin Analysis": (self.margins, 5),
            "Balance Sheet": (self.balance_sheet, 5),
            "Cash Flow Quality": (self.cash_flow, 5),
            "Composite Moat": (self.moat, 15),
            "Composite Score": (self.composite, 5),
        }


def split_sections(report_text: str) -> dict[int, str]:
    """Split a report into its numbered sections.

    Returns a dict of section number -> section text (header included).
    """
    sections: dict[int, str] = {}
    matches = list(re.finditer(r"^##\s*SECTION\s+(\d+)\b.*$", report_text, re.MULTILINE))
    for i, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(report_text)
        sections[number] = report_text[match.start():end].strip()
    return sections


def _find_score(section_text: str | None, denominator: int) -> float | None:
    if not section_text:
        return None
    match = re.search(
        rf"SCORE:\s*\**\s*(\d+(?:\.\d+)?)\s*/\s*{denominator}\b",
        section_text,
        re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


def _find_rating(section_text: str | None) -> str | None:
    if not section_text:
        return None
    match = re.search(
        r"RATING:\s*\**\s*(STRONG|ADEQUATE|CONCERNING|RED\s+FLAG)",
        section_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1).upper())


_COMPOSITE_VALUE = re.compile(r"(\d(?:\.\d+)?)\s*/\s*5\.0")


def _find_composite(section_text: str | None) -> float | None:
    if not section_text:
        return None
    # Prefer the value on the COMPOSITE table row: the section may show the
    # moat normalization arithmetic (e.g. "11/15 = 3.7/5.0") before the row,
    # so the first "/5.0" in the section is not necessarily the composite.
    for line in section_text.splitlines():
        if re.search(r"\bCOMPOSITE\b", line):
            match = _COMPOSITE_VALUE.search(line)
            if match:
                return float(match.group(1))
    # Fall back to the last "/5.0" in the section — the composite row is the
    # final row of the scorecard table.
    matches = _COMPOSITE_VALUE.findall(section_text)
    return float(matches[-1]) if matches else None


def parse_scores(report_text: str) -> ScoreCard:
    """Parse the explicit score lines out of a completed report."""
    sections = split_sections(report_text)
    return ScoreCard(
        revenue_quality=_find_score(sections.get(2), 5),
        margins=_find_score(sections.get(3), 5),
        balance_sheet=_find_score(sections.get(4), 5),
        cash_flow=_find_score(sections.get(5), 5),
        moat=_find_score(sections.get(6), 15),
        governance=_find_rating(sections.get(7)),
        composite=_find_composite(sections.get(9)),
    )
