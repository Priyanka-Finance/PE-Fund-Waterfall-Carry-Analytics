"""Fund economics engine: capital calls, fees, distributions and the waterfall.

``run_fund`` walks the fund quarter by quarter.  In each quarter it

1. works out the management fee (net of portfolio-fee offsets) and the
   partnership expenses;
2. funds investments, fees and expenses - by a capital call, by a drawing on
   the subscription line, or (once commitments are exhausted) out of proceeds;
3. splits realisation proceeds between the LPs and the GP's own commitment;
4. runs the LP share through the distribution waterfall
   (return of capital -> preferred return -> GP catch-up -> carried-interest split);
5. values what is left: fair value, accrued carry and the LPs' net asset value.

At liquidation it tests the GP clawback and releases the carry escrow.

How the waterfall is computed
-----------------------------
Both waterfall types use one algorithm that differs only in *which capital is
eligible* at a distribution date:

* whole-of-fund ("European"): every LP contribution to date;
* deal-by-deal ("American"): LP capital in realised (or written-down)
  investments, plus the fees and expenses allocated to them.

For a set of eligible contributions ``c_j`` made at ``t_j`` the amount needed
to return capital plus a compounding preferred return at time ``t`` is

    hurdle(t) = sum_j c_j * (1 + r)^(t - t_j)  -  sum_k p_k * (1 + r)^(t - t_k)

where ``p_k`` are the return-of-capital and preferred-return payments already
made.  This is algebraically identical to the familiar running ledger
(opening balance + accrual - payments) but it also copes with the eligible set
changing over time, which is exactly what happens deal by deal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

import numpy as np
import pandas as pd

from .config import ConfigError, FundConfig
from .deals import DealSchedule, build_deal_schedule
from .metrics import irr_from_years, safe_ratio
from .timeline import quarter_index

EPS = 1e-9


# --------------------------------------------------------------------------- #
# Waterfall building blocks
# --------------------------------------------------------------------------- #
def split_tiers(
    amount: float,
    capital_outstanding: float,
    pref_outstanding: float,
    profit_distributed: float,
    carry_distributed: float,
    carry_rate: float,
    catch_up_rate: float,
) -> tuple[float, float, float, float, float, float]:
    """Run one LP-class distribution through the four waterfall tiers.

    Parameters
    ----------
    amount
        Cash available to the LP class at this date.
    capital_outstanding, pref_outstanding
        Eligible capital not yet returned and preferred return accrued but unpaid.
    profit_distributed
        Cumulative profit distributions so far (tiers 2-4, LP and GP together).
    carry_distributed
        Cumulative carried interest distributed to the GP so far.

    Returns
    -------
    ``(return_of_capital, preferred_return, catch_up_gp, catch_up_lp, split_lp, split_gp)``
    """
    remaining = max(amount, 0.0)

    # Tier 1 - return of capital: 100% to LPs.
    roc = min(remaining, capital_outstanding)
    remaining -= roc

    # Tier 2 - preferred return: 100% to LPs.
    pref = min(remaining, pref_outstanding)
    remaining -= pref

    # Tier 3 - GP catch-up: the GP takes ``catch_up_rate`` of each dollar until its
    # cumulative carry equals ``carry_rate`` of all profit distributed so far.
    catch_up_gp = catch_up_lp = 0.0
    if catch_up_rate > carry_rate and remaining > 0.0:
        profit = profit_distributed + pref
        needed = (carry_rate * profit - carry_distributed) / (catch_up_rate - carry_rate)
        catch_up = min(remaining, max(needed, 0.0))
        catch_up_gp = catch_up_rate * catch_up
        catch_up_lp = catch_up - catch_up_gp
        remaining -= catch_up

    # Tier 4 - residual split, e.g. 80 / 20.
    split_gp = carry_rate * remaining
    split_lp = remaining - split_gp
    return roc, pref, catch_up_gp, catch_up_lp, split_lp, split_gp


def clawback_due(
    contributions: float,
    lp_distributions: float,
    carry: float,
    pref_accrued: float,
    carry_rate: float,
    enabled: bool,
    after_tax_limit: bool,
    tax_rate: float,
) -> tuple[float, float]:
    """GP clawback on a whole-of-fund basis.

    The standard two-pronged test.  The GP returns the greater of

    * (A) carry received in excess of ``carry_rate`` x cumulative net profit, and
    * (B) the shortfall of LP distributions against contributed capital plus
      the preferred return on it,

    never more than the carry it received and, if ``after_tax_limit`` is set,
    never more than that carry net of the assumed tax on it.

    Returns ``(clawback_payable, uncapped_amount)``; the difference is what the
    after-tax cap costs the LPs.
    """
    if carry <= EPS:
        return 0.0, 0.0
    profit = lp_distributions + carry - contributions
    excess_carry = max(carry - carry_rate * max(profit, 0.0), 0.0)
    lp_shortfall = max(contributions + pref_accrued - lp_distributions, 0.0)
    uncapped = min(carry, max(excess_carry, lp_shortfall))
    if uncapped <= EPS:
        return 0.0, 0.0
    if not enabled:
        return 0.0, uncapped
    cap = carry * (1.0 - tax_rate) if after_tax_limit else carry
    return min(uncapped, cap), uncapped


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class FundResult:
    """Everything produced by one run of the fund model."""

    config: FundConfig
    schedule: DealSchedule
    arrays: dict[str, np.ndarray]
    totals: dict[str, float]
    flags: list[str] = field(default_factory=list)
    interim: bool = True
    #: LP capital called, by purpose (one row per deal, then fees, then expenses) and quarter.
    lp_capital_by_kind: np.ndarray | None = None
    #: Share of each kind of capital that was eligible for return in each quarter's waterfall.
    eligibility: np.ndarray | None = None

    @property
    def kind_names(self) -> list[str]:
        """Row labels of ``lp_capital_by_kind`` and ``eligibility``."""
        return [*self.schedule.names, "Management fees", "Partnership expenses"]

    # ------------------------------------------------------------------ #
    @property
    def dates(self) -> list[dt.date]:
        return self.schedule.dates

    @property
    def n_quarters(self) -> int:
        return self.schedule.n_quarters

    @property
    def final_quarter(self) -> int:
        return self.schedule.n_quarters - 1

    def quarter_of(self, as_of: dt.date | int | None = None) -> int:
        """Resolve an "as of" argument (date, quarter index or ``None`` = liquidation)."""
        if as_of is None:
            return self.final_quarter
        if isinstance(as_of, (int, np.integer)):
            q = int(as_of)
        else:
            q = quarter_index(self.config.fund.first_close, as_of)
        return int(min(max(q, 0), self.final_quarter))

    @property
    def valuation_quarter(self) -> int:
        """Quarter of the configured valuation date (liquidation if none is set)."""
        return self.quarter_of(self.config.fund.valuation_date)

    # ------------------------------------------------------------------ #
    @cached_property
    def quarterly(self) -> pd.DataFrame:
        """Quarter-by-quarter cash flows, waterfall tiers, balances and metrics."""
        frame = pd.DataFrame({name: values for name, values in self.arrays.items() if values.ndim == 1})
        frame.insert(0, "date", pd.to_datetime(self.dates))
        frame.insert(0, "quarter", np.arange(self.n_quarters))
        return frame

    @cached_property
    def deals(self) -> pd.DataFrame:
        """One row per deal: cost, proceeds, MOIC, IRR and value-creation bridge."""
        s = self.schedule
        vq = self.valuation_quarter
        rows = []
        total_gain = float(sum(d["gain"] for d in s.details))
        for i, detail in enumerate(s.details):
            flows = s.proc[i] - s.inv[i]
            status = "Realised" if s.exit_q[i] <= vq else ("Unrealised" if s.entry_q[i] <= vq else "Not yet invested")
            if status == "Realised" and detail["proceeds"] <= EPS:
                status = "Written off"
            elif status == "Unrealised" and s.proc[i, : vq + 1].sum() > EPS:
                status = "Partially realised"
            rows.append(
                {
                    "deal": detail["name"],
                    "sector": detail["sector"],
                    "strategy": detail["strategy"],
                    "mode": detail["mode"],
                    "entry_date": detail["entry_date"],
                    "exit_date": detail["exit_date"],
                    "hold_years": detail["hold_years"],
                    "invested": detail["invested"],
                    "proceeds": detail["proceeds"],
                    "gain": detail["gain"],
                    "gross_moic": detail["gross_moic"],
                    "gross_irr": irr_from_years(s.t, flows),
                    "share_of_gain": safe_ratio(detail["gain"], total_gain),
                    "status_at_valuation": status,
                    "fv_at_valuation": float(s.fv[i, vq]),
                    "realised_at_valuation": float(s.proc[i, : vq + 1].sum()),
                    "invested_at_valuation": float(s.inv[i, : vq + 1].sum()),
                    "ownership": detail.get("ownership", np.nan),
                    "entry_ebitda": detail.get("entry_ebitda", np.nan),
                    "exit_ebitda": detail.get("exit_ebitda", np.nan),
                    "entry_ev_multiple": detail.get("entry_ev_multiple", np.nan),
                    "exit_ev_multiple": detail.get("exit_ev_multiple", np.nan),
                    "entry_leverage": detail.get("entry_leverage", np.nan),
                    "exit_leverage": detail.get("exit_leverage", np.nan),
                    "vc_ebitda_growth": detail.get("vc_ebitda_growth", np.nan),
                    "vc_multiple_expansion": detail.get("vc_multiple_expansion", np.nan),
                    "vc_combined": detail.get("vc_combined", np.nan),
                    "vc_deleveraging": detail.get("vc_deleveraging", np.nan),
                    "vc_equity_floor": detail.get("vc_equity_floor", np.nan),
                }
            )
        frame = pd.DataFrame(rows)
        # The operating bridge only explains deals still driven by the operating case.
        bridge_cols = [c for c in frame.columns if c.startswith("vc_")]
        frame.loc[frame["mode"] != "operating", bridge_cols] = np.nan
        return frame

    # ------------------------------------------------------------------ #
    def metrics(self, as_of: dt.date | int | None = None) -> dict[str, Any]:
        """Fund performance as of a date (default: liquidation).

        Net figures are the LPs' experience after fees, expenses and carry.
        Gross figures are deal-level, before any fund-level cost.
        """
        q = self.quarter_of(as_of)
        a, t = self.arrays, self.schedule.t
        window = slice(0, q + 1)
        nav = float(a["lp_nav"][q])
        if np.isnan(nav):
            raise ValueError("Interim NAV was not computed; run the fund with interim=True")

        paid_in = float(a["lp_contributions"][window].sum())
        distributed = float(a["lp_distributions"][window].sum())
        net_flows = a["lp_distributions"][window] - a["lp_contributions"][window]
        net_flows = net_flows.copy()
        net_flows[q] += nav

        invested = float(a["investments"][window].sum())
        proceeds = float(a["gross_proceeds"][window].sum())
        fair_value = float(a["portfolio_fv"][q])
        gross_flows = (a["gross_proceeds"][window] - a["investments"][window]).copy()
        gross_flows[q] += fair_value

        dpi = safe_ratio(distributed, paid_in)
        rvpi = safe_ratio(nav, paid_in)
        fund = self.config.fund
        return {
            "as_of": self.dates[q],
            "quarter": q,
            "is_liquidated": q == self.final_quarter,
            # Net (LP) performance
            "paid_in": paid_in,
            "distributed": distributed,
            "nav": nav,
            "dpi": dpi,
            "rvpi": rvpi,
            "tvpi": dpi + rvpi,
            "net_irr": irr_from_years(t[window], net_flows),
            "net_profit": distributed + nav - paid_in,
            "paid_in_pct_of_commitment": safe_ratio(paid_in, fund.lp_commitment),
            "unfunded_commitment": float(a["lp_unfunded"][q]),
            # Gross (deal-level) performance
            "invested": invested,
            "gross_proceeds": proceeds,
            "portfolio_fv": fair_value,
            "gross_moic": safe_ratio(proceeds + fair_value, invested),
            "gross_irr": irr_from_years(t[window], gross_flows),
            "gross_profit": proceeds + fair_value - invested,
            # Fund-level costs to date
            "management_fees": float(a["mgmt_fee_net"][window].sum()),
            "fee_offsets": float(a["fee_offset"][window].sum()),
            "expenses": float(a["expenses"][window].sum() + a["line_interest"][window].sum()),
            "carry_distributed": float(a["gp_carry"][window].sum()),
            "carry_accrued": float(a["accrued_carry"][q]),
            "clawback_paid": float(a["clawback_paid"][window].sum()),
            "clawback_exposure": float(a["clawback_exposure"][q]),
            "escrow_balance": float(a["escrow_balance"][q]),
        }

    @cached_property
    def summary(self) -> dict[str, Any]:
        """Headline numbers at liquidation plus whole-life totals."""
        out = self.metrics()
        out.update(self.totals)
        out["gross_to_net_irr_spread"] = out["gross_irr"] - out["net_irr"]
        return out


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def run_fund(
    cfg: FundConfig,
    *,
    interim: bool = True,
    schedule: DealSchedule | None = None,
) -> FundResult:
    """Run the fund model for one configuration.

    Parameters
    ----------
    cfg
        Validated fund configuration (terms, deals, shocks).
    interim
        Also compute quarter-end NAV, accrued carry and since-inception
        metrics.  Switch off for speed when only liquidation results are
        needed (Monte Carlo, sensitivity grids).
    schedule
        Pre-built deal schedule; built from ``cfg`` when omitted.
    """
    s = schedule if schedule is not None else build_deal_schedule(cfg)
    n, n_deals, t = s.n_quarters, s.n_deals, s.t
    final = n - 1
    fund, fee_terms, wf, cb = cfg.fund, cfg.management_fee, cfg.waterfall, cfg.clawback
    line = cfg.subscription_line
    lp_pct, gp_pct = fund.lp_pct, fund.gp_commitment_pct
    ip_quarters = fund.investment_period_quarters
    flags: list[str] = []

    # Kinds of LP capital: one row per deal, then management fees, then expenses.
    FEES, EXPENSES = n_deals, n_deals + 1
    contributions = np.zeros((n_deals + 2, n))

    # Deal aggregates.
    cum_inv = np.cumsum(s.inv, axis=1)
    cum_relief = np.cumsum(s.relief, axis=1)
    remaining_cost = cum_inv - cum_relief
    writedown = np.maximum(remaining_cost - s.fv, 0.0)
    inv_q, proc_q, relief_q = s.inv.sum(axis=0), s.proc.sum(axis=0), s.relief.sum(axis=0)
    fv_q = s.fv.sum(axis=0)
    cost_outstanding_q = remaining_cost.sum(axis=0)

    # Preferred-return growth factors: growth[s, q] compounds from quarter s to quarter q.
    rate, carry_rate, catch_up_rate = wf.pref_rate, wf.carry_rate, wf.catch_up_rate
    simple_pref = wf.pref_compounding == "simple"
    elapsed = t[None, :] - t[:, None]
    if wf.pref_compounding == "quarterly":
        growth = (1.0 + rate / 4.0) ** (4.0 * elapsed)
    else:
        growth = (1.0 + rate) ** elapsed
    american = wf.type == "american"
    tax_rate = cb.assumed_tax_rate

    names = [
        "investments", "gross_proceeds", "cost_relieved", "portfolio_fv", "cost_outstanding",
        "mgmt_fee_basis", "mgmt_fee_rate",
        "mgmt_fee_gross", "fee_offset", "mgmt_fee_net", "portfolio_fees", "expenses", "line_interest",
        "lp_expenses", "line_interest_accrued",
        "lp_called_investments", "lp_called_fees", "lp_called_expenses", "lp_contributions",
        "gp_contributions", "netted_from_proceeds", "recalled_capacity", "lp_unfunded",
        "lp_distributable", "tier1_return_of_capital", "tier2_preferred_return",
        "tier3_catch_up_gp", "tier3_catch_up_lp", "tier4_split_lp", "tier4_split_gp",
        "lp_distributions", "gp_carry", "carry_escrowed", "escrow_release", "escrow_balance",
        "clawback_paid", "clawback_from_escrow", "gp_carry_cash", "gp_commit_distributions",
        "lp_capital_outstanding", "lp_pref_outstanding", "line_balance",
        "accrued_carry", "clawback_exposure", "lp_nav", "gp_commit_nav",
    ]  # fmt: skip
    a = {name: np.zeros(n) for name in names}
    a["investments"], a["gross_proceeds"], a["cost_relieved"] = inv_q.copy(), proc_q.copy(), relief_q.copy()
    a["portfolio_fv"], a["cost_outstanding"] = fv_q.copy(), cost_outstanding_q.copy()
    if not interim:
        for name in ("accrued_carry", "clawback_exposure", "lp_nav", "gp_commit_nav",
                     "lp_capital_outstanding", "lp_pref_outstanding"):  # fmt: skip
            a[name][:] = np.nan
            a[name][final] = 0.0
    roc, pref = a["tier1_return_of_capital"], a["tier2_preferred_return"]

    # Running state.
    unfunded_lp, unfunded_gp = fund.lp_commitment, fund.gp_commitment
    recycle_cap_lp = cfg.recycling.cap_pct_of_commitments * fund.lp_commitment
    recycle_cap_gp = cfg.recycling.cap_pct_of_commitments * fund.gp_commitment
    recycled_lp = recycled_gp = 0.0
    offset_credit = 0.0
    payable_fee = payable_exp_lp = payable_exp_gp = 0.0
    roc_cum = pref_cum = profit_cum = carry_cum = lp_dist_cum = 0.0
    escrow = 0.0
    # Whole-of-fund "shadow" ledger used by the clawback test.
    shadow_capital = shadow_pref = shadow_pref_accrued = 0.0
    # Subscription line: principal scheduled for repayment, and interest due, by quarter.
    line_repay_lp = np.zeros((n_deals + 2, n))
    line_repay_gp = np.zeros(n)
    line_interest_lp, line_interest_gp = np.zeros(n), np.zeros(n)
    line_draw_lp, line_draw_gp = np.zeros(n), np.zeros(n)
    ones = np.ones(n_deals + 2)
    eligibility = np.ones((n_deals + 2, n))

    def hurdle_state(q: int, weights: np.ndarray, extra_capital: float = 0.0) -> tuple[float, float]:
        """Eligible capital not yet returned and preferred return accrued but unpaid."""
        paid_in = contributions[:, : q + 1]
        base = float(weights @ paid_in.sum(axis=1)) + extra_capital
        capital_out = max(base - roc_cum, 0.0)
        if simple_pref:
            age = t[q] - t[: q + 1]
            accrued = rate * (float(weights @ (paid_in @ age)) - float(roc[: q + 1] @ age))
            return capital_out, max(accrued - pref_cum, 0.0)
        factor = growth[: q + 1, q]
        hurdle = float(weights @ (paid_in @ factor)) + extra_capital
        hurdle -= float((roc[: q + 1] + pref[: q + 1]) @ factor)
        return capital_out, max(hurdle - capital_out, 0.0)

    def line_liability(q: int, draws: np.ndarray) -> tuple[float, float]:
        """Principal and accrued interest on drawings still outstanding after quarter ``q``."""
        if not line.enabled:
            return 0.0, 0.0
        k = line.quarters_outstanding
        principal = accrued = 0.0
        for d in range(max(q - k + 1, 0), q + 1):
            if draws[d] > 0.0:
                principal += draws[d]
                accrued += draws[d] * line.interest_rate * (q - d) / 4.0
        return principal, accrued

    for q in range(n):
        is_final = q == final
        in_investment_period = q < ip_quarters

        # ---- 1. Recallable distributions (recycling) ----------------------
        if cfg.recycling.enabled and q <= ip_quarters and relief_q[q] > 0.0:
            add_lp = min(relief_q[q] * lp_pct, recycle_cap_lp - recycled_lp)
            add_gp = min(relief_q[q] * gp_pct, recycle_cap_gp - recycled_gp)
            unfunded_lp += add_lp
            unfunded_gp += add_gp
            recycled_lp += add_lp
            recycled_gp += add_gp
            a["recalled_capacity"][q] = add_lp + add_gp

        # ---- 2. Management fee for the coming quarter, net of offsets -----
        portfolio_fees = fee_terms.transaction_fee_pct * inv_q[q]
        fee_gross = 0.0
        if not is_final:
            portfolio_fees += fee_terms.monitoring_fee_pct / 4.0 * cost_outstanding_q[q]
            if in_investment_period:
                fee_rate, basis_name = fee_terms.rate_investment_period, fee_terms.basis_investment_period
            else:
                fee_rate, basis_name = fee_terms.rate_post_investment_period, fee_terms.basis_post_investment_period
            if basis_name == "committed":
                basis = fund.lp_commitment
            elif basis_name == "net_invested":
                basis = lp_pct * cost_outstanding_q[q]
            else:  # nav
                basis = lp_pct * fv_q[q]
            fee_gross = fee_rate * basis / 4.0
            a["mgmt_fee_basis"][q], a["mgmt_fee_rate"][q] = basis, fee_rate
        offset_credit += fee_terms.offset_pct * portfolio_fees * lp_pct
        offset_used = min(offset_credit, fee_gross)
        offset_credit -= offset_used
        fee_net = fee_gross - offset_used
        a["mgmt_fee_gross"][q], a["fee_offset"][q], a["mgmt_fee_net"][q] = fee_gross, offset_used, fee_net
        a["portfolio_fees"][q] = portfolio_fees

        # ---- 3. Partnership expenses ---------------------------------------
        expenses = cfg.expenses.organizational if q == 0 else 0.0
        if not is_final:
            expenses += cfg.expenses.annual_operating / 4.0
        a["expenses"][q] = expenses

        # ---- 4. Funding: subscription line or capital call ----------------
        new_lp = np.zeros(n_deals + 2)
        new_lp[:n_deals] = s.inv[:, q] * lp_pct
        new_lp[FEES] = fee_net
        new_lp[EXPENSES] = expenses * lp_pct
        new_gp = (inv_q[q] + expenses) * gp_pct

        k = line.quarters_outstanding
        use_line = (
            line.enabled
            and in_investment_period
            and q + k <= final
            and new_lp.sum() <= unfunded_lp + EPS
            and new_gp <= unfunded_gp + EPS
            and (new_lp.sum() + new_gp) > 0.0
        )
        if use_line:
            line_repay_lp[:, q + k] += new_lp
            line_repay_gp[q + k] += new_gp
            line_interest_lp[q + k] += new_lp.sum() * line.interest_rate * k / 4.0
            line_interest_gp[q + k] += new_gp * line.interest_rate * k / 4.0
            line_draw_lp[q], line_draw_gp[q] = new_lp.sum(), new_gp
            unfunded_lp -= new_lp.sum()  # commitment is reserved for the repayment call
            unfunded_gp -= new_gp
            new_lp = np.zeros(n_deals + 2)
            new_gp_inv = new_gp_exp = 0.0
        else:
            new_gp_inv, new_gp_exp = inv_q[q] * gp_pct, expenses * gp_pct
        a["line_interest"][q] = line_interest_lp[q] + line_interest_gp[q]
        a["lp_expenses"][q] = expenses * lp_pct + line_interest_lp[q]

        # LP capital call.  Investments first, then expenses, then fees.
        call = line_repay_lp[:, q].copy()  # repayment of earlier drawings (capacity already reserved)
        investment_need = float(new_lp[:n_deals].sum())
        if investment_need > unfunded_lp + 1e-6:
            raise ConfigError(
                f"Over-commitment in quarter {q} ({s.dates[q]}): investments need {investment_need:,.2f} "
                f"of LP capital but only {max(unfunded_lp, 0.0):,.2f} is unfunded. Reduce deal sizes, "
                f"lower fees or enable recycling."
            )
        call[:n_deals] += new_lp[:n_deals]
        unfunded_lp -= investment_need
        expense_need = new_lp[EXPENSES] + line_interest_lp[q] + payable_exp_lp
        fee_need = new_lp[FEES] + payable_fee
        expense_called = min(expense_need, max(unfunded_lp, 0.0))
        unfunded_lp -= expense_called
        fee_called = min(fee_need, max(unfunded_lp, 0.0))
        unfunded_lp -= fee_called
        call[EXPENSES] += expense_called
        call[FEES] += fee_called
        contributions[:, q] = call

        # Once commitments run out, fees and expenses are paid out of proceeds;
        # if there are none this quarter they are carried as a payable.
        lp_proceeds = proc_q[q] * lp_pct
        expense_netted = min(expense_need - expense_called, lp_proceeds)
        fee_netted = min(fee_need - fee_called, lp_proceeds - expense_netted)
        payable_exp_lp = expense_need - expense_called - expense_netted
        payable_fee = fee_need - fee_called - fee_netted
        a["netted_from_proceeds"][q] = expense_netted + fee_netted
        distributable = lp_proceeds - expense_netted - fee_netted

        # GP commitment: same mechanics, no management fee.
        if new_gp_inv > unfunded_gp + 1e-6:
            raise ConfigError(f"Over-commitment of the GP commitment in quarter {q} ({s.dates[q]})")
        unfunded_gp -= new_gp_inv
        gp_expense_need = new_gp_exp + line_interest_gp[q] + payable_exp_gp
        gp_expense_called = min(gp_expense_need, max(unfunded_gp, 0.0))
        unfunded_gp -= gp_expense_called
        gp_proceeds = proc_q[q] * gp_pct
        gp_expense_netted = min(gp_expense_need - gp_expense_called, gp_proceeds)
        payable_exp_gp = gp_expense_need - gp_expense_called - gp_expense_netted
        a["gp_contributions"][q] = line_repay_gp[q] + new_gp_inv + gp_expense_called
        a["gp_commit_distributions"][q] = gp_proceeds - gp_expense_netted

        a["lp_called_investments"][q] = call[:n_deals].sum()
        a["lp_called_fees"][q] = call[FEES]
        a["lp_called_expenses"][q] = call[EXPENSES]
        a["lp_contributions"][q] = call.sum()
        a["lp_unfunded"][q] = unfunded_lp
        a["lp_distributable"][q] = distributable

        # ---- 5. Distribution waterfall -------------------------------------
        if american:
            realised = cum_relief[:, q] + (writedown[:, q] if wf.writedowns_as_realized else 0.0)
            invested = cum_inv[:, q]
            weights = np.ones(n_deals + 2)
            np.divide(realised, invested, out=weights[:n_deals], where=invested > 0.0)
            weights[:n_deals] = np.where(invested > 0.0, np.clip(weights[:n_deals], 0.0, 1.0), 0.0)
            total_invested = float(invested.sum())
            if wf.expense_allocation == "all_to_date":
                share = 1.0
            else:
                share = min(float(realised.sum()) / total_invested, 1.0) if total_invested > 0.0 else 0.0
            weights[FEES] = weights[EXPENSES] = share
        else:
            weights = ones

        eligibility[:, q] = weights
        capital_out, pref_out = hurdle_state(q, weights)
        t1, t2, cu_gp, cu_lp, sp_lp, sp_gp = split_tiers(
            distributable, capital_out, pref_out, profit_cum, carry_cum, carry_rate, catch_up_rate
        )
        roc[q], pref[q] = t1, t2
        a["tier3_catch_up_gp"][q], a["tier3_catch_up_lp"][q] = cu_gp, cu_lp
        a["tier4_split_lp"][q], a["tier4_split_gp"][q] = sp_lp, sp_gp
        lp_dist = t1 + t2 + cu_lp + sp_lp
        carry = cu_gp + sp_gp
        roc_cum += t1
        pref_cum += t2
        profit_cum += t2 + cu_gp + cu_lp + sp_lp + sp_gp
        carry_cum += carry
        lp_dist_cum += lp_dist
        a["lp_distributions"][q] = lp_dist
        a["gp_carry"][q] = carry

        # Carry escrow (holdback) securing the clawback.
        escrowed = cb.escrow_pct * carry if not is_final else 0.0
        escrow += escrowed
        a["carry_escrowed"][q] = escrowed
        a["gp_carry_cash"][q] = carry - escrowed

        # ---- 6. Whole-of-fund shadow ledger for the clawback test ----------
        if q > 0:
            if simple_pref:
                accrual = shadow_capital * rate * (t[q] - t[q - 1])
            else:
                accrual = (shadow_capital + shadow_pref) * (growth[q - 1, q] - 1.0)
            shadow_pref += accrual
            shadow_pref_accrued += accrual
        shadow_capital += call.sum()
        to_capital = min(lp_dist, shadow_capital)
        shadow_capital -= to_capital
        shadow_pref -= min(lp_dist - to_capital, shadow_pref)

        # ---- 7. Liquidation: clawback and escrow release -------------------
        if is_final:
            due, uncapped = clawback_due(
                float(contributions.sum()), lp_dist_cum, carry_cum, shadow_pref_accrued,
                carry_rate, cb.enabled, cb.after_tax_limit, tax_rate,
            )  # fmt: skip
            from_escrow = min(due, escrow)
            a["clawback_paid"][q] = due
            a["clawback_from_escrow"][q] = from_escrow
            a["escrow_release"][q] = escrow - from_escrow
            a["lp_distributions"][q] += due
            a["gp_carry_cash"][q] += (escrow - from_escrow) - (due - from_escrow)
            lp_dist_cum += due
            escrow = 0.0
            clawback_uncapped = uncapped
            if payable_fee + payable_exp_lp + payable_exp_gp > 1e-6:
                flags.append(
                    "Fees or expenses of "
                    f"{payable_fee + payable_exp_lp + payable_exp_gp:,.2f} were still unpaid at liquidation."
                )
        a["escrow_balance"][q] = escrow

        # ---- 8. Interim valuation: hypothetical liquidation at fair value --
        if interim:
            cap_now, pref_now = hurdle_state(q, weights)
            a["lp_capital_outstanding"][q], a["lp_pref_outstanding"][q] = cap_now, pref_now
            principal_lp, accrued_lp = line_liability(q, line_draw_lp)
            principal_gp, accrued_gp = line_liability(q, line_draw_gp)
            liability_lp, liability_gp = principal_lp + accrued_lp, principal_gp + accrued_gp
            a["line_balance"][q] = principal_lp + principal_gp
            a["line_interest_accrued"][q] = accrued_lp + accrued_gp
            if not is_final:
                # If everything were sold at fair value today, all capital becomes eligible.
                hypothetical = max(lp_pct * fv_q[q] - payable_fee - payable_exp_lp, 0.0)
                cap_h, pref_h = hurdle_state(q, ones, extra_capital=liability_lp)
                h1, h2, hcu_gp, hcu_lp, hsp_lp, hsp_gp = split_tiers(
                    hypothetical, cap_h, pref_h, profit_cum, carry_cum, carry_rate, catch_up_rate
                )
                lp_h = h1 + h2 + hcu_lp + hsp_lp
                carry_h = hcu_gp + hsp_gp
                exposure, _ = clawback_due(
                    float(contributions[:, : q + 1].sum()) + liability_lp, lp_dist_cum + lp_h,
                    carry_cum + carry_h, shadow_pref_accrued, carry_rate,
                    cb.enabled, cb.after_tax_limit, tax_rate,
                )  # fmt: skip
                a["accrued_carry"][q] = carry_h
                a["clawback_exposure"][q] = exposure
                a["lp_nav"][q] = lp_h + exposure - liability_lp
                a["gp_commit_nav"][q] = gp_pct * fv_q[q] - liability_gp - payable_exp_gp

    if cfg.subscription_line.enabled and line_draw_lp.sum() == 0.0:
        flags.append("Subscription line is enabled but was never drawn.")
    if a["netted_from_proceeds"].sum() > 1e-6:
        flags.append(
            f"Commitments were exhausted: {a['netted_from_proceeds'].sum():,.2f} of fees and expenses "
            "was paid out of distribution proceeds."
        )

    # ---- Whole-life totals -------------------------------------------------
    lp_contrib_total = float(a["lp_contributions"].sum())
    lp_dist_total = float(a["lp_distributions"].sum())
    carry_gross = float(a["gp_carry"].sum())
    clawback = float(a["clawback_paid"].sum())
    carry_net = carry_gross - clawback
    fees = float(a["mgmt_fee_net"].sum())
    expenses_total = float(a["expenses"].sum() + a["line_interest"].sum())
    gross_profit = float(proc_q.sum() - inv_q.sum())
    gp_commit_profit = float(a["gp_commit_distributions"].sum() - a["gp_contributions"].sum())
    carry_dates = np.nonzero(a["gp_carry"] > 1e-9)[0]
    totals = {
        "committed_capital": fund.committed_capital,
        "lp_commitment": fund.lp_commitment,
        "gp_commitment": fund.gp_commitment,
        "total_invested": float(inv_q.sum()),
        "total_gross_proceeds": float(proc_q.sum()),
        "total_gross_profit": gross_profit,
        "total_lp_contributions": lp_contrib_total,
        "total_lp_distributions": lp_dist_total,
        "total_lp_profit": lp_dist_total - lp_contrib_total,
        "total_management_fees": fees,
        "total_fee_offsets": float(a["fee_offset"].sum()),
        "total_portfolio_fees": float(a["portfolio_fees"].sum()),
        "total_expenses": expenses_total,
        "total_line_interest": float(a["line_interest"].sum()),
        "tier1_return_of_capital": float(roc.sum()),
        "tier2_preferred_return": float(pref.sum()),
        "tier3_catch_up": float(a["tier3_catch_up_gp"].sum() + a["tier3_catch_up_lp"].sum()),
        "tier3_catch_up_gp": float(a["tier3_catch_up_gp"].sum()),
        "tier4_split_lp": float(a["tier4_split_lp"].sum()),
        "tier4_split_gp": float(a["tier4_split_gp"].sum()),
        "carry_gross": carry_gross,
        "clawback_paid": clawback,
        "clawback_uncapped": float(clawback_uncapped),
        "clawback_shortfall": float(clawback_uncapped - clawback),
        "carry_net": carry_net,
        "peak_escrow": float(a["escrow_balance"].max()),
        "whole_fund_pref_accrued": float(shadow_pref_accrued),
        "carry_pct_of_lp_class_profit": safe_ratio(carry_net, lp_dist_total - lp_contrib_total + carry_net),
        "first_carry_quarter": int(carry_dates[0]) if len(carry_dates) else -1,
        "first_carry_date": s.dates[int(carry_dates[0])] if len(carry_dates) else None,
        "gp_commit_contributions": float(a["gp_contributions"].sum()),
        "gp_commit_distributions": float(a["gp_commit_distributions"].sum()),
        "gp_commit_profit": gp_commit_profit,
        "gp_total_economics": fees + carry_net + gp_commit_profit,
        "gp_share_of_gross_profit": safe_ratio(fees + carry_net + gp_commit_profit, gross_profit),
        "lp_share_of_gross_profit": safe_ratio(lp_dist_total - lp_contrib_total, gross_profit),
        "fund_life_years": float(t[final]),
        "recycled_capital": recycled_lp + recycled_gp,
        "peak_line_balance": float(a["line_balance"].max()) if interim else float("nan"),
    }

    result = FundResult(
        config=cfg, schedule=s, arrays=a, totals=totals, flags=flags, interim=interim,
        lp_capital_by_kind=contributions, eligibility=eligibility,
    )  # fmt: skip
    if interim:
        _add_since_inception_metrics(result)
    return result


def _add_since_inception_metrics(result: FundResult) -> None:
    """Cumulative paid-in, DPI / RVPI / TVPI, IRR and J-curve series for every quarter."""
    a, t, n = result.arrays, result.schedule.t, result.n_quarters
    paid_in = np.cumsum(a["lp_contributions"])
    distributed = np.cumsum(a["lp_distributions"])
    with np.errstate(divide="ignore", invalid="ignore"):
        dpi = np.where(paid_in > 0, distributed / paid_in, np.nan)
        rvpi = np.where(paid_in > 0, a["lp_nav"] / paid_in, np.nan)
        invested = np.cumsum(a["investments"])
        gross_moic = np.where(invested > 0, (np.cumsum(a["gross_proceeds"]) + a["portfolio_fv"]) / invested, np.nan)
    net_irr, gross_irr = np.full(n, np.nan), np.full(n, np.nan)
    lp_net = a["lp_distributions"] - a["lp_contributions"]
    gross_net = a["gross_proceeds"] - a["investments"]
    for q in range(1, n):
        flows = lp_net[: q + 1].copy()
        flows[q] += a["lp_nav"][q]
        net_irr[q] = irr_from_years(t[: q + 1], flows)
        flows = gross_net[: q + 1].copy()
        flows[q] += a["portfolio_fv"][q]
        gross_irr[q] = irr_from_years(t[: q + 1], flows)
    a["cum_paid_in"], a["cum_distributed"] = paid_in, distributed
    a["cum_net_cash_flow"] = np.cumsum(lp_net)
    a["dpi"], a["rvpi"], a["tvpi"] = dpi, rvpi, dpi + rvpi
    a["net_irr"], a["gross_irr"], a["gross_moic"] = net_irr, gross_irr, gross_moic
