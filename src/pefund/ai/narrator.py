"""Deterministic commentary writer.

This is the offline back-end of the commentary layer and its safety net.  It
turns the fact pack into the same seven-section commentary a language model is
asked for, using rules instead of a model, so that

* the project runs end to end with no API key;
* there is always a fully verified text to fall back on when a model's output
  fails the numeric grounding check;
* the language model's output has a baseline to be judged against.

Every figure is formatted straight from the fact pack, so the text passes the
grounding check by construction (and a test asserts that it does).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..fund import FundResult
from .prompts import SECTIONS

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}


class _Format:
    """Formatting bound to the fund's currency and unit."""

    def __init__(self, pack: dict[str, Any]) -> None:
        currency = pack["fund"]["currency"].upper()
        self.symbol = SYMBOLS.get(currency, currency + " ")
        self.unit = pack["fund"]["money_unit"].split()[-1]

    def money(self, value: float | None) -> str:
        """Signed money, e.g. $96.9m or -$15.1m."""
        if value is None:
            return "n/a"
        return f"{'-' if value < 0 else ''}{self.symbol}{abs(value):,.1f}{self.unit}"

    def size(self, value: float | None) -> str:
        """Unsigned money, for sentences whose wording already carries the direction."""
        return "n/a" if value is None else self.money(abs(value))

    @staticmethod
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}%"

    @staticmethod
    def rate(value: float | None) -> str:
        """A term such as a fee rate: no trailing zeros (2%, 1.75%)."""
        return "n/a" if value is None else f"{value:g}%"

    @staticmethod
    def multiple(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}x"

    @staticmethod
    def bps(value: float | None) -> str:
        return "n/a" if value is None else f"{abs(value):,.0f} bps"

    @staticmethod
    def years(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f} years"

    @staticmethod
    def month(iso: str | None) -> str:
        if not iso:
            return "n/a"
        return f"{dt.date.fromisoformat(iso):%B %Y}"


WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]

#: How each flexed assumption reads inside a sentence.
ASSUMPTION_PHRASES = {
    "Exit multiple change": "exit multiples",
    "EBITDA growth change": "EBITDA growth",
    "Holding period change (quarters)": "holding periods",
    "Management fee rate": "the management fee",
    "Carried interest rate": "the carry rate",
    "GP catch-up rate": "the GP catch-up",
    "Preferred return": "the preferred return",
    "GP commitment": "the GP commitment",
    "Carry escrow": "the carry escrow",
}


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _spelled(count: int, singular: str, plural: str | None = None) -> str:
    """'two deals' rather than '2 deals' for small counts, so sentences can start with them."""
    word = WORDS[count] if 0 <= count < len(WORDS) else str(count)
    return f"{word} {singular if count == 1 else (plural or singular + 's')}"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _phrase(assumption: str) -> str:
    return ASSUMPTION_PHRASES.get(assumption, assumption.lower())


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def _executive_summary(p: dict[str, Any], f: _Format) -> str:
    fund, terms, perf, val = p["fund"], p["terms"], p["performance_at_liquidation"], p["performance_at_valuation_date"]
    wf, gp = p["waterfall"], p["gp_economics"]
    lines = [
        f"{fund['name']} is a {f.money(fund['committed_capital'])} fund with "
        f"{_plural(fund['number_of_deals'], 'portfolio investment')}, modelled over a "
        f"{fund['fund_life_years']:.1f}-year life under a {terms['waterfall_type']} waterfall: a preferred return "
        f"of {f.rate(terms['preferred_return_pct'])}, a GP catch-up of {f.rate(terms['gp_catch_up_pct'])} and "
        f"carried interest of {f.rate(terms['carried_interest_pct'])}.",
        f"On these assumptions LPs earn a net IRR of {f.pct(perf['net_irr_pct'])} and a net TVPI of "
        f"{f.multiple(perf['net_tvpi_x'])}, against a gross IRR of {f.pct(perf['gross_irr_pct'])} and a gross MOIC "
        f"of {f.multiple(perf['gross_moic_x'])} on the deals themselves. Fund-level costs take "
        f"{f.bps(perf['gross_to_net_irr_spread_bps'])} of IRR.",
    ]
    if (wf["carry_net_of_clawback"] or 0) > 0:
        clawback = (
            f", after returning {f.money(wf['clawback_paid'])} through the clawback" if (wf["clawback_paid"] or 0) > 0 else ""
        )
        lines.append(
            f"The GP earns carried interest of {f.money(wf['carry_net_of_clawback'])}{clawback}, "
            f"{f.pct(wf['carry_share_of_lp_class_profit_pct'])} of LP-class profit. Adding management fees of "
            f"{f.money(gp['management_fees'])} and the profit on its own commitment, the GP and manager receive "
            f"{f.pct(gp['share_of_gross_profit_pct'])} of the fund's gross profit and LPs "
            f"{f.pct(gp['lp_share_of_gross_profit_pct'])}."
        )
    else:
        lines.append(
            "The GP earns no carried interest: LPs never receive their capital plus the preferred return. Its "
            f"economics are management fees of {f.money(gp['management_fees'])} and the result on its own commitment."
        )
    realised = "most of the value was still unrealised" if (val["rvpi_x"] or 0) > (val["dpi_x"] or 0) else (
        "most of the value had already been returned in cash"
    )  # fmt: skip
    lines.append(
        f"At the valuation date of {f.month(val['as_of'])}, LPs had paid in {f.money(val['lp_paid_in'])} "
        f"({f.pct(val['paid_in_pct_of_lp_commitments'])} of commitments) and received "
        f"{f.money(val['lp_distributions'])}. DPI was {f.multiple(val['dpi_x'])}, RVPI {f.multiple(val['rvpi_x'])} "
        f"and net TVPI {f.multiple(val['net_tvpi_x'])}, so {realised}."
    )
    return " ".join(lines)


def _waterfall(p: dict[str, Any], f: _Format) -> str:
    terms, perf, wf = p["terms"], p["performance_at_liquidation"], p["waterfall"]
    european = terms["waterfall_type"].startswith("whole")
    compounding = {"annual": "compounded annually", "quarterly": "compounded quarterly", "simple": "accrued without compounding"}
    paragraphs = []

    tiers = [f"LPs paid in {f.money(perf['lp_paid_in'])} over the life of the fund."]
    if (wf["tier1_return_of_capital"] or 0) >= (perf["lp_paid_in"] or 0) - 0.05:
        tiers.append(f"Tier 1 returned that capital in full ({f.money(wf['tier1_return_of_capital'])}).")
    else:
        tiers.append(f"Tier 1 returned {f.money(wf['tier1_return_of_capital'])} of it, short of the full amount.")
    if (wf["tier2_preferred_return"] or 0) > 0:
        tiers.append(
            f"Tier 2 paid {f.money(wf['tier2_preferred_return'])} of preferred return: the "
            f"{f.rate(terms['preferred_return_pct'])} hurdle, {compounding[terms['preferred_return_compounding']]}, "
            "on capital for as long as it stayed outstanding."
        )
    else:
        tiers.append("Nothing was left to pay the preferred return in tier 2.")
    if (wf["carry_distributed"] or 0) > 0:
        if (wf["tier3_gp_catch_up"] or 0) > 0:
            pace = "every dollar" if terms["gp_catch_up_pct"] >= 100 else f"{f.rate(terms['gp_catch_up_pct'])} of each dollar"
            tiers.append(
                f"Tier 3 gave the GP a catch-up of {f.money(wf['tier3_gp_catch_up'])}; it takes {pace} after the "
                f"hurdle until its carry equals {f.rate(terms['carried_interest_pct'])} of all profit distributed."
            )
        elif terms["gp_catch_up_pct"] == 0:
            tiers.append("There is no catch-up, so the GP shares only in profit above the hurdle.")
        tiers.append(
            f"Tier 4 split the rest {terms['lp_share_of_residual_pct']:g}/{terms['carried_interest_pct']:g}: "
            f"{f.money(wf['tier4_to_lps'])} to LPs and {f.money(wf['tier4_to_gp'])} to the GP."
        )
        share = wf["carry_share_of_lp_class_profit_pct"]
        full = abs((share or 0) - terms["carried_interest_pct"]) < 0.05 and (wf["clawback_paid"] or 0) == 0
        tiers.append(
            f"Carried interest distributed therefore totals {f.money(wf['carry_distributed'])}"
            + (f", {f.pct(share)} of LP-class profit, which is exactly what a completed catch-up is designed to deliver."
               if full else ".")
        )  # fmt: skip
    else:
        tiers.append(
            "No carried interest was paid, because LP distributions never cleared contributed capital plus the "
            "preferred return."
        )
    paragraphs.append(" ".join(tiers))

    if (wf["carry_distributed"] or 0) > 0 and wf["first_carry_date"]:
        if european:
            timing = [
                "Under whole-of-fund terms nothing reaches the GP until LPs have received all contributed capital "
                f"plus the preferred return on it. That is why the first carry was paid only in "
                f"{f.month(wf['first_carry_date'])}, {f.years(wf['first_carry_year_of_fund_life_years'])} into the "
                "fund's life, even though distributions began earlier."
            ]
        else:
            timing = [
                "Under deal-by-deal terms the GP takes carry as each profitable deal is realised, once realised "
                "losses, write-downs and the fees allocated to those deals have been recovered. The first carry was "
                f"paid in {f.month(wf['first_carry_date'])}, {f.years(wf['first_carry_year_of_fund_life_years'])} "
                "into the fund's life."
            ]
        event = next((e for e in wf["distribution_events"] if (e["gp_carry"] or 0) > 0), None)
        if event:
            parts = []
            if (event["tier1_return_of_capital"] or 0) > 0:
                parts.append(f"{f.money(event['tier1_return_of_capital'])} returned capital")
            if (event["tier2_preferred_return"] or 0) > 0:
                parts.append(f"{f.money(event['tier2_preferred_return'])} paid preferred return")
            if (event["tier3_catch_up_to_gp"] or 0) > 0:
                parts.append(f"{f.money(event['tier3_catch_up_to_gp'])} went to the GP as catch-up")
            if (event["tier4_to_lps"] or 0) > 0 or (event["tier4_to_gp"] or 0) > 0:
                parts.append(
                    f"the remainder was split {f.money(event['tier4_to_lps'])} to LPs and "
                    f"{f.money(event['tier4_to_gp'])} to the GP"
                )
            timing.append(
                f"In that quarter the LP class had {f.money(event['lp_distributable'])} to distribute: "
                f"{_join(parts)}."
            )
        paragraphs.append(" ".join(timing))

    if (wf["clawback_before_after_tax_cap"] or 0) > 0:
        text = (
            f"At liquidation the GP had been paid {f.money(wf['carry_distributed'])} of carry, more than the fund as "
            f"a whole supported, and the clawback test required {f.money(wf['clawback_before_after_tax_cap'])} back."
        )
        if (wf["lp_shortfall_from_after_tax_cap"] or 0) > 0:
            text += (
                f" The after-tax limit capped the repayment at {f.money(wf['clawback_paid'])}, leaving LPs "
                f"{f.money(wf['lp_shortfall_from_after_tax_cap'])} short of what a whole-of-fund waterfall would "
                "have given them."
            )
        elif (wf["clawback_paid"] or 0) > 0:
            text += f" The GP repaid {f.money(wf['clawback_paid'])} in full."
        else:
            text += " With the clawback switched off, none of it was recovered."
        paragraphs.append(text)
    elif terms["clawback_enabled"] and (wf["carry_distributed"] or 0) > 0:
        paragraphs.append("The clawback test at liquidation found nothing to return.")
    return "\n\n".join(paragraphs)


def _drivers(p: dict[str, Any], f: _Format) -> str:
    perf, portfolio, deals = p["performance_at_liquidation"], p["portfolio"], p["deals"]
    n = p["fund"]["number_of_deals"]
    top = deals[0]
    lines = [
        f"Gross profit of {f.money(perf['total_gross_profit'])} came from "
        f"{_plural(n, 'investment')} costing {f.money(perf['total_invested'])}. {top['name']} was the largest "
        f"contributor, with a gain of {f.money(top['gain'])} ({f.pct(top['share_of_total_gain_pct'])} of the total) "
        f"on a {f.multiple(top['gross_moic_x'])} gross multiple; "
        + (f"the top three deals produced {f.pct(portfolio['top3_share_of_gain_pct'])} of the gain."
           if (portfolio["top3_share_of_gain_pct"] or 0) <= 100 else
           f"the top three deals earned more than the fund's whole net gain "
           f"({f.pct(portfolio['top3_share_of_gain_pct'])} of it), the rest being given back by the losers."),
        _sentence(
            f"{_spelled(portfolio['deals_at_2x_or_better'], 'deal')} returned at least twice "
            f"{'its' if portfolio['deals_at_2x_or_better'] == 1 else 'their'} cost."
        ),
    ]
    if portfolio["deals_below_cost"]:
        written_off = (
            f", {_spelled(portfolio['write_offs'], 'of them a full write-off', 'of them full write-offs')}"
            if portfolio["write_offs"] else ""
        )  # fmt: skip
        lines.append(
            _sentence(f"{_spelled(portfolio['deals_below_cost'], 'deal')} returned less than cost{written_off}; ")
            + f"together the losses consumed {f.pct(portfolio['capital_lost_pct_of_invested'])} of invested capital."
        )
    drivers = portfolio["value_creation"]
    operating = [d for d in drivers if d["driver"] != "Direct-MOIC positions"]
    direct = next((d for d in drivers if d["driver"] == "Direct-MOIC positions"), None)
    if operating:
        gains = {
            "EBITDA growth": "EBITDA growth",
            "Multiple expansion / (contraction)": "multiple expansion",
            "Combined growth x multiple": "the combined growth-and-multiple effect",
            "Deleveraging (debt paydown)": "debt paydown",
            "Equity floor (limited liability)": "the limited-liability floor on equity",
        }
        losses = {**gains, "Multiple expansion / (contraction)": "multiple contraction",
                  "Deleveraging (debt paydown)": "higher net debt", "EBITDA growth": "falling EBITDA"}  # fmt: skip
        added = sorted((d for d in operating if (d["amount"] or 0) > 0), key=lambda d: -d["amount"])
        removed = sorted((d for d in operating if (d["amount"] or 0) < 0), key=lambda d: d["amount"])
        all_positive = not removed and (direct is None or (direct["amount"] or 0) >= 0)

        def part(d: dict[str, Any], names: dict[str, str]) -> str:
            share = f" ({f.pct(d['share_of_gain_pct'])})" if all_positive else ""
            return f"{f.size(d['amount'])}{share} to {names.get(d['driver'], d['driver'])}"

        sentence = "For the buyout deals the operating bridge "
        if added:
            sentence += f"credits {_join([part(d, gains) for d in added[:3]])}"
        else:
            sentence += "shows no driver adding value"
        if removed:
            taken = [f"{losses.get(d['driver'], d['driver'])} took away {f.size(d['amount'])}" for d in removed[:1]]
            taken += [f"{losses.get(d['driver'], d['driver'])} {f.size(d['amount'])}" for d in removed[1:]]
            sentence += f", while {_join(taken)}"
        if direct and (direct["amount"] or 0) != 0:
            share = f" ({f.pct(direct['share_of_gain_pct'])})" if all_positive else ""
            verb = "add" if direct["amount"] > 0 else "lose"
            sentence += (
                f"; the growth-equity positions, modelled on a direct multiple, {verb} {f.size(direct['amount'])}{share}"
            )
        lines.append(sentence + ".")
        if added and added[0]["driver"] == "EBITDA growth":
            if any(d["driver"] == "Multiple expansion / (contraction)" for d in removed):
                lines.append("Earnings growth carried the portfolio; lower exit multiples gave part of it back.")
            else:
                lines.append(
                    "Returns are therefore earned mainly by growing earnings rather than by buying low and selling high."
                )
    lines.append(f"The cost-weighted average holding period is {f.years(portfolio['average_hold_years'])}.")
    return " ".join(lines)


def _economics(p: dict[str, Any], f: _Format) -> str:
    bridge, split, costs = p["gross_to_net_bridge"], p["split_of_gross_profit"], p["costs"]
    perf, pme = p["performance_at_liquidation"], p["public_market_equivalent"]
    steps = {row["step"]: row for row in bridge}
    takes = [
        f"{label} {f.bps(steps[key]['irr_impact_bps'])}"
        for key, label in (("Management fees", "management fees take"), ("Partnership expenses", "partnership expenses"),
                           ("Carried interest", "carried interest"))
        if key in steps and abs(steps[key]["irr_impact_bps"] or 0) >= 1
    ]  # fmt: skip
    lines = [
        f"Starting from the {f.pct(perf['gross_irr_pct'])} gross IRR, {_join(takes)}, leaving the "
        f"{f.pct(perf['net_irr_pct'])} net IRR."
    ]
    financing = steps.get("Financing and timing")
    if financing and abs(financing["irr_impact_bps"] or 0) >= 1:
        direction = "adds back" if financing["irr_impact_bps"] > 0 else "costs a further"
        lines.append(
            f"The subscription line {direction} {f.bps(financing['irr_impact_bps'])} by delaying capital calls, at "
            f"the price of {f.money(costs['subscription_line_interest'])} of interest."
        )
    by_name = {row["component"]: row for row in split}

    def share(component: str) -> str:
        row = by_name[component]
        return f"{f.money(row['amount'])} ({f.pct(row['share_pct'])})"

    if (by_name["LP net profit"]["amount"] or 0) >= 0 and (perf["total_gross_profit"] or 0) > 0:
        lines.append(
            f"Of the {f.money(perf['total_gross_profit'])} of gross profit, LPs keep {share('LP net profit')}. Carried "
            f"interest takes {share('Carried interest')}, management fees {share('Management fees')}, the profit on "
            f"the GP's own commitment {share('GP commitment profit')} and partnership expenses "
            f"{share('Partnership expenses')}."
        )
    else:
        lines.append(
            f"The portfolio's gross result of {f.money(perf['total_gross_profit'])} does not cover fund costs: LPs "
            f"end with a net result of {f.money(by_name['LP net profit']['amount'])} after management fees of "
            f"{f.money(by_name['Management fees']['amount'])} and partnership expenses of "
            f"{f.money(by_name['Partnership expenses']['amount'])}."
        )
    offsets = (
        f", after {f.money(costs['fee_offsets'])} of portfolio-company fee offsets" if (costs["fee_offsets"] or 0) > 0 else ""
    )
    lines.append(
        f"Management fees total {f.money(costs['management_fees'])}, "
        f"{f.pct(costs['management_fees_pct_of_commitments'])} of committed capital over the fund's life{offsets}."
    )
    if pme["ks_pme_x"] is not None:
        verdict = "ahead of" if pme["ks_pme_x"] > 1 else "behind"
        lines.append(
            f"Against a public benchmark compounding at {f.rate(pme['benchmark_return_pct'])} a year, the "
            f"Kaplan-Schoar PME is {f.multiple(pme['ks_pme_x'])}, so the fund finishes {verdict} that benchmark."
        )
    return " ".join(lines)


def _comparison(p: dict[str, Any], f: _Format) -> str:
    c = p["waterfall_comparison"]
    eu, am = c["whole_of_fund"], c["deal_by_deal"]
    mc = p.get("monte_carlo")
    lines = []
    same = abs((eu["carry_net_of_clawback"] or 0) - (am["carry_net_of_clawback"] or 0)) < 0.05
    if (eu["carry_distributed"] or 0) == 0 and (am["carry_distributed"] or 0) == 0:
        return "Neither waterfall pays any carried interest on these outcomes, so the choice between them does not matter here."
    if same:
        lines.append(
            f"Run on identical deals, the two waterfalls pay the GP the same carry in total "
            f"({f.money(eu['carry_net_of_clawback'])}) but on very different dates."
        )
    else:
        lines.append(
            f"Run on identical deals, whole-of-fund pays the GP {f.money(eu['carry_net_of_clawback'])} of carry net "
            f"of clawback and deal-by-deal {f.money(am['carry_net_of_clawback'])}, and on very different dates."
        )
    if am["first_carry_date"] and eu["first_carry_date"]:
        lines.append(
            f"First carry arrives in {f.month(am['first_carry_date'])} under deal-by-deal against "
            f"{f.month(eu['first_carry_date'])} under whole-of-fund, and the average dollar of carry arrives "
            f"{f.years(c['carry_arrives_earlier_by_years'])} earlier."
        )
    elif am["first_carry_date"]:
        lines.append(
            f"Deal-by-deal pays carry from {f.month(am['first_carry_date'])}; whole-of-fund pays none at all on "
            "these outcomes."
        )
    if (c["lp_irr_cost_of_deal_by_deal_bps"] or 0) != 0:
        lines.append(
            f"Earlier carry for the GP is later cash for LPs: deal-by-deal moves LP net IRR from "
            f"{f.pct(eu['net_irr_pct'])} to {f.pct(am['net_irr_pct'])}, a cost of "
            f"{f.bps(c['lp_irr_cost_of_deal_by_deal_bps'])}, while the present value of the GP's carry at a "
            f"{f.rate(c['present_value_discount_rate_pct'])} discount rate "
            f"{'rises' if (c['carry_present_value_uplift_for_gp'] or 0) >= 0 else 'falls'} by "
            f"{f.size(c['carry_present_value_uplift_for_gp'])}."
        )
    if (am["clawback_paid"] or 0) > 0:
        lines.append(f"On these outcomes deal-by-deal ends with the GP repaying {f.money(am['clawback_paid'])} through the clawback.")
    elif (am["peak_clawback_exposure"] or 0) > 0:
        lines.append(
            f"The risk LPs take in exchange is over-distribution: at its peak the GP held "
            f"{f.money(am['peak_clawback_exposure'])} more carry than the fund would have supported had it been "
            "liquidated at fair value."
        )
    else:
        lines.append(
            "The risk LPs take in exchange is over-distribution, although on the base case the GP was never ahead of "
            "its whole-of-fund entitlement."
        )
    if mc:
        lines.append(
            f"In the simulation a material clawback arises in "
            f"{f.pct(mc['probability_of_material_clawback_deal_by_deal_pct'])} of trials under deal-by-deal, averaging "
            f"{f.money(mc['average_clawback_when_triggered_deal_by_deal'])} when it does, against "
            f"{f.pct(mc['probability_of_material_clawback_whole_of_fund_pct'])} under whole-of-fund."
        )
    return " ".join(lines)


def _scenarios(p: dict[str, Any], f: _Format) -> str:
    blocks = []
    scenarios = p.get("scenarios") or []
    if len(scenarios) > 1:
        ranked = sorted(scenarios, key=lambda s: s["net_irr_pct"] if s["net_irr_pct"] is not None else -999)
        worst, best = ranked[0], ranked[-1]
        intro = (
            f"Across the {_plural(len(scenarios), 'scenario')} net IRR ranges from {f.pct(worst['net_irr_pct'])} "
            f"({worst['name']}) to {f.pct(best['net_irr_pct'])} ({best['name']})."
        )
        bullets = []
        for s in scenarios[1:]:
            change = s["net_irr_change_vs_base_bps"] or 0
            direction = "above" if change > 0 else "below"
            line = (
                f"- **{s['name']}**: net IRR {f.pct(s['net_irr_pct'])} ({f.bps(change)} {direction} base), net TVPI "
                f"{f.multiple(s['net_tvpi_x'])}, carry {f.money(s['carry_net_of_clawback'])}"
            )
            if (s["clawback_paid"] or 0) > 0:
                line += f" after a clawback of {f.money(s['clawback_paid'])}"
            bullets.append(line + ".")
        blocks.append(intro + "\n\n" + "\n".join(bullets))
        clawed = max(scenarios, key=lambda s: s["clawback_paid"] or 0)
        if (clawed["clawback_paid"] or 0) > 0:
            text = (
                f"The \"{clawed['name']}\" scenario is the one that triggers the clawback: the GP repays "
                f"{f.money(clawed['clawback_paid'])}"
            )
            if (clawed["lp_shortfall_from_after_tax_cap"] or 0) > 0:
                text += (
                    f", and the after-tax limit leaves LPs {f.money(clawed['lp_shortfall_from_after_tax_cap'])} short"
                )
            blocks.append(text + ". The same deals under whole-of-fund terms pay no early carry, so there is nothing to recover.")

    sensitivity = p.get("sensitivity") or {}
    rows = sensitivity.get("one_at_a_time") or []
    if len(rows) >= 3:
        first, second, third, last = rows[0], rows[1], rows[2], rows[-1]
        text = (
            f"Moving one assumption at a time, {_phrase(first['assumption'])} has the largest effect: net IRR runs "
            f"from {f.pct(first['unfavourable_case_net_irr_pct'])} to {f.pct(first['favourable_case_net_irr_pct'])}, "
            f"a swing of {f.bps(first['swing_bps'])}. "
            + _sentence(f"{_phrase(second['assumption'])} ({f.bps(second['swing_bps'])}) and "
                        f"{_phrase(third['assumption'])} ({f.bps(third['swing_bps'])}) follow.")
        )  # fmt: skip
        if last["assumption"] == "Preferred return" and p["terms"]["gp_catch_up_pct"] >= 100:
            text += (
                f" The preferred return barely moves the result ({f.bps(last['swing_bps'])}): with a full catch-up "
                "the hurdle changes when carry is paid, not how much."
            )
        blocks.append(text)
    breakevens = {b["threshold"]: b for b in p.get("breakevens") or []}
    hurdle = next((b for t, b in breakevens.items() if "preferred return" in t), None)
    capital = next((b for t, b in breakevens.items() if "capital back" in t), None)
    if hurdle and capital:
        blocks.append(
            f"Exit multiples could fall {f.pct(abs(hurdle['exit_multiple_change_pct']))} across the whole portfolio "
            f"before LPs fail to earn the preferred return (a gross MOIC of {f.multiple(hurdle['gross_moic_x'])}), "
            f"and {f.pct(abs(capital['exit_multiple_change_pct']))} before they lose capital "
            f"({f.multiple(capital['gross_moic_x'])})."
        )
    grid = sensitivity.get("exit_multiple_and_holding_period_grid")
    if grid:
        blocks.append(
            f"Across the grid of exit-multiple and holding-period changes, net IRR ranges from "
            f"{f.pct(grid['lowest_net_irr_pct'])} to {f.pct(grid['highest_net_irr_pct'])}."
        )
    mc = p.get("monte_carlo")
    if mc:
        blocks.append(
            f"The Monte Carlo simulation ({mc['trials']:,} trials) puts the median net IRR at "
            f"{f.pct(mc['net_irr_median_pct'])}, with a 5th-to-95th percentile range of "
            f"{f.pct(mc['net_irr_5th_percentile_pct'])} to {f.pct(mc['net_irr_95th_percentile_pct'])}. LPs miss the "
            f"hurdle, and the GP earns no carry, in {f.pct(mc['probability_net_irr_below_hurdle_pct'])} of trials; LP "
            f"capital is lost in {f.pct(mc['probability_of_capital_loss_pct'])}."
        )
    return "\n\n".join(blocks) if blocks else "No scenario or sensitivity analysis was run."


def _risks(p: dict[str, Any], f: _Format) -> str:
    portfolio, val, terms = p["portfolio"], p["performance_at_valuation_date"], p["terms"]
    bullets = []
    if (portfolio["top3_share_of_gain_pct"] or 0) >= 40:
        bullets.append(
            f"**Concentration.** Three deals produce {f.pct(portfolio['top3_share_of_gain_pct'])} of the gain and "
            f"{portfolio['top_deal']} alone {f.pct(portfolio['top_deal_share_of_gain_pct'])}; a miss on any of them "
            "moves the fund."
        )
    rows = (p.get("sensitivity") or {}).get("one_at_a_time") or []
    if rows:
        bullets.append(
            f"**Key assumption.** LP returns are most sensitive to {_phrase(rows[0]['assumption'])}: a swing of "
            f"{f.bps(rows[0]['swing_bps'])} in net IRR across the range tested."
        )
    steps = {row["step"]: row for row in p["gross_to_net_bridge"]}
    fee_cost = abs((steps.get("Management fees") or {}).get("irr_impact_bps") or 0)
    carry_cost = abs((steps.get("Carried interest") or {}).get("irr_impact_bps") or 0)
    if fee_cost >= carry_cost and fee_cost > 0:
        versus = f", more than carried interest ({f.bps(carry_cost)})," if carry_cost >= 1 else ""
        bullets.append(
            f"**Fee drag.** Management fees cost {f.bps(fee_cost)} of IRR{versus} and are paid whatever the outcome; "
            f"they total {f.pct(p['costs']['management_fees_pct_of_commitments'])} of commitments."
        )
    mc = p.get("monte_carlo")
    if mc and (mc["probability_of_material_clawback_deal_by_deal_pct"] or 0) >= 1:
        cap = " and the after-tax limit means LPs would not recover all of it" if terms["clawback_after_tax_limit"] else ""
        bullets.append(
            f"**Clawback under deal-by-deal terms.** A material clawback arises in "
            f"{f.pct(mc['probability_of_material_clawback_deal_by_deal_pct'])} of simulated outcomes{cap}. "
            + ("No carry is held in escrow to secure it." if (terms["carry_escrow_pct"] or 0) == 0 else
               f"An escrow of {f.rate(terms['carry_escrow_pct'])} of carry secures part of it.")
        )  # fmt: skip
    if (val["rvpi_x"] or 0) > (val["dpi_x"] or 0):
        bullets.append(
            f"**Unrealised value.** At {f.month(val['as_of'])} RVPI of {f.multiple(val['rvpi_x'])} exceeds DPI of "
            f"{f.multiple(val['dpi_x'])}: most of the reported {f.multiple(val['net_tvpi_x'])} TVPI rests on marks, "
            "which in this model follow a stated valuation policy rather than market evidence."
        )
    if mc and (mc["probability_net_irr_below_hurdle_pct"] or 0) > 0:
        bullets.append(
            f"**Hurdle risk.** LPs fall short of the {f.rate(terms['preferred_return_pct'])} preferred return in "
            f"{f.pct(mc['probability_net_irr_below_hurdle_pct'])} of simulated outcomes."
        )
    for flag in p.get("model_flags") or []:
        bullets.append(f"**Model note.** {flag}")
    if not bullets:
        bullets.append("No specific risk indicator was triggered by the model outputs.")
    return "\n".join(f"- {bullet}" for bullet in bullets[:6])


WRITERS = [_executive_summary, _waterfall, _drivers, _economics, _comparison, _scenarios, _risks]


def write_commentary(pack: dict[str, Any]) -> str:
    """Seven-section Markdown commentary built only from the fact pack."""
    f = _Format(pack)
    sections = []
    for (title, _), writer in zip(SECTIONS, WRITERS, strict=True):
        sections.append(f"## {title}\n\n{writer(pack, f)}")
    return "\n\n".join(sections)


# --------------------------------------------------------------------------- #
# Explaining one distribution, and offline question answering
# --------------------------------------------------------------------------- #
def explain_distribution(result: FundResult, as_of: dt.date | int | None = None) -> str:
    """Tier-by-tier explanation of the waterfall in one quarter.

    With ``as_of`` omitted, explains the quarter in which carry was first paid
    (or the largest distribution if no carry was paid).
    """
    a = result.arrays
    cfg = result.config
    if as_of is None:
        paid = [q for q in range(result.n_quarters) if a["gp_carry"][q] > 1e-9]
        q = paid[0] if paid else int(a["lp_distributable"].argmax())
    else:
        q = result.quarter_of(as_of)
    symbol = SYMBOLS.get(cfg.fund.currency.upper(), cfg.fund.currency + " ")

    def money(value: float) -> str:
        return f"{symbol}{value:,.2f}{cfg.fund.unit}"

    cash = float(a["lp_distributable"][q])
    date = f"{result.dates[q]:%d %B %Y}"
    if cash <= 1e-9:
        return f"No cash was distributed to the LP class in the quarter ending {date}."
    kind = "whole-of-fund" if cfg.waterfall.type == "european" else "deal-by-deal"
    t1, t2 = float(a["tier1_return_of_capital"][q]), float(a["tier2_preferred_return"][q])
    cu_gp, cu_lp = float(a["tier3_catch_up_gp"][q]), float(a["tier3_catch_up_lp"][q])
    sp_lp, sp_gp = float(a["tier4_split_lp"][q]), float(a["tier4_split_gp"][q])
    lines = [
        f"Quarter ending {date} ({kind} waterfall): {money(cash)} was available to the LP class.",
        f"1. Return of capital: {money(t1)} to LPs. "
        + (f"{money(float(a['lp_capital_outstanding'][q]))} of eligible capital is still to be returned."
           if a["lp_capital_outstanding"][q] > 1e-9 else "All eligible capital has now been returned."),
        f"2. Preferred return: {money(t2)} to LPs. "
        + (f"{money(float(a['lp_pref_outstanding'][q]))} of preferred return is still owed."
           if a["lp_pref_outstanding"][q] > 1e-9 else "The preferred return is paid up to date."),
    ]  # fmt: skip
    if cu_gp + cu_lp > 1e-9:
        lines.append(
            f"3. GP catch-up: {money(cu_gp)} to the GP" + (f" and {money(cu_lp)} to LPs" if cu_lp > 1e-9 else "")
            + f", moving the GP towards {cfg.waterfall.carry_rate:.0%} of profit distributed to date."
        )
    else:
        lines.append("3. GP catch-up: nothing was needed or nothing was left for this tier.")
    if sp_lp + sp_gp > 1e-9:
        lines.append(
            f"4. Residual split: {money(sp_lp)} to LPs and {money(sp_gp)} to the GP "
            f"({1 - cfg.waterfall.carry_rate:.0%} / {cfg.waterfall.carry_rate:.0%})."
        )
    else:
        lines.append("4. Residual split: no cash reached this tier.")
    lines.append(
        f"Result: LPs received {money(t1 + t2 + cu_lp + sp_lp)} and the GP {money(cu_gp + sp_gp)} of carried interest"
        + (f", of which {money(float(a['carry_escrowed'][q]))} was placed in escrow." if a["carry_escrowed"][q] > 1e-9 else ".")
    )
    if a["clawback_paid"][q] > 1e-9:
        lines.append(f"At liquidation the GP also returned {money(float(a['clawback_paid'][q]))} through the clawback.")
    return "\n".join(lines)


def lookup_facts(pack: dict[str, Any], question: str, limit: int = 12) -> str:
    """Offline answer to a question: the facts whose names best match its words."""
    from .facts import flatten_numbers

    stop = {"the", "a", "an", "of", "is", "was", "what", "how", "much", "did", "does", "in", "to", "and", "for", "on",
            "why", "when", "which", "are", "were", "fund", "it", "that", "this", "at", "by", "with", "do"}  # fmt: skip
    words = {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in question).split() if w not in stop}
    synonyms = {"irr": {"irr"}, "carry": {"carry", "carried"}, "fees": {"fee", "fees", "management"},
                "clawback": {"clawback"}, "tvpi": {"tvpi"}, "dpi": {"dpi"}, "moic": {"moic"},
                "hurdle": {"preferred", "hurdle", "pref"}, "pref": {"preferred", "hurdle", "pref"}}  # fmt: skip
    expanded = set(words)
    for word in words:
        expanded |= synonyms.get(word, set())
    scored = []
    for path, value in flatten_numbers(pack).items():
        tokens = set(path.replace("[", "_").replace("]", "_").replace(".", "_").split("_"))
        score = len(expanded & tokens)
        if score:
            scored.append((score, -len(path), path, value))
    if not scored:
        return "Offline mode found no figure matching that question. Set an API key to ask free-form questions."
    scored.sort(reverse=True)
    lines = ["Offline mode (no language model configured): the closest figures in the model output are"]
    lines += [f"- {path} = {value:g}" for _, _, path, value in scored[:limit]]
    return "\n".join(lines)
