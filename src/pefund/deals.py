"""Deal-level cash flows, cost relief and fair-value marks.

This module turns the deal assumptions (plus any scenario shocks) into four
``deals x quarters`` arrays that the fund engine consumes:

``inv``     equity invested (cost)
``proc``    gross proceeds received (dividends, partial exits, final exit)
``relief``  cost basis relieved by each realisation
``fv``      fair value of the unrealised holding at each quarter-end

Two valuation modes are supported per deal:

* **direct** - total proceeds = ``gross_moic`` x cost;
* **operating** - an LBO-style driver model: equity value at exit =
  EBITDA x EV/EBITDA multiple - net debt, scaled by the fund's ownership.
  This is what makes the "exit multiple" and "holding period" sensitivities
  economically meaningful: a longer hold compounds EBITDA and pays down debt.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Deal, FundConfig
from .timeline import quarter_grid, quarter_index, year_fractions


@dataclass
class DealSchedule:
    """Arrays describing every deal on the fund's quarter-end grid."""

    names: list[str]
    dates: list[dt.date]
    t: np.ndarray  # years since first close, one per quarter
    inv: np.ndarray  # (deals, quarters)
    proc: np.ndarray
    relief: np.ndarray
    fv: np.ndarray
    entry_q: np.ndarray
    exit_q: np.ndarray
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def n_deals(self) -> int:
        return len(self.names)

    @property
    def n_quarters(self) -> int:
        return len(self.dates)


def _operating_outcome(deal: Deal, hold_years: float, exit_factor: float, growth_shift: float) -> dict[str, float]:
    """Exit equity value and value-creation bridge for an operating-case deal."""
    op = deal.operating
    assert op is not None
    invested = deal.invested
    ownership = invested / op.entry_equity

    growth = max(op.ebitda_growth + growth_shift, -0.95)
    entry_ebitda = op.entry_ebitda
    exit_ebitda = entry_ebitda * (1.0 + growth) ** hold_years
    entry_multiple = op.entry_ev_multiple
    exit_multiple = op.exit_ev_multiple * exit_factor

    # Cumulative EBITDA over the hold (continuous compounding of the CAGR).
    if abs(growth) < 1e-12:
        cumulative_ebitda = entry_ebitda * hold_years
    else:
        cumulative_ebitda = entry_ebitda * ((1.0 + growth) ** hold_years - 1.0) / math.log(1.0 + growth)

    entry_net_debt = entry_ebitda * op.entry_leverage
    exit_net_debt = entry_net_debt - op.cash_conversion * cumulative_ebitda
    exit_ev = exit_ebitda * exit_multiple
    exit_equity_unfloored = exit_ev - exit_net_debt
    exit_equity = max(exit_equity_unfloored, 0.0)  # limited liability: equity cannot go below zero

    return {
        "ownership": ownership,
        "entry_ebitda": entry_ebitda,
        "exit_ebitda": exit_ebitda,
        "entry_ev_multiple": entry_multiple,
        "exit_ev_multiple": exit_multiple,
        "entry_net_debt": entry_net_debt,
        "exit_net_debt": exit_net_debt,
        "entry_leverage": op.entry_leverage,
        "exit_leverage": exit_net_debt / exit_ebitda if exit_ebitda > 0 else float("nan"),
        "total_proceeds": ownership * exit_equity,
        # Value-creation bridge, in fund-share money terms; sums to proceeds - cost.
        "vc_ebitda_growth": ownership * (exit_ebitda - entry_ebitda) * entry_multiple,
        "vc_multiple_expansion": ownership * (exit_multiple - entry_multiple) * entry_ebitda,
        "vc_combined": ownership * (exit_ebitda - entry_ebitda) * (exit_multiple - entry_multiple),
        "vc_deleveraging": ownership * (entry_net_debt - exit_net_debt),
        "vc_equity_floor": ownership * (exit_equity - exit_equity_unfloored),
    }


def build_deal_schedule(cfg: FundConfig) -> DealSchedule:
    """Project every deal onto the quarter grid, applying ``cfg.shocks``."""
    first_close = cfg.fund.first_close
    max_q = cfg.fund.max_quarters
    shocks = cfg.shocks
    hold_at_cost = cfg.valuation.hold_at_cost_quarters

    plans: list[dict[str, Any]] = []
    for deal in cfg.deals:
        override = shocks.deal_overrides.get(deal.name, {})
        exit_factor = (1.0 + shocks.exit_multiple_pct) * (1.0 + float(override.get("exit_multiple_pct", 0.0)))
        exit_factor = max(exit_factor, 0.0)
        growth_shift = shocks.ebitda_growth_shift + float(override.get("ebitda_growth_shift", 0.0))
        hold_shift = shocks.hold_shift_quarters + int(override.get("hold_shift_quarters", 0))

        investments = [(quarter_index(first_close, i.date), i.amount) for i in deal.investments]
        entry_q = investments[0][0]
        last_investment_q = investments[-1][0]
        base_exit_q = quarter_index(first_close, deal.exit_date)
        # An exit can never precede the last cheque, nor run past the extended fund term.
        exit_q = min(max(base_exit_q + hold_shift, last_investment_q + 1), max_q)
        applied_shift = exit_q - base_exit_q
        hold_years = (exit_q - entry_q) / 4.0
        invested = deal.invested

        detail: dict[str, Any] = {"mode": "operating" if deal.operating else "direct"}
        if deal.operating is not None:
            detail.update(_operating_outcome(deal, hold_years, exit_factor, growth_shift))
            total_proceeds = detail["total_proceeds"]
        else:
            total_proceeds = float(deal.gross_moic) * invested * exit_factor

        if override.get("gross_moic") is not None:
            total_proceeds = float(override["gross_moic"]) * invested
            detail["mode"] = "direct"  # the operating bridge no longer explains the outcome
        if override.get("write_off"):
            total_proceeds = 0.0
            detail["mode"] = "direct"

        # Interim events move with the exit; any that would land on or after it fold into the exit.
        events = []
        for event in deal.proceeds_events:
            event_q = quarter_index(first_close, event.date) + applied_shift
            if entry_q < event_q < exit_q:
                events.append((event_q, event))

        plans.append(
            {
                "deal": deal,
                "investments": investments,
                "entry_q": entry_q,
                "exit_q": exit_q,
                "invested": invested,
                "total_proceeds": max(total_proceeds, 0.0),
                "events": sorted(events, key=lambda pair: pair[0]),
                "detail": detail,
            }
        )

    n_quarters = max(plan["exit_q"] for plan in plans) + 1
    n_deals = len(plans)
    dates = quarter_grid(first_close, n_quarters)
    t = year_fractions(dates)

    inv = np.zeros((n_deals, n_quarters))
    proc = np.zeros((n_deals, n_quarters))
    relief = np.zeros((n_deals, n_quarters))
    fv = np.zeros((n_deals, n_quarters))

    for i, plan in enumerate(plans):
        entry_q, exit_q, total = plan["entry_q"], plan["exit_q"], plan["total_proceeds"]
        for q, amount in plan["investments"]:
            inv[i, q] += amount

        cost_invested = np.cumsum(inv[i])
        relieved = 0.0
        paid = 0.0
        for q, event in plan["events"]:
            amount = event.share * total
            remaining_cost = cost_invested[q] - relieved
            if event.kind == "partial_exit":
                cost_out = float(event.stake_sold) * remaining_cost
            else:
                # Dividend recap: cost-recovery convention, proceeds return capital first.
                cost_out = min(amount, remaining_cost)
            proc[i, q] += amount
            relief[i, q] += cost_out
            relieved += cost_out
            paid += amount
        proc[i, exit_q] += total - paid
        relief[i, exit_q] += cost_invested[exit_q] - relieved

        # Fair value: held at cost, then converges linearly to the value still to be realised.
        remaining_cost = np.cumsum(inv[i]) - np.cumsum(relief[i])
        future_proceeds = total - np.cumsum(proc[i])
        ramp_start = entry_q + hold_at_cost
        for q in range(entry_q, exit_q):
            if exit_q > ramp_start:
                weight = min(max((q - ramp_start) / (exit_q - ramp_start), 0.0), 1.0)
            else:
                weight = 0.0
            fv[i, q] = (1.0 - weight) * remaining_cost[q] + weight * future_proceeds[q]

        deal = plan["deal"]
        plan["detail"].update(
            {
                "name": deal.name,
                "sector": deal.sector,
                "strategy": deal.strategy,
                "entry_date": dates[entry_q],
                "exit_date": dates[exit_q],
                "hold_years": (exit_q - entry_q) / 4.0,
                "invested": plan["invested"],
                "proceeds": total,
                "gain": total - plan["invested"],
                "gross_moic": total / plan["invested"],
            }
        )

    return DealSchedule(
        names=[plan["deal"].name for plan in plans],
        dates=dates,
        t=t,
        inv=inv,
        proc=proc,
        relief=relief,
        fv=fv,
        entry_q=np.array([plan["entry_q"] for plan in plans]),
        exit_q=np.array([plan["exit_q"] for plan in plans]),
        details=[plan["detail"] for plan in plans],
    )
