"""Numeric grounding: check every figure in generated text against the fact pack.

A language model can write fluent commentary around a number it invented.  In a
fund report that is the failure that matters, so nothing is published on trust:

1. every number and every date is pulled out of the generated text;
2. each is matched to a figure in the fact pack of the same kind (a percentage
   to a percentage, a multiple to a multiple, money to money), allowing only
   for the rounding the text itself uses;
3. anything unmatched is reported, the model is asked to repair it, and if it
   still cannot be verified the pipeline falls back to the deterministic writer.

Two levels of strictness apply:

* **kind** - "15.2%" is never verified by a money figure of 15.2;
* **label** - a figure introduced as "net IRR", "gross IRR", "TVPI", "MOIC",
  "DPI" or "RVPI" must equal a fact of that same metric, so a gross IRR cannot
  be passed off as a net IRR.

What the check cannot do: confirm that a correctly valued, correctly typed
figure is attached to the right scenario or deal.  It removes invented and
mislabelled numbers, not every possible misstatement, which is why the prompt
also restricts the model to the fact pack and why a human should still read
the commentary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .facts import flatten_numbers

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_YEAR = re.compile(rf"\b(?:(\d{{1,2}})\s+)?({_MONTH})\.?,?\s+(\d{{4}})\b", re.IGNORECASE)
_QUARTER_YEAR = re.compile(r"\bQ([1-4])\s+(\d{4})\b", re.IGNORECASE)
_YEAR = re.compile(r"(?<![\w.,$€£₹-])(19\d{2}|20\d{2})(?![\w%]|\.\d|,\d)")
_NUMBER = re.compile(
    r"(?<![\w.])(?P<sign>[-+−–])?(?P<currency>[$€£₹])?(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suffix>\s?(?:%|x\b|bps\b|bp\b|basis points\b|percentage points\b|points\b|pts\b|m\b|mn\b|million\b|bn\b|"
    r"billion\b|years?\b|yrs?\b|quarters?\b|months?\b))?",
    re.IGNORECASE,
)
_ORDINAL = re.compile(r"^(st|nd|rd|th)\b", re.IGNORECASE)
#: Figures that need no source: nil, unity and the whole.
ALWAYS_ALLOWED = (0.0, 1.0, 100.0)

#: A figure that follows one of these labels must be a fact of the same metric.
#: Each entry: (pattern for the label, text that the fact's key must contain, unit that must follow the number).
_FILLER = r"[^.;:%\dx]{0,28}?"
LABELLED_METRICS = [
    (re.compile(rf"\bnet IRR\b{_FILLER}(?P<number>[-−–]?\d+(?:\.\d+)?)\s?%", re.IGNORECASE), "net_irr", "net IRR"),
    (
        re.compile(rf"\bgross IRR\b{_FILLER}(?P<number>[-−–]?\d+(?:\.\d+)?)\s?%", re.IGNORECASE),
        "gross_irr",
        "gross IRR",
    ),
    (re.compile(rf"\bTVPI\b{_FILLER}(?P<number>\d+(?:\.\d+)?)\s?x\b", re.IGNORECASE), "tvpi", "TVPI"),
    (re.compile(rf"\bMOIC\b{_FILLER}(?P<number>\d+(?:\.\d+)?)\s?x\b", re.IGNORECASE), "moic", "MOIC"),
    (re.compile(rf"\bDPI\b{_FILLER}(?P<number>\d+(?:\.\d+)?)\s?x\b", re.IGNORECASE), "dpi", "DPI"),
    (re.compile(rf"\bRVPI\b{_FILLER}(?P<number>\d+(?:\.\d+)?)\s?x\b", re.IGNORECASE), "rvpi", "RVPI"),
]

KIND_BY_SUFFIX = {
    "%": "pct", "x": "multiple", "bps": "bps", "bp": "bps", "basis points": "bps", "percentage points": "points",
    "points": "points", "pts": "points", "m": "money", "mn": "money", "million": "money", "bn": "money",
    "billion": "money", "year": "duration", "years": "duration", "yr": "duration", "yrs": "duration",
    "quarter": "duration", "quarters": "duration", "month": "duration", "months": "duration",
}  # fmt: skip


def _fact_kind(path: str) -> str:
    leaf = path.rsplit(".", 1)[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    if leaf.endswith("_pct") or "_pct_" in leaf:  # e.g. paid_in_pct_of_lp_commitments
        return "pct"
    if leaf.endswith("_bps") or "_bps_" in leaf:
        return "bps"
    if leaf.endswith("_x"):
        return "multiple"
    if leaf.endswith(("_years", "_quarters")):
        return "duration"
    if leaf in ("number_of_deals", "number_of_investors", "deals_below_cost", "write_offs", "deals_at_2x_or_better",
                "trials"):  # fmt: skip
        return "count"
    return "money"


@dataclass
class GroundingReport:
    """Outcome of checking a piece of generated text against the fact pack."""

    total: int = 0
    verified: int = 0
    unverified: list[dict[str, str]] = field(default_factory=list)

    @property
    def score(self) -> float:
        return 1.0 if self.total == 0 else self.verified / self.total

    @property
    def passed(self) -> bool:
        return not self.unverified

    def summary(self) -> str:
        if self.total == 0:
            return "No figures to verify."
        text = f"{self.verified} of {self.total} figures verified against the model output ({self.score:.0%})."
        if self.unverified:
            listed = ", ".join(f"'{item['token']}'" for item in self.unverified[:8])
            text += f" Not verified: {listed}."
        return text


def _context(text: str, start: int, end: int, width: int = 45) -> str:
    return " ".join(text[max(0, start - width) : min(len(text), end + width)].split())


def _pack_dates(pack: Any) -> tuple[set[tuple[int, int]], set[int]]:
    """Every (year, month) and year that appears as a date string in the pack."""
    months: set[tuple[int, int]] = set()
    years: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            for match in _ISO_DATE.finditer(node):
                year, month = int(match.group(1)), int(match.group(2))
                months.add((year, month))
                years.add(year)

    walk(pack)
    return months, years


def verify_numbers(text: str, pack: dict[str, Any], threshold_kind_strict: bool = True) -> GroundingReport:
    """Check every number and date in ``text`` against ``pack``."""
    report = GroundingReport()
    facts = flatten_numbers(pack)
    by_kind: dict[str, list[float]] = {}
    for path, value in facts.items():
        by_kind.setdefault(_fact_kind(path), []).append(abs(value))
    pack_months, pack_years = _pack_dates(pack)
    working = text

    def blank(match: re.Match[str]) -> str:
        return " " * (match.end() - match.start())

    def note(ok: bool, token: str, start: int, end: int) -> None:
        report.total += 1
        if ok:
            report.verified += 1
        else:
            report.unverified.append({"token": token.strip(), "context": _context(text, start, end)})

    # --- dates first, so their digits are not mistaken for figures ---------
    for match in _ISO_DATE.finditer(working):
        note((int(match.group(1)), int(match.group(2))) in pack_months, match.group(0), match.start(), match.end())
    working = _ISO_DATE.sub(blank, working)
    for match in _MONTH_YEAR.finditer(working):
        month, year = MONTHS[match.group(2).lower().rstrip(".")], int(match.group(3))
        note((year, month) in pack_months, match.group(0), match.start(), match.end())
    working = _MONTH_YEAR.sub(blank, working)
    for match in _QUARTER_YEAR.finditer(working):
        quarter, year = int(match.group(1)), int(match.group(2))
        note((year, quarter * 3) in pack_months, match.group(0), match.start(), match.end())
    working = _QUARTER_YEAR.sub(blank, working)
    for match in _YEAR.finditer(working):
        note(int(match.group(1)) in pack_years, match.group(0), match.start(), match.end())
    working = _YEAR.sub(blank, working)

    # --- structural numbers that are not figures ----------------------------
    working = re.sub(r"(?im)^\s*#{1,6}.*$", blank, working)  # headings
    working = re.sub(r"(?im)^\s*\d+[.)]\s", blank, working)  # ordered-list markers
    working = re.sub(r"(?i)\btiers?\s+\d(?:\s*(?:-|to|and|–)\s*\d)?", blank, working)  # "tier 3", "tiers 1-2"

    # --- labelled metrics: the figure must be a fact of that very metric ----
    mislabelled: dict[int, str] = {}
    for pattern, key_part, label in LABELLED_METRICS:
        allowed = [abs(value) for path, value in facts.items() if key_part in path.rsplit(".", 1)[-1]]
        for match in pattern.finditer(working):
            raw = match.group("number").lstrip("-−–")
            decimals = len(raw.split(".")[1]) if "." in raw else 0
            tolerance = 0.5 * 10.0 ** (-decimals) + 1e-9
            if not any(abs(fact - float(raw)) <= tolerance for fact in allowed):
                mislabelled[match.start("number") + (len(match.group("number")) - len(raw))] = label

    # --- every remaining number ---------------------------------------------
    for match in _NUMBER.finditer(working):
        raw = match.group("number")
        if _ORDINAL.match(working[match.end("number") :]):
            continue  # "5th percentile"
        value = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tolerance = 0.5 * 10.0 ** (-decimals) + 1e-9
        suffix = (match.group("suffix") or "").strip().lower()
        kind = KIND_BY_SUFFIX.get(suffix)
        if kind is None and match.group("currency"):
            kind = "money"
        if suffix in ("bn", "billion"):
            value, tolerance = value * 1000.0, tolerance * 1000.0

        if any(abs(value - allowed) <= 1e-9 for allowed in ALWAYS_ALLOWED):
            ok = True
        else:
            if kind == "pct":
                pools = [("pct", 1.0), ("bps", 0.01)]
            elif kind == "points":
                pools = [("bps", 0.01), ("pct", 1.0)]
            elif kind == "bps":
                pools = [("bps", 1.0), ("pct", 100.0)]
            elif kind == "duration":
                pools = [("duration", 1.0), ("duration", 12.0), ("duration", 4.0), ("duration", 0.25), ("count", 1.0)]
            elif kind in ("multiple", "money"):
                pools = [(kind, 1.0)]
            else:  # a bare number: a count, or a figure whose unit is in the surrounding words
                pools = [(name, 1.0) for name in by_kind] if not threshold_kind_strict else [
                    ("count", 1.0), ("duration", 1.0), ("money", 1.0), ("pct", 1.0), ("multiple", 1.0), ("bps", 1.0),
                ]  # fmt: skip
            ok = any(
                abs(fact * scale - value) <= tolerance * max(scale, 1.0)
                for name, scale in pools
                for fact in by_kind.get(name, [])
            )
        if match.start("number") in mislabelled:
            ok = False  # the value exists, but not for the metric the text names
        note(ok, match.group(0), match.start(), match.end())
    return report
