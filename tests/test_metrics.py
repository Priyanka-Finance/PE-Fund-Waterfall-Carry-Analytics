"""Return maths: IRR, multiples and public-market equivalent."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from pefund.metrics import irr_from_years, ks_pme, multiples, safe_ratio, xirr


def test_irr_of_a_single_period_investment():
    assert irr_from_years([0, 1], [-100, 110]) == pytest.approx(0.10, abs=1e-10)
    assert irr_from_years([0, 2], [-100, 121]) == pytest.approx(0.10, abs=1e-10)
    assert irr_from_years([0, 5], [-100, 200]) == pytest.approx(2 ** (1 / 5) - 1, abs=1e-10)


def test_irr_handles_losses_and_very_high_returns():
    assert irr_from_years([0, 3], [-100, 50]) == pytest.approx(0.5 ** (1 / 3) - 1, abs=1e-9)
    assert irr_from_years([0, 0.25], [-100, 300]) == pytest.approx(3**4 - 1, rel=1e-8)  # 8,000% a year


def test_irr_solves_the_npv_equation_for_irregular_flows():
    years = [0.0, 0.5, 1.25, 3.0, 4.75, 6.0]
    flows = [-100.0, -50.0, 20.0, 60.0, 90.0, 80.0]
    rate = irr_from_years(years, flows)
    assert sum(cf / (1 + rate) ** t for t, cf in zip(years, flows, strict=True)) == pytest.approx(0.0, abs=1e-8)


@pytest.mark.parametrize(
    "flows",
    [[-100.0, 0.0], [100.0, 50.0], [-100.0, -50.0], [0.0, 0.0], [-100.0]],
)
def test_irr_is_undefined_without_both_an_outflow_and_an_inflow(flows):
    assert math.isnan(irr_from_years(list(range(len(flows))), flows))


def test_xirr_uses_actual_days_like_excel():
    dates = [dt.date(2020, 12, 31), dt.date(2021, 12, 31), dt.date(2022, 12, 31)]
    assert xirr(dates, [-100, 0, 121]) == pytest.approx(0.10, abs=1e-10)
    # A leap year adds a day, so the same nominal gain is a touch below 10% a year.
    leap = [dt.date(2023, 12, 31), dt.date(2024, 12, 31)]
    assert xirr(leap, [-100, 110]) == pytest.approx(1.10 ** (365 / 366) - 1, abs=1e-10)
    assert math.isnan(xirr([], []))


def test_multiples_and_safe_ratio():
    m = multiples(paid_in=100.0, distributed=60.0, nav=90.0)
    assert (m["dpi"], m["rvpi"], m["tvpi"]) == pytest.approx((0.6, 0.9, 1.5))
    assert math.isnan(safe_ratio(1.0, 0.0))
    assert math.isnan(multiples(0.0, 0.0, 0.0)["tvpi"])


def test_pme_is_one_when_the_fund_matches_the_benchmark():
    # 100 in, grows at exactly the benchmark rate for three years.
    assert ks_pme([0, 3], [100, 0], [0, 100 * 1.08**3], 0.0, 0.08) == pytest.approx(1.0)
    assert ks_pme([0, 3], [100, 0], [0, 150], 0.0, 0.08) > 1.0
    assert ks_pme([0, 3], [100, 0], [0, 0], 110.0, 0.08) < 1.0  # residual NAV counts, but it is below benchmark
