"""Deal cash flows, the operating model, cost relief and fair-value marks."""

from __future__ import annotations

import math

import numpy as np
import pytest

from conftest import make_config
from pefund import run_fund
from pefund.deals import build_deal_schedule

OPERATING = {
    "name": "Buyout",
    "investments": [{"date": "2020-12-31", "amount": 60.0}],
    "exit_date": "2025-12-31",
    "operating": {
        "entry_ebitda": 10.0,
        "entry_ev_multiple": 10.0,
        "entry_leverage": 4.0,
        "ebitda_growth": 0.10,
        "exit_ev_multiple": 11.0,
        "cash_conversion": 0.30,
    },
}


def schedule(deals, **sections):
    return build_deal_schedule(make_config(deals, **sections))


def test_operating_case_matches_a_hand_calculation():
    detail = schedule([OPERATING]).details[0]
    # Entry: EV 100, net debt 40, equity 60 -> the fund's 60 buys all of it.
    assert detail["ownership"] == pytest.approx(1.0)
    exit_ebitda = 10.0 * 1.1**5
    cumulative = 10.0 * (1.1**5 - 1.0) / math.log(1.1)
    exit_net_debt = 40.0 - 0.30 * cumulative
    proceeds = exit_ebitda * 11.0 - exit_net_debt
    assert detail["exit_ebitda"] == pytest.approx(exit_ebitda)
    assert detail["exit_net_debt"] == pytest.approx(exit_net_debt)
    assert detail["proceeds"] == pytest.approx(proceeds)
    assert detail["gross_moic"] == pytest.approx(proceeds / 60.0)


def test_value_creation_bridge_adds_up_to_the_gain():
    detail = schedule([OPERATING]).details[0]
    bridge = sum(detail[k] for k in ("vc_ebitda_growth", "vc_multiple_expansion", "vc_combined", "vc_deleveraging",
                                     "vc_equity_floor"))  # fmt: skip
    assert bridge == pytest.approx(detail["gain"])
    assert detail["vc_ebitda_growth"] == pytest.approx((10.0 * 1.1**5 - 10.0) * 10.0)
    assert detail["vc_multiple_expansion"] == pytest.approx(1.0 * 10.0)
    assert detail["vc_equity_floor"] == 0.0


def test_leverage_amplifies_an_exit_multiple_shock():
    base = schedule([OPERATING]).details[0]["proceeds"]
    shocked = schedule([OPERATING], shocks={"exit_multiple_pct": -0.10}).details[0]["proceeds"]
    assert shocked / base - 1.0 < -0.10  # a 10% fall in enterprise value costs equity more than 10%


def test_equity_cannot_fall_below_zero():
    detail = schedule([OPERATING], shocks={"exit_multiple_pct": -0.95}).details[0]
    assert detail["proceeds"] == 0.0
    assert detail["vc_equity_floor"] > 0.0
    bridge = sum(detail[k] for k in ("vc_ebitda_growth", "vc_multiple_expansion", "vc_combined", "vc_deleveraging",
                                     "vc_equity_floor"))  # fmt: skip
    assert bridge == pytest.approx(-60.0)  # the whole cheque is lost, and the bridge still reconciles


def test_longer_hold_compounds_ebitda_but_lowers_the_irr():
    base = run_fund(make_config([OPERATING])).deals.iloc[0]
    later = run_fund(make_config([OPERATING], shocks={"hold_shift_quarters": 8})).deals.iloc[0]
    assert later["proceeds"] > base["proceeds"] and later["hold_years"] == base["hold_years"] + 2
    assert later["gross_irr"] < base["gross_irr"]


def test_growth_shock_changes_only_operating_deals():
    direct = {"name": "Direct", "investments": [{"date": "2020-12-31", "amount": 50.0}], "exit_date": "2024-12-31",
              "gross_moic": 2.0}  # fmt: skip
    base = schedule([OPERATING, direct])
    shocked = schedule([OPERATING, direct], shocks={"ebitda_growth_shift": -0.05})
    assert shocked.details[0]["proceeds"] < base.details[0]["proceeds"]
    assert shocked.details[1]["proceeds"] == base.details[1]["proceeds"]


def test_follow_on_and_partial_exit_cash_flows():
    deal = {
        "name": "Growth",
        "investments": [{"date": "2020-12-31", "amount": 20.0}, {"date": "2021-12-31", "amount": 10.0}],
        "exit_date": "2024-12-31",
        "gross_moic": 3.0,
        "proceeds_events": [{"date": "2023-12-31", "kind": "partial_exit", "share": 0.4, "stake_sold": 0.5}],
    }
    s = schedule([deal])
    assert s.inv[0].sum() == pytest.approx(30.0) and s.inv[0, 4] == pytest.approx(10.0)
    assert s.proc[0].sum() == pytest.approx(90.0)
    assert s.proc[0, 12] == pytest.approx(36.0) and s.proc[0, 16] == pytest.approx(54.0)
    assert s.relief[0, 12] == pytest.approx(15.0)  # half the 30 of cost leaves with half the stake
    assert s.relief[0].sum() == pytest.approx(30.0)  # all cost is relieved by the final exit


def test_dividend_recap_returns_cost_first():
    deal = {
        "name": "Recap",
        "investments": [{"date": "2020-12-31", "amount": 100.0}],
        "exit_date": "2024-12-31",
        "gross_moic": 2.0,
        "proceeds_events": [{"date": "2022-12-31", "kind": "dividend_recap", "share": 0.2}],
    }
    s = schedule([deal])
    assert s.proc[0, 8] == pytest.approx(40.0)
    assert s.relief[0, 8] == pytest.approx(40.0)  # the whole dividend is treated as return of cost
    assert s.relief[0, 16] == pytest.approx(60.0)


def test_marks_hold_at_cost_then_converge_to_exit_value():
    deal = {"name": "Mark", "investments": [{"date": "2020-12-31", "amount": 100.0}], "exit_date": "2024-12-31",
            "gross_moic": 2.0}  # fmt: skip
    fv = schedule([deal]).fv[0]
    np.testing.assert_allclose(fv[:5], 100.0)  # first year at cost
    assert fv[10] == pytest.approx(100.0 + 100.0 * (10 - 4) / (16 - 4))  # half-way up the ramp
    assert (np.diff(fv[4:16]) > 0).all()
    assert fv[16] == 0.0  # nothing is held after the exit


def test_a_write_off_is_marked_down_gradually_and_returns_nothing():
    deal = {"name": "Zero", "investments": [{"date": "2020-12-31", "amount": 50.0}], "exit_date": "2023-12-31",
            "gross_moic": 0.0}  # fmt: skip
    s = schedule([deal])
    assert s.proc.sum() == 0.0 and s.relief[0, 12] == pytest.approx(50.0)
    assert s.fv[0, 4] == pytest.approx(50.0) and 0.0 < s.fv[0, 11] < 50.0
    assert math.isnan(run_fund(make_config([deal])).deals.iloc[0]["gross_irr"])


def test_hold_shift_is_bounded_by_the_last_cheque_and_the_fund_term():
    deal = {"name": "Late", "investments": [{"date": "2021-12-31", "amount": 10.0}], "exit_date": "2022-12-31",
            "gross_moic": 2.0}  # fmt: skip
    early = schedule([deal], shocks={"hold_shift_quarters": -12})
    assert early.exit_q[0] == early.entry_q[0] + 1  # never before the cheque has been written
    late = schedule([deal], shocks={"hold_shift_quarters": 100})
    assert late.exit_q[0] == 48  # ten-year term plus two years of extensions


def test_per_deal_overrides_combine_with_fund_wide_shocks():
    direct = {"name": "D", "investments": [{"date": "2020-12-31", "amount": 50.0}], "exit_date": "2024-12-31",
              "gross_moic": 2.0}  # fmt: skip
    shocks = {"exit_multiple_pct": 0.10, "deal_overrides": {"D": {"exit_multiple_pct": -0.50}}}
    assert schedule([direct], shocks=shocks).details[0]["proceeds"] == pytest.approx(100.0 * 1.10 * 0.50)
    forced = {"deal_overrides": {"D": {"gross_moic": 0.3}}}
    assert schedule([direct], shocks=forced).details[0]["proceeds"] == pytest.approx(15.0)
    written_off = {"deal_overrides": {"D": {"write_off": True}}}
    assert schedule([direct], shocks=written_off).details[0]["proceeds"] == 0.0
