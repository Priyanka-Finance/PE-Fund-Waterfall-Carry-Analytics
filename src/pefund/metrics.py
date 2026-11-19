"""Performance maths: IRR, multiples and public-market equivalent.

Conventions used throughout the project
---------------------------------------
* Cash flows are signed from the investor's point of view: contributions are
  negative, distributions positive.
* IRR is an annualised, money-weighted rate on actual dates (the same
  definition as Excel's ``XIRR``): the rate ``r`` solving
  ``sum(cf_i / (1 + r) ** (days_i / 365)) = 0``.
* Multiples: ``DPI = distributions / paid-in``, ``RVPI = NAV / paid-in``,
  ``TVPI = DPI + RVPI``, ``MOIC = total value / invested cost``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np

from .timeline import DAYS_IN_YEAR

_MIN_RATE = -0.9999
_MAX_RATE = 1.0e4


def _npv(rate: float, years: np.ndarray, flows: np.ndarray) -> float:
    return float(np.sum(flows / (1.0 + rate) ** years))


def irr_from_years(years: Sequence[float], flows: Sequence[float]) -> float:
    """Annualised IRR given cash flows and their timing in years.

    Returns ``nan`` when no IRR exists (all flows have the same sign, or the
    investment is a total loss so the rate would be -100%).
    """
    years_arr = np.asarray(years, dtype=float)
    flows_arr = np.asarray(flows, dtype=float)
    mask = np.abs(flows_arr) > 1e-12
    if mask.sum() < 2:
        return float("nan")
    years_arr, flows_arr = years_arr[mask], flows_arr[mask]
    if (flows_arr > 0).all() or (flows_arr < 0).all():
        return float("nan")
    years_arr = years_arr - years_arr.min()

    # Bracket a sign change, expanding upwards from a sensible range.
    low, high = _MIN_RATE, 1.0
    f_low = _npv(low, years_arr, flows_arr)
    f_high = _npv(high, years_arr, flows_arr)
    while f_low * f_high > 0 and high < _MAX_RATE:
        high *= 4.0
        f_high = _npv(high, years_arr, flows_arr)
    if f_low * f_high > 0:
        return float("nan")

    # Bisection: slower than Newton but cannot diverge, which matters when this
    # runs thousands of times inside the Monte Carlo engine.
    for _ in range(200):
        mid = 0.5 * (low + high)
        f_mid = _npv(mid, years_arr, flows_arr)
        if abs(f_mid) < 1e-12 or (high - low) < 1e-12:
            return mid
        if f_low * f_mid <= 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return 0.5 * (low + high)


def xirr(dates: Sequence[dt.date], flows: Sequence[float]) -> float:
    """Annualised IRR on actual dates (ACT/365), equivalent to Excel ``XIRR``."""
    if len(dates) == 0:
        return float("nan")
    origin = min(dates)
    years = [(d - origin).days / DAYS_IN_YEAR for d in dates]
    return irr_from_years(years, flows)


def safe_ratio(numerator: float, denominator: float) -> float:
    """Division that returns ``nan`` instead of raising when the denominator is zero."""
    if denominator is None or abs(denominator) < 1e-12:
        return float("nan")
    return float(numerator) / float(denominator)


def multiples(paid_in: float, distributed: float, nav: float) -> dict[str, float]:
    """DPI, RVPI and TVPI from cumulative paid-in, distributions and residual NAV."""
    dpi = safe_ratio(distributed, paid_in)
    rvpi = safe_ratio(nav, paid_in)
    return {"dpi": dpi, "rvpi": rvpi, "tvpi": dpi + rvpi}


def ks_pme(
    years: Sequence[float],
    contributions: Sequence[float],
    distributions: Sequence[float],
    nav: float,
    benchmark_return: float,
) -> float:
    """Kaplan-Schoar public-market equivalent against a constant-return benchmark.

    Each cash flow is compounded to the measurement date at the benchmark
    return.  A PME above 1.0 means the fund beat the benchmark.  ``contributions``
    and ``distributions`` are both positive amounts.
    """
    years_arr = np.asarray(years, dtype=float)
    horizon = float(years_arr.max()) if len(years_arr) else 0.0
    growth = (1.0 + benchmark_return) ** (horizon - years_arr)
    fv_contributions = float(np.sum(np.asarray(contributions, dtype=float) * growth))
    fv_distributions = float(np.sum(np.asarray(distributions, dtype=float) * growth))
    return safe_ratio(fv_distributions + nav, fv_contributions)
