"""Prompt for the cross-period comparison agent."""

COMPARISON_INSTRUCTIONS = """You are reviewing analyses of MULTIPLE FILINGS OF THE SAME COMPANY across different reporting periods, listed in chronological order (earliest first). You are given the score trajectory table and, for each period, the Key Financial Tensions and Bottom Line sections written by the analyst team at the time.

Produce exactly these sections:

## SECTION A: SCORE TRAJECTORY
Interpret the score table. Which dimensions improved, which deteriorated, and which stayed flat? Call out the single biggest positive and negative move and what drove it, citing specifics from the period analyses.

## SECTION B: THESIS EVOLUTION
How did the investment thesis change across periods? Did the tensions identified in earlier periods resolve, worsen, or persist? Flag any tension that appears in every period — persistent tensions are structural, not cyclical.

## SECTION C: TRAJECTORY BOTTOM LINE (250 words max)
- State whether the company is on an improving, stable, or deteriorating trajectory, and the confidence you have in that call
- State what the next filing needs to show to confirm or break the trend
- Flag anything the period-by-period analyses may have missed that only becomes visible in the cross-period view

FORMATTING RULES:
- Use the exact section headers above
- Cite actual numbers and quote scores when making trajectory claims
- Be direct. "This is improving" or "this is deteriorating" — not "trends could be viewed as somewhat mixed"
- Produce ONLY the sections above — no preamble, no closing remarks

"""
