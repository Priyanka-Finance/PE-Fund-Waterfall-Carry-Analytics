"""Analytics on top of a fund run: bridges, splits, comparisons and capital accounts."""

from __future__ import annotations

import numpy as np
import pytest

from pefund import analytics, run_fund


@pytest.fixture(scope="module")
def result(base_config):
    return run_fund(base_config)


def test_bridge_starts_at_gross_and_ends_at_net(result):
    bridge = analytics.gross_to_net_bridge(result).set_index("step")
    summary = result.summary
    assert bridge.loc["Gross return (deal level)", "irr_after"] == pytest.approx(summary["gross_irr"])
    assert bridge.loc["Gross return (deal level)", "multiple_after"] == pytest.approx(summary["gross_moic"])
    assert bridge.loc["Net return to LPs", "irr_after"] == pytest.approx(summary["net_irr"])
    assert bridge.loc["Net return to LPs", "multiple_after"] == pytest.approx(summary["tvpi"])
    # The individual steps add up to the total gross-to-net gap.
    steps = bridge.iloc[1:-1]["irr_impact"].sum()
    assert steps == pytest.approx(bridge.loc["Net return to LPs", "irr_impact"])
    assert (bridge.loc[["Management fees", "Partnership expenses", "Carried interest"], "irr_impact"] < 0).all()
    assert bridge.loc["Financing and timing", "irr_impact"] == pytest.approx(0.0, abs=1e-12)  # no subscription line


def test_bridge_isolates_the_subscription_line_effect(base_config):
    lined = run_fund(base_config.with_values({"subscription_line.enabled": True}))
    bridge = analytics.gross_to_net_bridge(lined).set_index("step")
    assert bridge.loc["Financing and timing", "irr_impact"] > 0.005  # deferring calls flatters IRR
    assert bridge.loc["Financing and timing", "multiple_impact"] == pytest.approx(0.0, abs=1e-9)  # but not the multiple
    assert bridge.loc["Net return to LPs", "irr_after"] == pytest.approx(lined.summary["net_irr"])


def test_bridge_works_at_an_interim_date(result, base_config):
    bridge = analytics.gross_to_net_bridge(result, base_config.fund.valuation_date).set_index("step")
    interim = result.metrics(base_config.fund.valuation_date)
    assert bridge.loc["Net return to LPs", "irr_after"] == pytest.approx(interim["net_irr"])
    assert bridge.loc["Net return to LPs", "multiple_after"] == pytest.approx(interim["tvpi"])


def test_value_split_accounts_for_all_gross_profit(result):
    split = analytics.value_split(result)
    assert split["amount"].sum() == pytest.approx(split.attrs["gross_profit"])
    assert split["share_of_gross_profit"].sum() == pytest.approx(1.0)
    by_name = split.set_index("component")["amount"]
    assert by_name["Carried interest"] == pytest.approx(result.totals["carry_net"])
    assert by_name["Management fees"] == pytest.approx(result.totals["total_management_fees"])


def test_lp_gp_economics_table(result):
    table = analytics.lp_gp_economics(result).set_index("party")
    assert table.loc["Limited partners", "irr"] == pytest.approx(result.summary["net_irr"])
    combined = table.loc["GP and manager combined", "profit"]
    parts = table.loc[["GP commitment (as investor)", "GP carried interest (net of clawback)",
                       "Manager - management fees (net of offsets)"], "profit"].sum()  # fmt: skip
    assert combined == pytest.approx(parts)
    # The GP commitment pays no fee or carry, so it earns close to the gross multiple.
    assert table.loc["GP commitment (as investor)", "multiple"] > table.loc["Limited partners", "multiple"]


def test_value_creation_reconciles_to_total_gain(result):
    drivers = analytics.value_creation(result)
    assert drivers.attrs["unexplained"] == pytest.approx(0.0, abs=1e-9)
    assert drivers["amount"].sum() == pytest.approx(result.deals["gain"].sum())


def test_concentration_and_sector_summary(result):
    stats = analytics.concentration(result)
    assert stats["n_deals"] == 12 and stats["n_write_offs"] == 1 and stats["n_losers"] == 2
    assert 0 < stats["top_deal_share_of_gain"] < stats["top3_share_of_gain"] < 1
    sectors = analytics.sector_summary(result)
    assert sectors["invested"].sum() == pytest.approx(result.totals["total_invested"])
    assert sectors["gain"].is_monotonic_decreasing


def test_annual_summary_adds_up_to_whole_life_totals(result):
    annual = analytics.annual_summary(result)
    assert annual["lp_contributions"].sum() == pytest.approx(result.totals["total_lp_contributions"])
    assert annual["gp_carry"].sum() == pytest.approx(result.totals["carry_gross"])
    assert annual["tvpi"].iloc[-1] == pytest.approx(result.summary["tvpi"])


def test_investor_accounts_sum_to_the_lp_class(result, base_config):
    accounts = analytics.investor_allocation(result, base_config.fund.valuation_date)
    interim = result.metrics(base_config.fund.valuation_date)
    assert len(accounts) == len(base_config.investors)
    assert accounts["share_of_lp_class"].sum() == pytest.approx(1.0)
    for column, key in (("paid_in", "paid_in"), ("distributions", "distributed"), ("nav", "nav")):
        assert accounts[column].sum() == pytest.approx(interim[key])
    assert np.ptp(accounts["tvpi"]) == pytest.approx(0.0)  # identical terms, identical multiples


def test_waterfall_comparison_table_and_timeline(base_config):
    comparison = analytics.compare_waterfalls(base_config)
    table = comparison["table"].set_index("waterfall")
    european, american = table.loc["Whole-of-fund (European)"], table.loc["Deal-by-deal (American)"]
    assert american["first_carry_date"] < european["first_carry_date"]
    assert american["average_carry_timing_years"] < european["average_carry_timing_years"]
    assert american["carry_present_value"] > european["carry_present_value"]  # same carry, earlier
    timeline = comparison["timeline"]
    assert (timeline["american_cumulative_carry"] >= timeline["european_cumulative_carry"] - 1e-9).all()
    assert timeline["european_cumulative_carry"].iloc[-1] == pytest.approx(european["carry_net"])


def test_interim_metrics_respect_identities(result):
    for q in (6, 14, 22, 30):
        m = result.metrics(q)
        assert m["tvpi"] == pytest.approx(m["dpi"] + m["rvpi"])
        assert m["nav"] <= result.config.fund.lp_pct * m["portfolio_fv"] + 1e-9  # never above gross fair value
        assert m["unfunded_commitment"] == pytest.approx(result.config.fund.lp_commitment - m["paid_in"])
    with pytest.raises(ValueError, match="interim=True"):
        run_fund(result.config, interim=False).metrics(10)


def test_public_market_equivalent_moves_with_the_benchmark(result):
    low = analytics.public_market_equivalent(result, benchmark_return=0.04)
    high = analytics.public_market_equivalent(result, benchmark_return=0.16)
    assert low > 1.0 > high  # net IRR of about 15% sits between the two benchmarks
