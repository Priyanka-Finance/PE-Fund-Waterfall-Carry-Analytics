"""Fund- and portfolio-level analytics built on a :class:`~pefund.fund.FundResult`.

Each function answers one question an LP, a GP or an investment committee
would ask of a fund model:

* ``gross_to_net_bridge`` - where does the gap between gross and net return come from?
* ``value_split``         - who ends up with each dollar of gross profit?
* ``lp_gp_economics``     - what does each side put in and take out?
* ``value_creation``      - did returns come from growth, multiple or leverage?
* ``concentration``       - how dependent is the fund on a few deals?
* ``compare_waterfalls``  - what changes between whole-of-fund and deal-by-deal?
* ``investor_allocation`` - what does each LP's capital account look like?
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from .config import FundConfig
from .fund import FundResult, run_fund
from .metrics import irr_from_years, ks_pme, safe_ratio


# --------------------------------------------------------------------------- #
# Gross-to-net
# --------------------------------------------------------------------------- #
def gross_to_net_bridge(result: FundResult, as_of: dt.date | int | None = None) -> pd.DataFrame:
    """Walk from the gross (deal-level) return to the LPs' net return.

    Each step switches on one more fund-level cost and re-measures the LPs'
    IRR and multiple on the resulting cash flows:

    1. gross - the LPs' share of deal cash flows only;
    2. after management fees (net of offsets);
    3. after partnership expenses (including subscription-line interest);
    4. after carried interest (net of any clawback, including accrued carry);
    5. financing and timing - subscription-line deferral of capital calls and
       fees settled out of proceeds - which lands on the reported net return.

    The first row carries the gross level, the middle rows the impact of each
    cost, and the last row the net level and the total gross-to-net gap.
    """
    q = result.quarter_of(as_of)
    a, t = result.arrays, result.schedule.t[: q + 1]
    lp_pct = result.config.fund.lp_pct
    window = slice(0, q + 1)

    def stage(outflows: np.ndarray, inflows: np.ndarray, terminal: float) -> tuple[float, float]:
        # The multiple uses separate in- and out-flows so that a quarter holding
        # both a capital call and a distribution is not netted away.
        flows = inflows - outflows
        flows[q] += terminal
        return irr_from_years(t, flows), safe_ratio(inflows.sum() + terminal, outflows.sum())

    invest = lp_pct * a["investments"][window]
    proceeds = lp_pct * a["gross_proceeds"][window]
    fair_value = lp_pct * float(a["portfolio_fv"][q])
    fees = a["mgmt_fee_net"][window]
    expenses = a["lp_expenses"][window]
    carry = a["gp_carry"][window] - a["clawback_paid"][window]
    accrued_carry = float(a["accrued_carry"][q] - a["clawback_exposure"][q])

    stages = [
        ("Gross return (deal level)", stage(invest, proceeds, fair_value)),
        ("Management fees", stage(invest + fees, proceeds, fair_value)),
        ("Partnership expenses", stage(invest + fees + expenses, proceeds, fair_value)),
        ("Carried interest", stage(invest + fees + expenses, proceeds - carry, fair_value - accrued_carry)),
        (
            "Financing and timing",
            stage(a["lp_contributions"][window], a["lp_distributions"][window], float(a["lp_nav"][q])),
        ),
    ]
    gross_irr, gross_multiple = stages[0][1]
    net_irr, net_multiple = stages[-1][1]

    rows = []
    previous: tuple[float, float] | None = None
    for label, (irr, multiple) in stages:
        rows.append(
            {
                "step": label,
                "irr_after": irr,
                "irr_impact": 0.0 if previous is None else irr - previous[0],
                "multiple_after": multiple,
                "multiple_impact": 0.0 if previous is None else multiple - previous[1],
            }
        )
        previous = (irr, multiple)
    rows.append(
        {
            "step": "Net return to LPs",
            "irr_after": net_irr,
            "irr_impact": net_irr - gross_irr,
            "multiple_after": net_multiple,
            "multiple_impact": net_multiple - gross_multiple,
        }
    )
    frame = pd.DataFrame(rows)
    frame["irr_impact_bps"] = frame["irr_impact"] * 1e4
    return frame


def value_split(result: FundResult, as_of: dt.date | int | None = None) -> pd.DataFrame:
    """Who receives each dollar of the fund's gross profit.

    The five lines always add up to gross profit (proceeds plus fair value less
    cost), which makes this a useful reconciliation as well as a chart.
    """
    q = result.quarter_of(as_of)
    a = result.arrays
    window = slice(0, q + 1)
    gross_profit = float(a["gross_proceeds"][window].sum() + a["portfolio_fv"][q] - a["investments"][window].sum())
    lp_profit = float(a["lp_distributions"][window].sum() + a["lp_nav"][q] - a["lp_contributions"][window].sum())
    gp_commit_profit = float(
        a["gp_commit_distributions"][window].sum() + a["gp_commit_nav"][q] - a["gp_contributions"][window].sum()
    )
    fees = float(a["mgmt_fee_net"][window].sum())
    expenses = float(a["expenses"][window].sum() + a["line_interest"][window].sum() + a["line_interest_accrued"][q])
    carry = float(
        a["gp_carry"][window].sum() - a["clawback_paid"][window].sum() + a["accrued_carry"][q] - a["clawback_exposure"][q]
    )
    rows = [
        ("LP net profit", lp_profit, "LP"),
        ("Carried interest", carry, "GP"),
        ("Management fees", fees, "GP"),
        ("GP commitment profit", gp_commit_profit, "GP"),
        ("Partnership expenses", expenses, "Third parties"),
    ]
    frame = pd.DataFrame(rows, columns=["component", "amount", "recipient"])
    frame["share_of_gross_profit"] = frame["amount"] / gross_profit if abs(gross_profit) > 1e-9 else np.nan
    frame.attrs["gross_profit"] = gross_profit
    frame.attrs["unexplained"] = gross_profit - float(frame["amount"].sum())
    return frame


# --------------------------------------------------------------------------- #
# LP / GP economics
# --------------------------------------------------------------------------- #
def lp_gp_economics(result: FundResult) -> pd.DataFrame:
    """Whole-life cash in, cash out, profit and returns for the LPs and the GP."""
    a, t, s = result.arrays, result.schedule.t, result.totals
    lp_flows = a["lp_distributions"] - a["lp_contributions"]
    gp_commit_flows = a["gp_commit_distributions"] - a["gp_contributions"]
    gp_all_flows = gp_commit_flows + a["gp_carry_cash"] + a["mgmt_fee_net"]
    rows = [
        {
            "party": "Limited partners",
            "contributions": s["total_lp_contributions"],
            "distributions": s["total_lp_distributions"],
            "profit": s["total_lp_profit"],
            "multiple": safe_ratio(s["total_lp_distributions"], s["total_lp_contributions"]),
            "irr": irr_from_years(t, lp_flows),
        },
        {
            "party": "GP commitment (as investor)",
            "contributions": s["gp_commit_contributions"],
            "distributions": s["gp_commit_distributions"],
            "profit": s["gp_commit_profit"],
            "multiple": safe_ratio(s["gp_commit_distributions"], s["gp_commit_contributions"]),
            "irr": irr_from_years(t, gp_commit_flows),
        },
        {
            "party": "GP carried interest (net of clawback)",
            "contributions": 0.0,
            "distributions": s["carry_net"],
            "profit": s["carry_net"],
            "multiple": np.nan,
            "irr": np.nan,
        },
        {
            "party": "Manager - management fees (net of offsets)",
            "contributions": 0.0,
            "distributions": s["total_management_fees"],
            "profit": s["total_management_fees"],
            "multiple": np.nan,
            "irr": np.nan,
        },
        {
            "party": "GP and manager combined",
            "contributions": s["gp_commit_contributions"],
            "distributions": s["gp_commit_distributions"] + s["carry_net"] + s["total_management_fees"],
            "profit": s["gp_total_economics"],
            "multiple": safe_ratio(
                s["gp_commit_distributions"] + s["carry_net"] + s["total_management_fees"],
                s["gp_commit_contributions"],
            ),
            "irr": irr_from_years(t, gp_all_flows),
        },
    ]
    frame = pd.DataFrame(rows)
    frame["share_of_gross_profit"] = frame["profit"] / s["total_gross_profit"] if s["total_gross_profit"] else np.nan
    return frame


def carry_profile(result: FundResult, discount_rate: float = 0.10) -> dict[str, Any]:
    """Timing of carried interest: when it is earned, paid, escrowed and clawed back."""
    a, t = result.arrays, result.schedule.t
    cash = a["gp_carry_cash"]
    discount = (1.0 + discount_rate) ** (-t)
    paid = np.nonzero(a["gp_carry"] > 1e-9)[0]
    total = float(cash.sum())
    weighted_time = float((cash * t).sum() / total) if abs(total) > 1e-9 else float("nan")
    return {
        "carry_gross": float(a["gp_carry"].sum()),
        "carry_net": total,
        "clawback_paid": float(a["clawback_paid"].sum()),
        "first_carry_date": result.dates[int(paid[0])] if len(paid) else None,
        "first_carry_year": float(t[int(paid[0])]) if len(paid) else float("nan"),
        "average_carry_timing_years": weighted_time,
        "carry_present_value": float((cash * discount).sum()),
        "discount_rate": discount_rate,
        "peak_escrow": float(a["escrow_balance"].max()),
        "peak_clawback_exposure": float(np.nanmax(a["clawback_exposure"])) if result.interim else float("nan"),
    }


def compare_waterfalls(cfg: FundConfig) -> dict[str, Any]:
    """Run the same fund under both waterfall types and line up the differences."""
    runs = {kind: run_fund(cfg.with_values({"waterfall.type": kind})) for kind in ("european", "american")}
    rows = []
    for kind, result in runs.items():
        summary, profile = result.summary, carry_profile(result)
        rows.append(
            {
                "waterfall": "Whole-of-fund (European)" if kind == "european" else "Deal-by-deal (American)",
                "net_irr": summary["net_irr"],
                "tvpi": summary["tvpi"],
                "lp_distributions": summary["total_lp_distributions"],
                "carry_gross": profile["carry_gross"],
                "clawback_paid": profile["clawback_paid"],
                "carry_net": profile["carry_net"],
                "first_carry_date": profile["first_carry_date"],
                "average_carry_timing_years": profile["average_carry_timing_years"],
                "carry_present_value": profile["carry_present_value"],
                "peak_clawback_exposure": profile["peak_clawback_exposure"],
            }
        )
    table = pd.DataFrame(rows)
    european, american = runs["european"], runs["american"]
    n = max(european.n_quarters, american.n_quarters)

    def padded(result: FundResult, name: str) -> np.ndarray:
        values = np.cumsum(result.arrays[name])
        return np.concatenate([values, np.full(n - len(values), values[-1])])

    timeline = pd.DataFrame(
        {
            "date": pd.to_datetime(european.dates if european.n_quarters == n else american.dates),
            "european_cumulative_carry": padded(european, "gp_carry_cash"),
            "american_cumulative_carry": padded(american, "gp_carry_cash"),
            "european_cumulative_lp_distributions": padded(european, "lp_distributions"),
            "american_cumulative_lp_distributions": padded(american, "lp_distributions"),
        }
    )
    return {"table": table, "timeline": timeline, "runs": runs}


# --------------------------------------------------------------------------- #
# Portfolio analytics
# --------------------------------------------------------------------------- #
def value_creation(result: FundResult) -> pd.DataFrame:
    """Split the portfolio's total gain into its operating drivers.

    Operating-case deals are decomposed into EBITDA growth, multiple expansion,
    the combined growth-times-multiple effect and deleveraging.  Deals modelled
    directly on a MOIC (growth equity, write-offs, overridden outcomes) are
    shown as one line because no operating bridge exists for them.
    """
    deals = result.deals
    operating = deals[deals["mode"] == "operating"]
    direct = deals[deals["mode"] != "operating"]
    rows = [
        ("EBITDA growth", float(operating["vc_ebitda_growth"].sum())),
        ("Multiple expansion / (contraction)", float(operating["vc_multiple_expansion"].sum())),
        ("Combined growth x multiple", float(operating["vc_combined"].sum())),
        ("Deleveraging (debt paydown)", float(operating["vc_deleveraging"].sum())),
        ("Equity floor (limited liability)", float(operating["vc_equity_floor"].sum())),
        ("Direct-MOIC positions", float(direct["gain"].sum())),
    ]
    frame = pd.DataFrame(rows, columns=["driver", "amount"])
    total = float(deals["gain"].sum())
    frame["share_of_gain"] = frame["amount"] / total if abs(total) > 1e-9 else np.nan
    frame.attrs["total_gain"] = total
    frame.attrs["unexplained"] = total - float(frame["amount"].sum())
    return frame


def concentration(result: FundResult) -> dict[str, Any]:
    """Dispersion of outcomes: winners, losers and dependence on the best deals."""
    deals = result.deals.sort_values("gain", ascending=False)
    invested = float(deals["invested"].sum())
    gains = deals["gain"]
    positive_gain = float(gains[gains > 0].sum())
    losers = deals[deals["gross_moic"] < 1.0]
    return {
        "n_deals": int(len(deals)),
        "top_deal": str(deals.iloc[0]["deal"]),
        "top_deal_share_of_gain": safe_ratio(float(gains.iloc[0]), float(gains.sum())),
        "top3_share_of_gain": safe_ratio(float(gains.iloc[:3].sum()), float(gains.sum())),
        "top3_share_of_positive_gain": safe_ratio(float(gains.iloc[:3].sum()), positive_gain),
        "loss_ratio": safe_ratio(float(losers["invested"].sum()), invested),
        "capital_loss_ratio": safe_ratio(float(-losers["gain"].sum()), invested),
        "n_losers": int(len(losers)),
        "n_write_offs": int((deals["proceeds"] <= 1e-9).sum()),
        "n_above_2x": int((deals["gross_moic"] >= 2.0).sum()),
        "average_hold_years": float((deals["hold_years"] * deals["invested"]).sum() / invested),
        "largest_position_pct_of_invested": float(deals["invested"].max() / invested),
    }


def sector_summary(result: FundResult) -> pd.DataFrame:
    """Cost, proceeds and gain by sector."""
    grouped = result.deals.groupby("sector", as_index=False).agg(
        deals=("deal", "count"), invested=("invested", "sum"), proceeds=("proceeds", "sum"), gain=("gain", "sum")
    )
    grouped["gross_moic"] = grouped["proceeds"] / grouped["invested"]
    return grouped.sort_values("gain", ascending=False).reset_index(drop=True)


def annual_summary(result: FundResult) -> pd.DataFrame:
    """Calendar-year roll-up of cash flows with year-end balances and metrics."""
    q = result.quarterly
    flow_cols = [
        "investments", "gross_proceeds", "mgmt_fee_net", "expenses", "lp_contributions", "lp_distributions",
        "tier1_return_of_capital", "tier2_preferred_return", "tier3_catch_up_gp", "tier3_catch_up_lp",
        "tier4_split_lp", "tier4_split_gp", "gp_carry", "gp_carry_cash", "clawback_paid", "gp_commit_distributions",
    ]  # fmt: skip
    balance_cols = ["portfolio_fv", "lp_nav", "accrued_carry", "lp_unfunded", "dpi", "rvpi", "tvpi", "net_irr"]
    year = q["date"].dt.year
    flows = q.groupby(year)[flow_cols].sum()
    balances = q.groupby(year)[balance_cols].last()
    out = flows.join(balances)
    out.index.name = "year"
    return out.reset_index()


def investor_allocation(result: FundResult, as_of: dt.date | int | None = None) -> pd.DataFrame:
    """Capital-account summary for each LP in the register.

    Every LP invests on the same terms, so each one takes its pro-rata share
    (by commitment) of every LP-class cash flow and of the net asset value.
    """
    investors = result.config.investors
    if not investors:
        return pd.DataFrame()
    q = result.quarter_of(as_of)
    m = result.metrics(q)
    lp_commitment = result.config.fund.lp_commitment
    rows = []
    for investor in investors:
        share = investor.commitment / lp_commitment
        paid_in, distributed, nav = m["paid_in"] * share, m["distributed"] * share, m["nav"] * share
        rows.append(
            {
                "investor": investor.name,
                "commitment": investor.commitment,
                "share_of_lp_class": share,
                "paid_in": paid_in,
                "unfunded": m["unfunded_commitment"] * share,
                "distributions": distributed,
                "nav": nav,
                "total_value": distributed + nav,
                "dpi": m["dpi"],
                "rvpi": m["rvpi"],
                "tvpi": m["tvpi"],
                "net_irr": m["net_irr"],
            }
        )
    return pd.DataFrame(rows)


def public_market_equivalent(
    result: FundResult, benchmark_return: float = 0.08, as_of: dt.date | int | None = None
) -> float:
    """Kaplan-Schoar PME of the LPs' net cash flows against a constant-return benchmark."""
    q = result.quarter_of(as_of)
    a = result.arrays
    return ks_pme(
        result.schedule.t[: q + 1],
        a["lp_contributions"][: q + 1],
        a["lp_distributions"][: q + 1],
        float(a["lp_nav"][q]),
        benchmark_return,
    )
