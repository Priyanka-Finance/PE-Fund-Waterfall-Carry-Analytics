"""Engine-level checks: independent re-implementations, conservation of cash, fees and funding."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import make_config, one_deal
from pefund import ConfigError, run_fund

FEATURE_SETS = {
    "european": {},
    "american": {"waterfall.type": "american"},
    "american_all_to_date": {"waterfall.type": "american", "waterfall.expense_allocation": "all_to_date"},
    "subscription_line": {"subscription_line.enabled": True},
    "recycling": {"recycling.enabled": True},
    "escrow": {"waterfall.type": "american", "clawback.escrow_pct": 0.3},
    "simple_pref": {"waterfall.pref_compounding": "simple"},
    "quarterly_pref": {"waterfall.pref_compounding": "quarterly"},
    "no_catch_up": {"waterfall.catch_up_rate": 0.0},
    "partial_catch_up": {"waterfall.catch_up_rate": 0.8},
    "fee_on_committed_throughout": {"management_fee.basis_post_investment_period": "committed"},
    "fee_on_nav": {"management_fee.basis_post_investment_period": "nav"},
    "stress": {"shocks.exit_multiple_pct": -0.5, "shocks.hold_shift_quarters": 8},
    "boom": {"shocks.exit_multiple_pct": 0.5, "shocks.hold_shift_quarters": -4},
}


# --------------------------------------------------------------------------- #
# Independent re-implementations of the waterfall
# --------------------------------------------------------------------------- #
def running_ledger_european(contributions, distributable, years, rate=0.08, carry=0.20):
    """Textbook whole-of-fund waterfall kept as running balances, quarter by quarter."""
    capital = pref_owed = profit_paid = carry_paid = 0.0
    rows = []
    for q, (paid_in, cash) in enumerate(zip(contributions, distributable, strict=True)):
        if q > 0:
            pref_owed += (capital + pref_owed) * ((1.0 + rate) ** (years[q] - years[q - 1]) - 1.0)
        capital += paid_in
        roc = min(cash, capital)
        capital -= roc
        cash -= roc
        pref = min(cash, pref_owed)
        pref_owed -= pref
        cash -= pref
        needed = max((carry * (profit_paid + pref) - carry_paid) / (1.0 - carry), 0.0)
        catch_up = min(cash, needed)
        cash -= catch_up
        split_gp = carry * cash
        profit_paid += pref + catch_up + cash
        carry_paid += catch_up + split_gp
        rows.append((roc, pref, catch_up, cash - split_gp, split_gp))
    return np.array(rows)


def per_deal_ledger_american(cfg, rate=0.08, carry=0.20):
    """Deal-by-deal waterfall with one capital-and-pref ledger per deal.

    Valid for funds with no fees, expenses, partial exits or write-downs.  A
    deal's ledger joins the realised pool on exit, bringing its accrued pref.
    """
    from pefund.deals import build_deal_schedule

    s = build_deal_schedule(cfg)
    deal_capital = np.zeros(s.n_deals)
    deal_pref = np.zeros(s.n_deals)
    pool_capital = pool_pref = profit_paid = carry_paid = 0.0
    carry_by_quarter = np.zeros(s.n_quarters)
    lp_by_quarter = np.zeros(s.n_quarters)
    for q in range(s.n_quarters):
        if q > 0:
            factor = (1.0 + rate) ** (s.t[q] - s.t[q - 1]) - 1.0
            deal_pref += (deal_capital + deal_pref) * factor
            pool_pref += (pool_capital + pool_pref) * factor
        deal_capital += s.inv[:, q]
        for i in np.nonzero(s.exit_q == q)[0]:
            pool_capital += deal_capital[i]
            pool_pref += deal_pref[i]
            deal_capital[i] = deal_pref[i] = 0.0
        cash = s.proc[:, q].sum()
        roc = min(cash, pool_capital)
        pool_capital -= roc
        cash -= roc
        pref = min(cash, pool_pref)
        pool_pref -= pref
        cash -= pref
        needed = max((carry * (profit_paid + pref) - carry_paid) / (1.0 - carry), 0.0)
        catch_up = min(cash, needed)
        cash -= catch_up
        split_gp = carry * cash
        profit_paid += pref + catch_up + cash
        carry_paid += catch_up + split_gp
        carry_by_quarter[q] = catch_up + split_gp
        lp_by_quarter[q] = roc + pref + cash - split_gp
    return carry_by_quarter, lp_by_quarter


def test_european_engine_equals_a_textbook_running_ledger(base_config):
    result = run_fund(base_config)
    a = result.arrays
    expected = running_ledger_european(a["lp_contributions"], a["lp_distributable"], result.schedule.t)
    np.testing.assert_allclose(a["tier1_return_of_capital"], expected[:, 0], atol=1e-8)
    np.testing.assert_allclose(a["tier2_preferred_return"], expected[:, 1], atol=1e-8)
    np.testing.assert_allclose(a["tier3_catch_up_gp"], expected[:, 2], atol=1e-8)
    np.testing.assert_allclose(a["tier4_split_lp"], expected[:, 3], atol=1e-8)
    np.testing.assert_allclose(a["tier4_split_gp"], expected[:, 4], atol=1e-8)


@pytest.mark.parametrize("seed", range(8))
def test_american_engine_equals_per_deal_ledgers_on_random_portfolios(seed):
    rng = np.random.default_rng(seed)
    deals = []
    for i in range(int(rng.integers(3, 9))):
        entry_q = int(rng.integers(0, 16))
        exit_q = entry_q + int(rng.integers(4, 24))
        year, quarter = divmod(entry_q, 4)
        exit_year, exit_quarter = divmod(exit_q, 4)
        deals.append(
            {
                "name": f"Deal {i}",
                "investments": [
                    {"date": f"{2021 + year}-{3 * quarter + 1:02d}-15", "amount": float(rng.uniform(10, 60))}
                ],
                "exit_date": f"{2021 + exit_year}-{3 * exit_quarter + 1:02d}-15",
                "gross_moic": float(rng.choice([0.0, 0.5, 1.0, 1.5, 2.5, 4.0])),
            }
        )
    cfg = make_config(deals, waterfall={"type": "american", "writedowns_as_realized": False})
    result = run_fund(cfg)
    carry, lp = per_deal_ledger_american(cfg)
    np.testing.assert_allclose(result.arrays["gp_carry"], carry, atol=1e-8)
    np.testing.assert_allclose(result.arrays["lp_distributions"] - result.arrays["clawback_paid"], lp, atol=1e-8)


# --------------------------------------------------------------------------- #
# Conservation of cash under every feature combination
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(FEATURE_SETS))
def test_every_dollar_of_proceeds_is_accounted_for(base_config, name):
    result = run_fund(base_config.with_values(FEATURE_SETS[name]))
    a = result.arrays
    paid_out = (
        a["lp_distributions"].sum()
        - a["clawback_paid"].sum()  # clawback is GP money, not proceeds
        + a["gp_carry"].sum()
        + a["gp_commit_distributions"].sum()
        + a["netted_from_proceeds"].sum()
    )
    assert paid_out == pytest.approx(a["gross_proceeds"].sum(), abs=1e-6)
    # Tier amounts add up to what was distributable in every single quarter.
    tiers = sum(a[k] for k in (
        "tier1_return_of_capital", "tier2_preferred_return", "tier3_catch_up_gp",
        "tier3_catch_up_lp", "tier4_split_lp", "tier4_split_gp",
    ))  # fmt: skip
    np.testing.assert_allclose(tiers, a["lp_distributable"], atol=1e-8)
    # Carry cash to the GP, after escrow and clawback, equals carry net of clawback.
    assert a["gp_carry_cash"].sum() == pytest.approx(result.totals["carry_net"], abs=1e-8)
    assert a["escrow_balance"][-1] == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("name", sorted(FEATURE_SETS))
def test_gross_profit_is_fully_split_between_the_parties(base_config, name):
    from pefund.analytics import value_split

    result = run_fund(base_config.with_values(FEATURE_SETS[name]))
    for as_of in (None, 8, 18, 30):
        split = value_split(result, as_of)
        assert split.attrs["unexplained"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("name", sorted(FEATURE_SETS))
def test_balances_never_go_negative(base_config, name):
    result = run_fund(base_config.with_values(FEATURE_SETS[name]))
    a = result.arrays
    for key in ("lp_unfunded", "lp_capital_outstanding", "lp_pref_outstanding", "accrued_carry",
                "clawback_exposure", "escrow_balance", "lp_distributions", "gp_carry", "mgmt_fee_net"):  # fmt: skip
        assert a[key].min() >= -1e-9, key
    assert a["lp_contributions"].sum() <= base_config.fund.lp_commitment * 1.10 + 1e-9  # commitments plus recycling cap
    assert a["lp_nav"][-1] == pytest.approx(0.0)
    assert a["portfolio_fv"][-1] == pytest.approx(0.0)


def test_net_metrics_at_liquidation_are_internally_consistent(base_config):
    m = run_fund(base_config).summary
    assert m["rvpi"] == pytest.approx(0.0)
    assert m["tvpi"] == pytest.approx(m["dpi"])
    assert m["dpi"] == pytest.approx(m["total_lp_distributions"] / m["total_lp_contributions"])
    assert m["net_irr"] < m["gross_irr"]
    assert m["tvpi"] < m["gross_moic"]


def test_carry_is_only_paid_when_lps_have_cleared_the_hurdle(base_config):
    """Whole-of-fund: once any carry has been paid at liquidation, LP net IRR is at least the pref."""
    for shock in (-0.6, -0.4, -0.2, 0.0, 0.3):
        result = run_fund(base_config.with_values({"shocks.exit_multiple_pct": shock}), interim=False)
        if result.totals["carry_net"] > 1e-6:
            assert result.summary["net_irr"] >= base_config.waterfall.pref_rate - 1e-6
        else:
            assert result.summary["net_irr"] <= base_config.waterfall.pref_rate + 1e-6


def test_american_and_european_total_carry_agree_on_the_sample_fund(base_config):
    european = run_fund(base_config)
    american = run_fund(base_config.with_values({"waterfall.type": "american"}))
    assert american.totals["carry_net"] == pytest.approx(european.totals["carry_net"], rel=1e-9)
    assert american.totals["first_carry_quarter"] < european.totals["first_carry_quarter"]


def test_interim_flag_does_not_change_cash_flows(base_config):
    full, fast = run_fund(base_config), run_fund(base_config, interim=False)
    for key in ("lp_contributions", "lp_distributions", "gp_carry", "mgmt_fee_net"):
        np.testing.assert_allclose(full.arrays[key], fast.arrays[key])
    assert fast.summary["net_irr"] == pytest.approx(full.summary["net_irr"])


# --------------------------------------------------------------------------- #
# Fees, expenses and the GP commitment
# --------------------------------------------------------------------------- #
def fee_fund(**fee_terms):
    deal = one_deal(400.0, 2.0, entry="2020-12-31", exit="2028-12-31")
    fund = {"committed_capital": 1000.0, "gp_commitment_pct": 0.10}
    return make_config([deal], fund=fund, management_fee=fee_terms)


def test_fee_on_committed_capital_during_the_investment_period():
    result = run_fund(fee_fund(rate_investment_period=0.02, rate_post_investment_period=0.02,
                               basis_post_investment_period="committed"))  # fmt: skip
    fees = result.arrays["mgmt_fee_net"]
    # 2% a year on LP commitments of 900, charged quarterly in advance: 4.5 a quarter.
    np.testing.assert_allclose(fees[:-1], 4.5)
    assert fees[-1] == 0.0  # nothing is charged at liquidation
    assert result.totals["total_management_fees"] == pytest.approx(4.5 * 32)


def test_fee_steps_down_to_invested_capital_after_the_investment_period():
    result = run_fund(fee_fund(rate_investment_period=0.02, rate_post_investment_period=0.015,
                               basis_post_investment_period="net_invested"))  # fmt: skip
    fees = result.arrays["mgmt_fee_net"]
    np.testing.assert_allclose(fees[:20], 4.5)  # five years on commitments
    np.testing.assert_allclose(fees[20:-1], 0.015 * 0.90 * 400.0 / 4.0)  # then 1.5% on the LPs' share of cost


def test_portfolio_fee_offset_reduces_the_management_fee():
    plain = run_fund(fee_fund(rate_investment_period=0.02))
    offset = run_fund(fee_fund(rate_investment_period=0.02, transaction_fee_pct=0.02, offset_pct=1.0))
    half = run_fund(fee_fund(rate_investment_period=0.02, transaction_fee_pct=0.02, offset_pct=0.5))
    transaction_fee = 0.02 * 400.0
    saved = plain.totals["total_management_fees"] - offset.totals["total_management_fees"]
    assert saved == pytest.approx(transaction_fee * 0.90)  # the LPs' 90% share of the fee is credited in full
    assert plain.totals["total_management_fees"] - half.totals["total_management_fees"] == pytest.approx(saved / 2)
    # The credit exceeds one quarter's fee (4.5), so it carries forward.
    assert offset.arrays["mgmt_fee_net"][0] == pytest.approx(0.0)
    assert offset.arrays["mgmt_fee_net"][1] == pytest.approx(9.0 - 7.2)


def test_gp_commitment_pays_no_fee_and_no_carry():
    cfg = fee_fund(rate_investment_period=0.02)
    result = run_fund(cfg)
    totals = result.totals
    assert totals["gp_commit_contributions"] == pytest.approx(0.10 * 400.0)  # its share of cost only
    assert totals["gp_commit_distributions"] == pytest.approx(0.10 * 800.0)  # its full share of proceeds
    lp_share_of_cost = 0.90 * 400.0
    assert totals["total_lp_contributions"] == pytest.approx(lp_share_of_cost + totals["total_management_fees"])


def test_expenses_are_shared_pro_rata_by_commitment():
    cfg = make_config([one_deal(100.0, 2.0)], fund={"gp_commitment_pct": 0.10},
                      expenses={"organizational": 10.0, "annual_operating": 4.0})  # fmt: skip
    result = run_fund(cfg)
    a = result.arrays
    assert a["expenses"][0] == pytest.approx(11.0)  # organisational cost plus the first quarter
    assert a["lp_called_expenses"][0] == pytest.approx(0.9 * 11.0)
    assert result.totals["total_expenses"] == pytest.approx(10.0 + 20 * 1.0)


# --------------------------------------------------------------------------- #
# Funding mechanics
# --------------------------------------------------------------------------- #
def test_over_commitment_is_rejected_with_a_clear_message():
    with pytest.raises(ConfigError, match="Over-commitment"):
        run_fund(make_config([one_deal(1200.0, 2.0)]))


def test_fees_are_netted_from_proceeds_once_commitments_are_exhausted():
    deal = one_deal(990.0, 2.0, entry="2020-12-31", exit="2026-12-31")
    cfg = make_config([deal], management_fee={"rate_investment_period": 0.02, "rate_post_investment_period": 0.02,
                                              "basis_post_investment_period": "committed"})  # fmt: skip
    result = run_fund(cfg)
    a = result.arrays
    assert a["lp_contributions"].sum() == pytest.approx(1000.0)  # never more than commitments
    assert a["netted_from_proceeds"].sum() > 0.0
    assert any("exhausted" in flag for flag in result.flags)
    # Every fee is eventually settled: called, or taken out of the exit proceeds.
    assert a["lp_called_fees"].sum() + a["netted_from_proceeds"].sum() == pytest.approx(a["mgmt_fee_net"].sum())


def test_recycling_lets_returned_cost_be_called_again():
    deals = [
        {"name": "Quick flip", "investments": [{"date": "2020-12-31", "amount": 600.0}],
         "exit_date": "2022-12-31", "gross_moic": 1.5},
        {"name": "Second", "investments": [{"date": "2023-12-31", "amount": 500.0}],
         "exit_date": "2027-12-31", "gross_moic": 2.0},
    ]  # fmt: skip
    with pytest.raises(ConfigError, match="Over-commitment"):
        run_fund(make_config(deals))
    result = run_fund(make_config(deals, recycling={"enabled": True, "cap_pct_of_commitments": 0.20}))
    assert result.totals["recycled_capital"] == pytest.approx(200.0)  # capped at 20% of commitments
    assert result.arrays["lp_contributions"].sum() == pytest.approx(1100.0)  # more than was committed


def test_subscription_line_defers_calls_raises_irr_and_lowers_the_multiple(base_config):
    plain = run_fund(base_config)
    lined = run_fund(base_config.with_values({"subscription_line.enabled": True}))
    assert lined.arrays["lp_contributions"][:2].sum() == pytest.approx(0.0)  # first drawings repaid two quarters later
    assert plain.arrays["lp_contributions"][0] > 0.0
    assert lined.summary["net_irr"] > plain.summary["net_irr"]
    assert lined.summary["tvpi"] < plain.summary["tvpi"]
    assert lined.totals["total_line_interest"] > 0.0
    assert lined.arrays["line_balance"][-1] == pytest.approx(0.0)
