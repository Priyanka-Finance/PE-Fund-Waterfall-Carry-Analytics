"""Quarter-end calendar helpers.

The model runs on a quarter-end grid: every cash flow (capital call, fee,
investment, realisation, distribution) is dated on a calendar quarter-end.
Quarter ``0`` is the fund's first close.  Preferred return and IRR use the
actual number of days between those dates (ACT/365).
"""

from __future__ import annotations

import calendar
import datetime as dt

import numpy as np

DAYS_IN_YEAR = 365.0


def to_date(value: object) -> dt.date:
    """Accept ``date``, ``datetime`` or an ISO string and return a ``date``."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        return dt.date.fromisoformat(value.strip()[:10])
    # pandas.Timestamp and numpy.datetime64 arrive here when reading Excel
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    raise TypeError(f"Cannot interpret {value!r} as a date")


def quarter_end(d: dt.date) -> dt.date:
    """Return the calendar quarter-end on or after ``d``."""
    month = ((d.month - 1) // 3 + 1) * 3
    return dt.date(d.year, month, calendar.monthrange(d.year, month)[1])


def add_quarters(d: dt.date, n: int) -> dt.date:
    """Move ``n`` quarters from a quarter-end date (``n`` may be negative)."""
    d = quarter_end(d)
    months = d.year * 12 + (d.month - 1) + 3 * n
    year, month = divmod(months, 12)
    month += 1
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def quarter_index(first_close: dt.date, d: dt.date) -> int:
    """Number of quarters between the first close and ``d`` (snapped to quarter-end)."""
    a, b = quarter_end(first_close), quarter_end(d)
    return (b.year - a.year) * 4 + (b.month - a.month) // 3


def quarter_grid(first_close: dt.date, n: int) -> list[dt.date]:
    """``n`` consecutive quarter-end dates starting at the first close."""
    return [add_quarters(first_close, i) for i in range(n)]


def year_fractions(dates: list[dt.date]) -> np.ndarray:
    """Years elapsed since the first date in ``dates`` on an ACT/365 basis."""
    origin = dates[0]
    return np.array([(d - origin).days / DAYS_IN_YEAR for d in dates], dtype=float)
