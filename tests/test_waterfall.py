"""Waterfall mechanics checked against closed-form, hand-computable answers."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from conftest import make_config, one_deal

from pefund import run_fund
from pefund.fund import clawback_due, split_tiers

# 2020-12-31 -> 2025-12-31 spans one leap day.
YEARS = (dt.date(2025, 12, 31) - dt.date(2020, 12, 31)).days / 365.0


def tiers(result) -> dict[str, float]:
    return {k: result.totals[k] for k in (
        "tier1_return_of_capital", "tier2_preferred_return", "tier3_catch_up", "tier3_catch_up_gp",
        "tier4_split_lp", "tier4_split_gp", "carry_gross", "carry_net", "total_lp_distributions",
    )}  # fmt: skip


# --------------------------------------------------------------------------- #
# One contribution, one distribution: every tier has a closed form
# --------------------------------------------------------------------------- #
def test_single_deal_full_waterfall_matches_closed_form():
    """100 in, 200 out after five years, 8% pref, 100% catch-up, 20% carry."""
    result = run_fund(make_config([one_deal(100.0, 2.0)]))
    pref = 100.0 * (1.08**YEARS - 1.0)
    catch_up = pref * 0.20 / 0.80  # GP reaches 20% of (pref + catch-up)
    residual = 200.0 - 100.0 - pref - catch_up
    got = tiers(result)
    assert got["tier1_return_of_capital"] == pytest.approx(100.0)
    assert got["tier2_preferred_return"] == pytest.approx(pref)
    assert got["tier3_catch_up"] == pytest.approx(catch_up)
    assert got["tier4_split_gp"] == pytest.approx(0.20 * residual)
    assert got["tier4_split_lp"] == pytest.approx(0.80 * residual)
    # With a full catch-up the GP ends on exactly 20% of total profit.
    assert got["carry_gross"] == pytest.approx(0.20 * 100.0)
    assert got["total_lp_distributions"] == pytest.approx(180.0)


def test_profit_below_hurdle_pays_no_carry():
    result = run_fund(make_config([one_deal(100.0, 1.2)]))
    got = tiers(result)
    assert got["carry_gross"] == pytest.approx(0.0)
    assert got["tier2_preferred_return"] == pytest.approx(20.0)  # all profit, still short of the hurdle
    assert got["total_lp_distributions"] == pytest.approx(120.0)
    assert result.arrays["lp_pref_outstanding"][-1] == pytest.approx(100.0 * (1.08**YEARS - 1.0) - 20.0)


def test_distribution_inside_the_catch_up_zone_goes_entirely_to_gp():
    pref = 100.0 * (1.08**YEARS - 1.0)
    full_catch_up = pref * 0.25
    proceeds = 100.0 + pref + 0.5 * full_catch_up  # stops half-way through the catch-up
    result = run_fund(make_config([one_deal(100.0, proceeds / 100.0)]))
    got = tiers(result)
    assert got["tier3_catch_up"] == pytest.approx(0.5 * full_catch_up)
    assert got["carry_gross"] == pytest.approx(0.5 * full_catch_up)
    assert got["tier4_split_lp"] == pytest.approx(0.0, abs=1e-9)


def test_no_catch_up_is_a_hard_hurdle():
    result = run_fund(make_config([one_deal(100.0, 2.0)], waterfall={"catch_up_rate": 0.0}))
    pref = 100.0 * (1.08**YEARS - 1.0)
    got = tiers(result)
    assert got["tier3_catch_up"] == pytest.approx(0.0)
    assert got["carry_gross"] == pytest.approx(0.20 * (100.0 - pref))  # carry only on profit above the pref


def test_partial_catch_up_takes_longer_to_reach_the_carry_share():
    result = run_fund(make_config([one_deal(100.0, 2.0)], waterfall={"catch_up_rate": 0.5}))
    pref = 100.0 * (1.08**YEARS - 1.0)
    catch_up = 0.20 * pref / (0.5 - 0.20)  # solves 0.5x = 0.2 (pref + x)
    got = tiers(result)
    assert got["tier3_catch_up"] == pytest.approx(catch_up)
    assert got["tier3_catch_up_gp"] == pytest.approx(0.5 * catch_up)
    assert got["carry_gross"] == pytest.approx(20.0)  # still 20% of profit once fully caught up


@pytest.mark.parametrize(
    ("compounding", "expected_pref"),
    [
        ("annual", 100.0 * (1.08**YEARS - 1.0)),
        ("quarterly", 100.0 * (1.02 ** (4.0 * YEARS) - 1.0)),
        ("simple", 100.0 * 0.08 * YEARS),
    ],
)
def test_pref_compounding_conventions(compounding, expected_pref):
    result = run_fund(make_config([one_deal(100.0, 2.0)], waterfall={"pref_compounding": compounding}))
    assert result.totals["tier2_preferred_return"] == pytest.approx(expected_pref)


@pytest.mark.parametrize("carry_rate", [0.0, 0.10, 0.25, 0.30])
def test_gp_share_of_profit_equals_carry_rate_when_fully_caught_up(carry_rate):
    result = run_fund(make_config([one_deal(100.0, 3.0)], waterfall={"carry_rate": carry_rate}))
    assert result.totals["carry_gross"] == pytest.approx(carry_rate * 200.0)


def test_pref_stops_accruing_on_capital_once_it_is_returned():
    """Two distributions: after the first, pref compounds only on what is still owed."""
    deal = {
        "name": "Staged",
        "investments": [{"date": "2020-12-31", "amount": 100.0}],
        "exit_date": "2024-12-31",
        "gross_moic": 1.5,
        "proceeds_events": [{"date": "2022-12-31", "kind": "partial_exit", "share": 0.4, "stake_sold": 0.4}],
    }
    result = run_fund(make_config([deal]))
    y1 = (dt.date(2022, 12, 31) - dt.date(2020, 12, 31)).days / 365.0
    y2 = (dt.date(2024, 12, 31) - dt.date(2022, 12, 31)).days / 365.0
    # Running ledger by hand.  60 arrives after two years and all of it repays capital,
    # leaving 40 of capital and two years of pref on the full 100 still owed.
    owed_after_first = 100.0 * 1.08**y1 - 60.0
    owed_at_exit = owed_after_first * 1.08**y2  # the 90 received at exit covers this in full
    pref = owed_at_exit - 40.0
    assert result.totals["tier1_return_of_capital"] == pytest.approx(100.0)
    assert result.totals["tier2_preferred_return"] == pytest.approx(pref)
    assert pref < 100.0 * (1.08 ** (y1 + y2) - 1.0)  # less than if nothing had been returned early
    assert result.totals["tier3_catch_up"] == pytest.approx(0.25 * pref)
    assert result.totals["carry_gross"] == pytest.approx(0.20 * 50.0)


# --------------------------------------------------------------------------- #
# Whole-of-fund versus deal-by-deal
# --------------------------------------------------------------------------- #
WINNER_THEN_FLAT = [
    {"name": "A", "investments": [{"date": "2020-12-31", "amount": 100.0}], "exit_date": "2023-12-31", "gross_moic": 2.0},
    {"name": "B", "investments": [{"date": "2020-12-31", "amount": 100.0}], "exit_date": "2026-12-31", "gross_moic": 1.0},
]
WINNER_THEN_WRITE_OFF = [
    WINNER_THEN_FLAT[0],
    {"name": "B", "investments": [{"date": "2020-12-31", "amount": 100.0}], "exit_date": "2026-12-31", "gross_moic": 0.0},
]
#: Marks stay at cost so that the write-down rule does not anticipate the loss.
NO_MARKDOWN = {"type": "american", "writedowns_as_realized": False}


def test_european_returns_all_capital_before_any_carry():
    result = run_fund(make_config(WINNER_THEN_FLAT))
    q_first = int(np.argmax(result.arrays["gross_proceeds"] > 0))
    assert result.arrays["gp_carry"][q_first] == pytest.approx(0.0)  # 200 received, 200 contributed
    assert result.totals["carry_gross"] == pytest.approx(20.0)  # 20% of the fund's 100 profit, paid at the end


def test_american_pays_carry_on_the_first_winner():
    result = run_fund(make_config(WINNER_THEN_FLAT, waterfall=NO_MARKDOWN))
    q_first = int(np.argmax(result.arrays["gross_proceeds"] > 0))
    # Deal A alone: 100 of profit, GP fully caught up, so 20 of carry straight away.
    assert result.arrays["gp_carry"][q_first] == pytest.approx(20.0)
    assert result.totals["carry_gross"] == pytest.approx(20.0)
    assert result.totals["clawback_paid"] == pytest.approx(0.0)


def test_both_waterfalls_pay_the_same_total_carry_on_a_profitable_fund():
    european = run_fund(make_config(WINNER_THEN_FLAT))
    american = run_fund(make_config(WINNER_THEN_FLAT, waterfall=NO_MARKDOWN))
    assert american.totals["carry_net"] == pytest.approx(european.totals["carry_net"])
    assert american.totals["total_lp_distributions"] == pytest.approx(european.totals["total_lp_distributions"])
    # Same money, later for the LPs: deal-by-deal lowers their IRR.
    assert american.summary["net_irr"] < european.summary["net_irr"]


def test_loss_carryforward_makes_later_winners_repay_earlier_losses():
    deals = [
        {"name": "Loser", "investments": [{"date": "2020-12-31", "amount": 100.0}], "exit_date": "2022-12-31", "gross_moic": 0.0},
        {"name": "Winner", "investments": [{"date": "2020-12-31", "amount": 100.0}], "exit_date": "2023-12-31", "gross_moic": 2.0},
    ]
    result = run_fund(make_config(deals, waterfall=NO_MARKDOWN))
    # The fund as a whole made nothing, so the winner's proceeds only return capital.
    assert result.totals["carry_gross"] == pytest.approx(0.0)
    assert result.totals["tier1_return_of_capital"] == pytest.approx(200.0)


# --------------------------------------------------------------------------- #
# Clawback and escrow
# --------------------------------------------------------------------------- #
def test_clawback_returns_carry_paid_before_a_later_loss():
    result = run_fund(make_config(WINNER_THEN_WRITE_OFF, waterfall=NO_MARKDOWN))
    assert result.totals["carry_gross"] == pytest.approx(20.0)  # paid on deal A
    assert result.totals["clawback_paid"] == pytest.approx(20.0)  # fund profit is nil, so all of it comes back
    assert result.totals["carry_net"] == pytest.approx(0.0)
    assert result.totals["total_lp_distributions"] == pytest.approx(200.0)  # LPs made whole


def test_after_tax_limit_caps_the_clawback():
    cfg = make_config(
        WINNER_THEN_WRITE_OFF, waterfall=NO_MARKDOWN, clawback={"after_tax_limit": True, "assumed_tax_rate": 0.40}
    )
    result = run_fund(cfg)
    assert result.totals["clawback_paid"] == pytest.approx(12.0)  # 60% of the 20 received
    assert result.totals["clawback_uncapped"] == pytest.approx(20.0)
    assert result.totals["clawback_shortfall"] == pytest.approx(8.0)  # what the cap costs the LPs


def test_escrow_holds_back_carry_and_funds_the_clawback_first():
    cfg = make_config(WINNER_THEN_WRITE_OFF, waterfall=NO_MARKDOWN, clawback={"escrow_pct": 0.5})
    result = run_fund(cfg)
    a = result.arrays
    q_first = int(np.argmax(a["gross_proceeds"] > 0))
    assert a["carry_escrowed"][q_first] == pytest.approx(10.0)
    assert a["gp_carry_cash"][q_first] == pytest.approx(10.0)
    assert result.totals["peak_escrow"] == pytest.approx(10.0)
    assert a["clawback_from_escrow"][-1] == pytest.approx(10.0)
    assert a["gp_carry_cash"][-1] == pytest.approx(-10.0)  # the rest is paid back by the GP directly
    assert a["gp_carry_cash"].sum() == pytest.approx(0.0)


def test_escrow_is_released_when_no_clawback_is_due():
    cfg = make_config(WINNER_THEN_FLAT, waterfall=NO_MARKDOWN, clawback={"escrow_pct": 0.3})
    result = run_fund(cfg)
    a = result.arrays
    assert a["escrow_release"][-1] == pytest.approx(0.3 * 20.0)
    assert a["gp_carry_cash"].sum() == pytest.approx(20.0)
    assert a["escrow_balance"][-1] == pytest.approx(0.0)


def test_writedowns_treated_as_realised_delay_carry():
    """With the ILPA-style rule, a deal marked below cost reduces carry on earlier winners."""
    with_rule = run_fund(make_config(WINNER_THEN_WRITE_OFF, waterfall={"type": "american"}))
    without_rule = run_fund(make_config(WINNER_THEN_WRITE_OFF, waterfall=NO_MARKDOWN))
    assert with_rule.totals["carry_gross"] < without_rule.totals["carry_gross"]
    assert with_rule.totals["clawback_uncapped"] < without_rule.totals["clawback_uncapped"]


def test_disabled_clawback_reports_the_exposure_but_pays_nothing():
    cfg = make_config(WINNER_THEN_WRITE_OFF, waterfall=NO_MARKDOWN, clawback={"enabled": False})
    result = run_fund(cfg)
    assert result.totals["clawback_paid"] == pytest.approx(0.0)
    assert result.totals["clawback_uncapped"] == pytest.approx(20.0)
    assert result.totals["carry_net"] == pytest.approx(20.0)


# --------------------------------------------------------------------------- #
# Pure functions
# --------------------------------------------------------------------------- #
def test_split_tiers_orders_the_tiers_and_conserves_cash():
    out = split_tiers(500.0, 100.0, 40.0, 0.0, 0.0, 0.20, 1.0)
    roc, pref, cu_gp, cu_lp, sp_lp, sp_gp = out
    assert (roc, pref, cu_gp, cu_lp) == pytest.approx((100.0, 40.0, 10.0, 0.0))
    assert (sp_lp, sp_gp) == pytest.approx((280.0, 70.0))
    assert sum(out) == pytest.approx(500.0)


def test_split_tiers_reopens_the_catch_up_after_new_pref():
    # GP already holds 20% of 100 of profit; 50 of new pref is then paid, so the GP must catch up again.
    _, pref, cu_gp, _, _, _ = split_tiers(1000.0, 0.0, 50.0, 100.0, 20.0, 0.20, 1.0)
    assert pref == pytest.approx(50.0)
    assert cu_gp == pytest.approx(12.5)  # (20 + x) = 0.2 (150 + x)


def test_clawback_two_pronged_test():
    # (A) excess carry: 30 received, entitled to 20% of 100 profit.
    due, uncapped = clawback_due(200.0, 270.0, 30.0, 0.0, 0.20, True, False, 0.0)
    assert (due, uncapped) == pytest.approx((10.0, 10.0))
    # (B) LP shortfall on capital plus pref exceeds the excess-carry test.
    due, _ = clawback_due(200.0, 215.0, 5.0, 25.0, 0.20, True, False, 0.0)
    assert due == pytest.approx(5.0)  # limited to carry actually received
    # No carry received, nothing to claw back.
    assert clawback_due(200.0, 150.0, 0.0, 30.0, 0.20, True, False, 0.0) == (0.0, 0.0)
