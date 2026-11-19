"""Build the "fact pack": the only numbers the commentary layer is allowed to use.

The language model never sees the fund model itself and never does arithmetic.
It receives this compact, pre-rounded dictionary of results and must write from
it.  Every figure in the generated text is later checked against the same
dictionary (see :mod:`pefund.ai.guardrails`), so a number the model invents or
miscalculates is caught rather than published.

Conventions inside the pack
---------------------------
* keys ending ``_pct`` are percentages in percent units (``15.2`` means 15.2%);
* keys ending ``_bps`` are basis points; keys ending ``_x`` are multiples;
* keys ending ``_years`` / ``_quarters`` are durations;
* everything else numeric is money in the fund's reporting unit.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from ..fund import FundResult


def _num(value: Any, digits: int = 1) -> float | None:
    """Round for the pack; ``None`` for missing or undefined values."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    rounded = round(number, digits)
    return 0.0 if rounded == 0 else rounded  # avoid "-0.0"


def _pct(value: Any, digits: int = 1) -> float | None:
    """A fraction expressed in percent units, e.g. 0.152 -> 15.2."""
    number = _num(value, 12)
    return None if number is None else _num(number * 100.0, digits)


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return f"{value:%Y-%m-%d}"
    return str(value)


def _distribution_events(result: FundResult, limit: int = 16) -> list[dict[str, Any]]:
    """Quarter-by-quarter waterfall for every quarter that distributed cash."""
    a = result.arrays
    events = []
    for q in np.nonzero(a["lp_distributable"] > 1e-9)[0]:
        events.append(
            {
                "date": _date(result.dates[int(q)]),
                "lp_distributable": _num(a["lp_distributable"][q]),
                "tier1_return_of_capital": _num(a["tier1_return_of_capital"][q]),
                "tier2_preferred_return": _num(a["tier2_preferred_return"][q]),
                "tier3_catch_up_to_gp": _num(a["tier3_catch_up_gp"][q]),
                "tier4_to_lps": _num(a["tier4_split_lp"][q] + a["tier3_catch_up_lp"][q]),
                "tier4_to_gp": _num(a["tier4_split_gp"][q]),
                "gp_carry": _num(a["gp_carry"][q]),
                "capital_still_to_return": _num(a["lp_capital_outstanding"][q]),
                "pref_still_to_pay": _num(a["lp_pref_outstanding"][q]),
            }
        )
    return events[:limit]


def build_fact_pack(bundle: dict[str, Any]) -> dict[str, Any]:
    """Assemble every reportable figure from a pipeline bundle."""
    result: FundResult = bundle["result"]
    cfg = result.config
    summary, valuation = bundle["summary"], bundle["valuation"]
    fund, wf, fees, cb = cfg.fund, cfg.waterfall, cfg.management_fee, cfg.clawback
    totals = result.totals

    pack: dict[str, Any] = {
        "note": "Hypothetical fund. Money is in the unit stated under fund.money_unit.",
        "fund": {
            "name": fund.name,
            "money_unit": f"{fund.currency} {fund.unit}",
            "currency": fund.currency,
            "committed_capital": _num(fund.committed_capital),
            "lp_commitments": _num(fund.lp_commitment),
            "gp_commitment": _num(fund.gp_commitment),
            "gp_commitment_pct": _pct(fund.gp_commitment_pct),
            "first_close": _date(fund.first_close),
            "investment_period_years": _num(fund.investment_period_years),
            "term_years": _num(fund.term_years),
            "fund_life_years": _num(summary["fund_life_years"]),
            "number_of_deals": len(cfg.deals),
            "number_of_investors": len(cfg.investors),
        },
        "terms": {
            "waterfall_type": "whole-of-fund (European)" if wf.type == "european" else "deal-by-deal (American)",
            "preferred_return_pct": _pct(wf.pref_rate),
            "preferred_return_compounding": wf.pref_compounding,
            "gp_catch_up_pct": _pct(wf.catch_up_rate),
            "carried_interest_pct": _pct(wf.carry_rate),
            "lp_share_of_residual_pct": _pct(1.0 - wf.carry_rate),
            "management_fee_investment_period_pct": _pct(fees.rate_investment_period, 2),
            "management_fee_basis_investment_period": fees.basis_investment_period,
            "management_fee_after_investment_period_pct": _pct(fees.rate_post_investment_period, 2),
            "management_fee_basis_after_investment_period": fees.basis_post_investment_period,
            "fee_offset_pct": _pct(fees.offset_pct),
            "clawback_enabled": cb.enabled,
            "clawback_after_tax_limit": cb.after_tax_limit,
            "clawback_assumed_tax_rate_pct": _pct(cb.assumed_tax_rate),
            "carry_escrow_pct": _pct(cb.escrow_pct),
            "subscription_line": cfg.subscription_line.enabled,
            "recycling": cfg.recycling.enabled,
        },
        "performance_at_liquidation": {
            "net_irr_pct": _pct(summary["net_irr"]),
            "net_tvpi_x": _num(summary["tvpi"], 2),
            "dpi_x": _num(summary["dpi"], 2),
            "gross_irr_pct": _pct(summary["gross_irr"]),
            "gross_moic_x": _num(summary["gross_moic"], 2),
            "gross_to_net_irr_spread_bps": _num(summary["gross_to_net_irr_spread"] * 1e4, 0),
            "lp_paid_in": _num(summary["paid_in"]),
            "lp_distributions": _num(summary["distributed"]),
            "lp_net_profit": _num(summary["net_profit"]),
            "total_invested": _num(summary["total_invested"]),
            "total_gross_proceeds": _num(summary["total_gross_proceeds"]),
            "total_gross_profit": _num(summary["total_gross_profit"]),
            "paid_in_pct_of_lp_commitments": _pct(summary["paid_in_pct_of_commitment"]),
        },
        "performance_at_valuation_date": {
            "as_of": _date(valuation["as_of"]),
            "dpi_x": _num(valuation["dpi"], 2),
            "rvpi_x": _num(valuation["rvpi"], 2),
            "net_tvpi_x": _num(valuation["tvpi"], 2),
            "net_irr_pct": _pct(valuation["net_irr"]),
            "gross_moic_x": _num(valuation["gross_moic"], 2),
            "gross_irr_pct": _pct(valuation["gross_irr"]),
            "lp_paid_in": _num(valuation["paid_in"]),
            "lp_distributions": _num(valuation["distributed"]),
            "lp_net_asset_value": _num(valuation["nav"]),
            "accrued_carry": _num(valuation["carry_accrued"]),
            "paid_in_pct_of_lp_commitments": _pct(valuation["paid_in_pct_of_commitment"]),
            "unfunded_commitment": _num(valuation["unfunded_commitment"]),
        },
        "waterfall": {
            "tier1_return_of_capital": _num(totals["tier1_return_of_capital"]),
            "tier2_preferred_return": _num(totals["tier2_preferred_return"]),
            "tier3_gp_catch_up": _num(totals["tier3_catch_up_gp"]),
            "tier4_to_lps": _num(totals["tier4_split_lp"] + totals["tier3_catch_up"] - totals["tier3_catch_up_gp"]),
            "tier4_to_gp": _num(totals["tier4_split_gp"]),
            "carry_distributed": _num(totals["carry_gross"]),
            "clawback_paid": _num(totals["clawback_paid"]),
            "clawback_before_after_tax_cap": _num(totals["clawback_uncapped"]),
            "lp_shortfall_from_after_tax_cap": _num(totals["clawback_shortfall"]),
            "carry_net_of_clawback": _num(totals["carry_net"]),
            "carry_share_of_lp_class_profit_pct": _pct(totals["carry_pct_of_lp_class_profit"]),
            "first_carry_date": _date(totals["first_carry_date"]),
            "first_carry_year_of_fund_life_years": _num(bundle["carry_profile"]["first_carry_year"]),
            "peak_escrow": _num(totals["peak_escrow"]),
            "distribution_events": _distribution_events(result),
        },
        "costs": {
            "management_fees": _num(totals["total_management_fees"]),
            "management_fees_pct_of_commitments": _pct(totals["total_management_fees"] / fund.committed_capital),
            "fee_offsets": _num(totals["total_fee_offsets"]),
            "partnership_expenses": _num(totals["total_expenses"]),
            "subscription_line_interest": _num(totals["total_line_interest"]),
        },
        "gross_to_net_bridge": [
            {
                "step": row["step"],
                "irr_after_pct": _pct(row["irr_after"]),
                "irr_impact_bps": _num(row["irr_impact_bps"], 0),
                "multiple_after_x": _num(row["multiple_after"], 2),
            }
            for _, row in bundle["bridge"].iterrows()
        ],  # fmt: skip
        "split_of_gross_profit": [
            {
                "component": row["component"],
                "recipient": row["recipient"],
                "amount": _num(row["amount"]),
                "share_pct": _pct(row["share_of_gross_profit"]),
            }
            for _, row in bundle["value_split"].iterrows()
        ],  # fmt: skip
        "gp_economics": {
            "management_fees": _num(totals["total_management_fees"]),
            "carry_net_of_clawback": _num(totals["carry_net"]),
            "gp_commitment_profit": _num(totals["gp_commit_profit"]),
            "total": _num(totals["gp_total_economics"]),
            "share_of_gross_profit_pct": _pct(totals["gp_share_of_gross_profit"]),
            "lp_share_of_gross_profit_pct": _pct(totals["lp_share_of_gross_profit"]),
        },
    }

    deals = result.deals.sort_values("gain", ascending=False)
    pack["deals"] = [
        {
            "name": row["deal"], "sector": row["sector"], "strategy": row["strategy"],
            "invested": _num(row["invested"]), "proceeds": _num(row["proceeds"]), "gain": _num(row["gain"]),
            "gross_moic_x": _num(row["gross_moic"], 2), "gross_irr_pct": _pct(row["gross_irr"]),
            "hold_years": _num(row["hold_years"]), "share_of_total_gain_pct": _pct(row["share_of_gain"]),
            "exit_date": _date(row["exit_date"]), "status_at_valuation_date": row["status_at_valuation"],
        }
        for _, row in deals.iterrows()
    ]  # fmt: skip
    concentration = bundle["concentration"]
    pack["portfolio"] = {
        "top_deal": concentration["top_deal"],
        "top_deal_share_of_gain_pct": _pct(concentration["top_deal_share_of_gain"]),
        "top3_share_of_gain_pct": _pct(concentration["top3_share_of_gain"]),
        "deals_below_cost": concentration["n_losers"],
        "write_offs": concentration["n_write_offs"],
        "deals_at_2x_or_better": concentration["n_above_2x"],
        "loss_ratio_pct_of_invested": _pct(concentration["loss_ratio"]),
        "capital_lost_pct_of_invested": _pct(concentration["capital_loss_ratio"]),
        "average_hold_years": _num(concentration["average_hold_years"]),
        "value_creation": [
            {"driver": row["driver"], "amount": _num(row["amount"]), "share_of_gain_pct": _pct(row["share_of_gain"])}
            for _, row in bundle["value_creation"].iterrows()
            if abs(row["amount"]) > 1e-9
        ],
    }
    pack["public_market_equivalent"] = {
        "ks_pme_x": _num(bundle["pme"], 2),
        "benchmark_return_pct": _pct(bundle["benchmark_return"]),
    }

    comparison = bundle["comparison"]["table"]
    european, american = comparison.iloc[0], comparison.iloc[1]
    pack["waterfall_comparison"] = {
        "whole_of_fund": {
            "net_irr_pct": _pct(european["net_irr"]), "net_tvpi_x": _num(european["tvpi"], 2),
            "carry_distributed": _num(european["carry_gross"]), "clawback_paid": _num(european["clawback_paid"]),
            "carry_net_of_clawback": _num(european["carry_net"]), "first_carry_date": _date(european["first_carry_date"]),
            "average_carry_timing_years": _num(european["average_carry_timing_years"]),
            "carry_present_value": _num(european["carry_present_value"]),
        },
        "deal_by_deal": {
            "net_irr_pct": _pct(american["net_irr"]), "net_tvpi_x": _num(american["tvpi"], 2),
            "carry_distributed": _num(american["carry_gross"]), "clawback_paid": _num(american["clawback_paid"]),
            "carry_net_of_clawback": _num(american["carry_net"]), "first_carry_date": _date(american["first_carry_date"]),
            "average_carry_timing_years": _num(american["average_carry_timing_years"]),
            "carry_present_value": _num(american["carry_present_value"]),
            "peak_clawback_exposure": _num(american["peak_clawback_exposure"]),
        },
        "lp_irr_cost_of_deal_by_deal_bps": _num((european["net_irr"] - american["net_irr"]) * 1e4, 0),
        "carry_arrives_earlier_by_years": _num(
            european["average_carry_timing_years"] - american["average_carry_timing_years"]
        ),
        "carry_present_value_uplift_for_gp": _num(american["carry_present_value"] - european["carry_present_value"]),
        "present_value_discount_rate_pct": _pct(bundle["carry_profile"]["discount_rate"]),
    }  # fmt: skip

    table = bundle.get("scenario_table")
    if table is not None:
        pack["scenarios"] = [
            {
                "name": row["label"], "what_changes": row["description"], "waterfall": row["waterfall"],
                "gross_moic_x": _num(row["gross_moic"], 2), "net_irr_pct": _pct(row["net_irr"]),
                "net_tvpi_x": _num(row["tvpi"], 2), "carry_net_of_clawback": _num(row["carry_net"]),
                "clawback_paid": _num(row["clawback_paid"]),
                "lp_shortfall_from_after_tax_cap": _num(row["clawback_shortfall"]),
                "management_fees": _num(row["total_management_fees"]),
                "net_irr_change_vs_base_bps": _num(row["net_irr_vs_base"] * 1e4, 0),
                "fund_life_years": _num(row["fund_life_years"]),
            }
            for _, row in table.iterrows()
        ]  # fmt: skip

    tornado = bundle.get("tornado")
    if tornado is not None:
        pack["sensitivity"] = {
            "metric": tornado.attrs["metric_label"],
            "one_at_a_time": [
                {
                    "assumption": row["label"],
                    "unfavourable_case_net_irr_pct": _pct(min(row["low_result"], row["high_result"])),
                    "favourable_case_net_irr_pct": _pct(max(row["low_result"], row["high_result"])),
                    "swing_bps": _num(row["swing"] * 1e4, 0),
                }
                for _, row in tornado.iterrows()
            ],
        }
    breakevens = bundle.get("breakevens")
    if breakevens is not None:
        pack["breakevens"] = [
            {
                "threshold": row["threshold"],
                "exit_multiple_change_pct": _pct(row["exit_multiple_change"]),
                "gross_moic_x": _num(row.get("gross_moic"), 2),
            }
            for _, row in breakevens.iterrows()
            if np.isfinite(row["exit_multiple_change"])
        ]
    grids = bundle.get("grids") or {}
    grid = grids.get("exit_multiple_x_holding_period__net_irr")
    if grid is not None:
        values = grid.to_numpy(dtype=float)
        pack.setdefault("sensitivity", {})["exit_multiple_and_holding_period_grid"] = {
            "lowest_net_irr_pct": _pct(np.nanmin(values)),
            "highest_net_irr_pct": _pct(np.nanmax(values)),
            "exit_multiple_changes_tested_pct": [_pct(v, 0) for v in grid.columns],
            "holding_period_changes_tested_quarters": [int(v) for v in grid.index],
        }

    stats = bundle.get("monte_carlo_stats")
    if stats is not None:
        pack["monte_carlo"] = {
            "trials": stats["n_sims"],
            "net_irr_median_pct": _pct(stats["net_irr_median"]),
            "net_irr_mean_pct": _pct(stats["net_irr_mean"]),
            "net_irr_5th_percentile_pct": _pct(stats["net_irr_p5"]),
            "net_irr_95th_percentile_pct": _pct(stats["net_irr_p95"]),
            "net_tvpi_5th_percentile_x": _num(stats["tvpi_p5"], 2),
            "net_tvpi_95th_percentile_x": _num(stats["tvpi_p95"], 2),
            "probability_net_irr_below_hurdle_pct": _pct(stats["prob_net_irr_below_pref"]),
            "probability_of_capital_loss_pct": _pct(stats["prob_capital_loss"]),
            "probability_of_no_carry_pct": _pct(stats["prob_no_carry"]),
            "carry_mean": _num(stats["carry_mean"]),
            "carry_5th_percentile": _num(stats["carry_p5"]),
            "carry_95th_percentile": _num(stats["carry_p95"]),
            "probability_of_material_clawback_deal_by_deal_pct": _pct(stats["prob_clawback_american"]),
            "average_clawback_when_triggered_deal_by_deal": _num(stats["clawback_mean_when_triggered"]),
            "largest_clawback_deal_by_deal": _num(stats["clawback_max"]),
            "probability_of_material_clawback_whole_of_fund_pct": _pct(stats["prob_clawback_european"]),
            "average_lp_irr_cost_of_deal_by_deal_bps": _num(stats["american_irr_drag_mean"] * 1e4, 0),
        }
    pack["model_flags"] = list(result.flags)
    return pack


def flatten_numbers(pack: Any, prefix: str = "") -> dict[str, float]:
    """Every numeric leaf of the pack, keyed by its dotted path."""
    out: dict[str, float] = {}
    if isinstance(pack, dict):
        for key, value in pack.items():
            out.update(flatten_numbers(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(pack, (list, tuple)):
        for index, value in enumerate(pack):
            out.update(flatten_numbers(value, f"{prefix}[{index}]"))
    elif isinstance(pack, bool):
        return out
    elif isinstance(pack, (int, float)) and np.isfinite(float(pack)):
        out[prefix] = float(pack)
    return out
