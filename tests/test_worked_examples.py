"""The hand-worked examples in docs/worked_example.md, checked against the engine.

Each expected figure below is derived by hand in the document; these tests keep
the document and the code in step.
"""

from pathlib import Path

import pytest

from pefund import load_config, run_fund

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "examples"


def distribution_quarter(result):
    """The first quarter with cash for the LP class."""
    return next(q for q in range(result.n_quarters) if result.arrays["lp_distributable"][q] > 0.0)


def tiers(result, q):
    a = result.arrays
    names = ("tier1_return_of_capital", "tier2_preferred_return", "tier3_catch_up_gp", "tier4_split_lp",
             "tier4_split_gp", "lp_distributions", "gp_carry")  # fmt: skip
    return {name: float(a[name][q]) for name in names}


def test_example_1_one_deal_runs_through_all_four_tiers():
    result = run_fund(load_config(EXAMPLES / "single_deal_four_tiers.yaml"))
    q = distribution_quarter(result)
    years = 1461 / 365  # 31 Mar 2022 to 31 Mar 2026 includes one leap day
    assert result.schedule.t[q] == pytest.approx(years)

    pref = 100.0 * (1.08**years - 1.0)
    catch_up = 0.20 * pref / (1.0 - 0.20)
    residual = 200.0 - 100.0 - pref - catch_up
    got = tiers(result, q)
    assert got["tier1_return_of_capital"] == pytest.approx(100.0)
    assert got["tier2_preferred_return"] == pytest.approx(pref) == pytest.approx(36.0776, abs=5e-5)
    assert got["tier3_catch_up_gp"] == pytest.approx(catch_up) == pytest.approx(9.0194, abs=5e-5)
    assert got["tier4_split_lp"] == pytest.approx(0.80 * residual) == pytest.approx(43.9224, abs=5e-5)
    assert got["tier4_split_gp"] == pytest.approx(0.20 * residual) == pytest.approx(10.9806, abs=5e-5)
    # With a full catch-up the GP ends with exactly 20% of the 100 of profit.
    assert got["gp_carry"] == pytest.approx(20.0)
    assert got["lp_distributions"] == pytest.approx(180.0)
    assert result.summary["tvpi"] == pytest.approx(1.80)
    assert result.summary["net_irr"] == pytest.approx(1.8 ** (1 / years) - 1.0, abs=1e-6)


def test_example_2_deal_by_deal_pays_early_and_claws_back():
    result = run_fund(load_config(EXAMPLES / "two_deal_clawback.yaml"))
    q = distribution_quarter(result)
    years = 731 / 365
    pref = 50.0 * (1.08**years - 1.0)
    got = tiers(result, q)
    assert got["tier1_return_of_capital"] == pytest.approx(50.0)  # only the winner's capital is eligible
    assert got["tier2_preferred_return"] == pytest.approx(pref) == pytest.approx(8.3323, abs=5e-5)
    assert got["tier3_catch_up_gp"] == pytest.approx(0.25 * pref) == pytest.approx(2.0831, abs=5e-5)
    assert got["gp_carry"] == pytest.approx(20.0)  # 20% of the winner's 100 gain
    assert got["lp_distributions"] == pytest.approx(130.0)

    totals = result.totals
    assert totals["carry_gross"] == pytest.approx(20.0)
    assert totals["clawback_uncapped"] == pytest.approx(10.0)  # carry 20 less 20% of fund profit 50
    assert totals["clawback_paid"] == pytest.approx(10.0)  # below the after-tax cap of 20 x (1 - 40%) = 12
    assert totals["carry_net"] == pytest.approx(10.0)
    assert totals["total_lp_distributions"] == pytest.approx(140.0)
    assert result.arrays["clawback_paid"][result.final_quarter] == pytest.approx(10.0)


def test_example_2_whole_of_fund_never_overpays():
    cfg = load_config(EXAMPLES / "two_deal_clawback.yaml").with_values({"waterfall.type": "european"})
    result = run_fund(cfg)
    q = distribution_quarter(result)
    pref = 100.0 * (1.08 ** (731 / 365) - 1.0)
    got = tiers(result, q)
    assert got["tier1_return_of_capital"] == pytest.approx(100.0)  # all capital, the loser's included
    assert got["tier2_preferred_return"] == pytest.approx(pref) == pytest.approx(16.6646, abs=5e-5)
    assert got["gp_carry"] == pytest.approx(10.0)
    assert result.totals["clawback_paid"] == 0.0
    assert result.totals["total_lp_distributions"] == pytest.approx(140.0)


def test_example_2_same_money_different_timing():
    """Both waterfalls end with the same split; deal-by-deal returns 10 to LPs three years later."""
    base = load_config(EXAMPLES / "two_deal_clawback.yaml")
    american = run_fund(base)
    european = run_fund(base.with_values({"waterfall.type": "european"}))
    assert american.totals["carry_net"] == pytest.approx(european.totals["carry_net"])
    assert american.summary["tvpi"] == pytest.approx(european.summary["tvpi"]) == pytest.approx(1.40)
    assert american.summary["net_irr"] == pytest.approx(0.1672, abs=5e-5)
    assert european.summary["net_irr"] == pytest.approx(0.1829, abs=5e-5)


def test_example_2_write_down_protection_reduces_the_overpayment():
    cfg = load_config(EXAMPLES / "two_deal_clawback.yaml").with_values({"waterfall.writedowns_as_realized": True})
    result = run_fund(cfg)
    # By March 2024 the loser is marked at 37.5 against a cost of 50; the 12.5 write-down
    # joins the capital that must be returned before carry.
    q = distribution_quarter(result)
    assert result.arrays["tier1_return_of_capital"][q] == pytest.approx(62.5)
    assert result.totals["carry_gross"] == pytest.approx(17.5)
    assert result.totals["clawback_paid"] == pytest.approx(7.5)
    assert result.totals["carry_net"] == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("overrides", "carry"),
    [
        ({"waterfall.catch_up_rate": 0.5}, 20.0),  # a partial catch-up still completes: 24.05 needed, 63.92 available
        ({"waterfall.catch_up_rate": 0.0}, 0.20 * (100.0 - 100.0 * (1.08 ** (1461 / 365) - 1.0))),  # hard hurdle
        ({"waterfall.pref_rate": 0.0}, 20.0),  # straight 80 / 20
    ],
)
def test_example_1_under_other_terms(overrides, carry):
    cfg = load_config(EXAMPLES / "single_deal_four_tiers.yaml").with_values(overrides)
    assert run_fund(cfg).totals["carry_gross"] == pytest.approx(carry)
