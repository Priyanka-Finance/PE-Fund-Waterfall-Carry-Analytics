"""Scenario, sensitivity, breakeven and Monte Carlo behaviour."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pefund import ConfigError, run_fund
from pefund import scenarios as sc
from pefund.montecarlo import MonteCarloSettings, run_monte_carlo, summarize_monte_carlo

SCENARIO_FILE = Path(__file__).resolve().parents[1] / "configs" / "scenarios.yaml"


@pytest.fixture(scope="module")
def scenario_table(base_config):
    return sc.run_scenarios(base_config, sc.load_scenarios(SCENARIO_FILE), interim=False)[0].set_index("scenario")


def test_base_scenario_reproduces_the_base_run(base_config, scenario_table):
    base = run_fund(base_config).summary
    assert scenario_table.loc["base", "net_irr"] == pytest.approx(base["net_irr"])
    assert scenario_table.loc["base", "net_irr_vs_base"] == 0.0


def test_scenarios_rank_in_the_expected_order(scenario_table):
    irr = scenario_table["net_irr"]
    assert irr["upside"] > irr["base"] > irr["downside"] > irr["severe_downside"]
    assert scenario_table.loc["severe_downside", "carry_net"] == pytest.approx(0.0)


def test_delayed_exits_grow_the_multiple_but_cut_the_irr(scenario_table):
    base, delayed = scenario_table.loc["base"], scenario_table.loc["delayed_exits"]
    assert delayed["gross_moic"] > base["gross_moic"]  # EBITDA keeps compounding
    assert delayed["net_irr"] < base["net_irr"]  # but the money arrives later
    assert delayed["total_management_fees"] > base["total_management_fees"]  # and fees run for longer


def test_late_losses_trigger_a_clawback_only_under_deal_by_deal(scenario_table):
    american = scenario_table.loc["late_losses_deal_by_deal"]
    european = scenario_table.loc["late_losses_whole_of_fund"]
    assert american["gross_moic"] == pytest.approx(european["gross_moic"])  # identical deal outcomes
    assert american["carry_gross"] > 0.0 and american["clawback_paid"] > 0.0
    assert american["clawback_shortfall"] > 0.0  # the after-tax cap leaves LPs short
    assert european["carry_gross"] == pytest.approx(0.0)
    assert european["clawback_paid"] == pytest.approx(0.0)
    assert european["tvpi"] > american["tvpi"]


def test_terms_scenarios_move_value_between_lp_and_gp(scenario_table):
    base = scenario_table.loc["base"]
    assert scenario_table.loc["lp_friendly_terms", "net_irr"] > base["net_irr"]
    assert scenario_table.loc["gp_friendly_terms", "net_irr"] < base["net_irr"]
    assert scenario_table.loc["gp_friendly_terms", "gp_total_economics"] > base["gp_total_economics"]
    # Terms do not change what the portfolio earns.
    assert scenario_table.loc["lp_friendly_terms", "gross_irr"] == pytest.approx(base["gross_irr"])


def test_scenario_file_rejects_unknown_keys(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("scenarios:\n  x:\n    override: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        sc.load_scenarios(bad)


def test_grid_is_monotonic_in_exit_multiple_and_centred_on_base(base_config):
    grid = sc.sensitivity_grid(
        base_config, ("exit_multiple", [-0.2, 0.0, 0.2]), ("hold_shift", [0, 4]), metric="net_irr"
    )
    assert grid.shape == (2, 3)
    assert (np.diff(grid.to_numpy(), axis=1) > 0).all()  # higher exit multiples, higher IRR
    assert grid.loc[0, 0.0] == pytest.approx(run_fund(base_config).summary["net_irr"])
    assert grid.attrs["x_label"] == "Exit multiple change"


def test_full_catch_up_makes_total_carry_independent_of_the_hurdle(base_config):
    """Above the catch-up zone the pref changes when carry is paid, not how much."""
    table = sc.one_way(base_config, "pref_rate", [0.0, 0.06, 0.08, 0.10], metrics=["carry_net", "first_carry_year"])
    assert np.ptp(table["carry_net"]) == pytest.approx(0.0, abs=1e-6)
    assert table["first_carry_year"].is_monotonic_increasing
    hard = sc.one_way(base_config.with_values({"waterfall.catch_up_rate": 0.0}), "pref_rate", [0.0, 0.08])
    assert hard["carry_net"].iloc[1] < hard["carry_net"].iloc[0]  # without a catch-up the hurdle does cost the GP


def test_fee_parameter_preserves_the_step_down(base_config):
    overrides = sc.overrides_for(base_config, "fee_rate", 0.015)
    assert overrides["management_fee.rate_investment_period"] == pytest.approx(0.015)
    assert overrides["management_fee.rate_post_investment_period"] == pytest.approx(0.0125)


def test_tornado_is_sorted_by_swing_and_brackets_the_base(base_config):
    table = sc.tornado(base_config)
    assert table["swing"].is_monotonic_decreasing
    assert table.iloc[0]["parameter"] == "exit_multiple"
    exit_row = table.set_index("parameter").loc["exit_multiple"]
    assert exit_row["low_delta"] < 0 < exit_row["high_delta"]


def test_breakevens_hit_their_targets(base_config):
    table = sc.breakevens(base_config).set_index("threshold")
    capital_back = table.filter(like="capital back", axis=0).iloc[0]
    assert capital_back["tvpi"] == pytest.approx(1.0, abs=1e-3)
    hurdle = table.filter(like="preferred return", axis=0).iloc[0]
    assert hurdle["net_irr"] == pytest.approx(base_config.waterfall.pref_rate, abs=1e-4)
    assert hurdle["carry_net"] < 0.5  # carry is only just starting
    assert capital_back["exit_multiple_change"] < hurdle["exit_multiple_change"] < 0.0


def test_solver_returns_nan_when_the_target_is_out_of_range(base_config):
    assert np.isnan(sc.solve_for(base_config, "exit_multiple", "tvpi", 50.0, -0.5, 0.5))


def test_unknown_parameter_is_rejected(base_config):
    with pytest.raises(ConfigError, match="Unknown parameter"):
        sc.run_with(base_config, {"not_a_parameter": 1.0})


# --------------------------------------------------------------------------- #
# Monte Carlo
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def simulation(base_config):
    return run_monte_carlo(base_config, MonteCarloSettings(n_sims=150, seed=7))


def test_monte_carlo_is_reproducible(base_config, simulation):
    again = run_monte_carlo(base_config, MonteCarloSettings(n_sims=150, seed=7))
    np.testing.assert_allclose(simulation["net_irr"], again["net_irr"])
    different = run_monte_carlo(base_config, MonteCarloSettings(n_sims=150, seed=8))
    assert not np.allclose(simulation["net_irr"], different["net_irr"])


def test_monte_carlo_with_no_randomness_reproduces_the_base_case(base_config):
    settings = MonteCarloSettings(n_sims=3, market_vol=0.0, idiosyncratic_vol=0.0, write_off_probability=0.0,
                                  hold_shift_per_market_sigma=0.0, hold_shift_noise_quarters=0)  # fmt: skip
    sims = run_monte_carlo(base_config, settings)
    base = run_fund(base_config).summary
    np.testing.assert_allclose(sims["net_irr"], base["net_irr"])
    np.testing.assert_allclose(sims["carry_net"], base["carry_net"])


def test_monte_carlo_summary_is_coherent(simulation):
    stats = summarize_monte_carlo(simulation)
    assert stats["n_sims"] == 150
    assert stats["net_irr_p5"] <= stats["net_irr_median"] <= stats["net_irr_p95"]
    assert 0.0 <= stats["prob_net_irr_below_pref"] <= 1.0
    # Whole-of-fund: no carry exactly when LPs miss the hurdle.
    assert stats["prob_no_carry"] == pytest.approx(stats["prob_net_irr_below_pref"], abs=0.02)
    # Deal-by-deal is where material clawbacks come from.
    assert stats["prob_clawback_american"] >= stats["prob_clawback_european"]
    assert stats["american_irr_drag_mean"] > 0.0


def test_deal_by_deal_never_pays_lps_more_carry_than_was_distributed(simulation):
    assert (simulation["american_clawback_paid"] <= simulation["american_carry_gross"] + 1e-9).all()
    assert (simulation["american_carry_net"] >= -1e-9).all()
