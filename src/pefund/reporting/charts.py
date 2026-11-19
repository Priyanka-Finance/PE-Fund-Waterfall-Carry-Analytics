"""Static charts (PNG) for reports, the README and the Excel dashboard.

Design rules applied to every chart
-----------------------------------
* One job per colour.  Series identity uses a fixed three-hue order that is
  validated as colour-blind safe in every pairing; magnitude uses a single-hue
  ramp; gains versus losses use a blue/red pair; totals are neutral.
* One axis per chart - never two scales on one plot.
* Thin marks, hairline grid, rounded bar ends, a small gap between touching bars.
* A legend whenever there are two or more series, plus sparing direct labels.
* Text is always ink-coloured; only marks carry the series colour.

Every chart has a table twin: the CSV of the same name written by the pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402
from matplotlib.ticker import MultipleLocator  # noqa: E402

from ..fund import FundResult  # noqa: E402

# --------------------------------------------------------------------------- #
# Palette (validated: three categorical slots pass every pairing under
# protanopia and deuteranopia simulation; see docs/methodology.md)
# --------------------------------------------------------------------------- #
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
NEUTRAL = "#c3c2b7"  # de-emphasised marks, e.g. capital simply being returned
TOTAL = "#52514e"  # level / total bars in bridge charts
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
RED = "#e34948"  # negative pole of the blue/red diverging pair
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

EDGE = 0.04  # shared left edge of title, subtitle, legend and footnote (figure fraction)
#: Masks gridlines and reference lines behind a value label.
LABEL_BOX = {"facecolor": SURFACE, "edgecolor": "none", "pad": 1.5}
BAR_Z, REFERENCE_Z, LINE_Z, MARKER_Z = 2.2, 2.4, 4.0, 5.0  # the axis line sits at 2.5, above the bars

PX = 1.0 / 100.0  # one design pixel, in inches
PT = 72.0 * PX  # one design pixel, in points
DPI = 200
FIGSIZE = (9.6, 5.4)


def _style() -> dict[str, Any]:
    return {
        "font.family": "sans-serif",
        "font.sans-serif": ["Inter", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 9.5,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": BASELINE,
        "axes.linewidth": 1.0 * PT,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.spines.left": False,
        "axes.grid": False,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 1.0 * PT,
        "grid.linestyle": "-",
        "xtick.color": INK_MUTED,
        "ytick.color": INK_MUTED,
        "xtick.labelcolor": INK_SECONDARY,
        "ytick.labelcolor": INK_SECONDARY,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "xtick.major.pad": 6,
        "ytick.major.pad": 6,
        "text.color": INK,
        "axes.labelcolor": INK_SECONDARY,
        "legend.frameon": False,
        "lines.linewidth": 2.0 * PT,
        "lines.solid_capstyle": "round",
        "lines.solid_joinstyle": "round",
    }


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def fmt_pct(value: float, digits: int = 1) -> str:
    return "n/a" if not np.isfinite(value) else f"{value * 100:.{digits}f}%"


def fmt_multiple(value: float, digits: int = 2) -> str:
    return "n/a" if not np.isfinite(value) else f"{value:.{digits}f}x"


def fmt_money(value: float, currency: str = "$", unit: str = "m", digits: int = 0) -> str:
    if not np.isfinite(value):
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}{currency}{abs(value):,.{digits}f}{unit}"


def fmt_bps(value: float) -> str:
    return f"{value:+,.0f} bps"


def _currency_symbol(result: FundResult) -> tuple[str, str]:
    symbols = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}
    fund = result.config.fund
    return symbols.get(fund.currency.upper(), fund.currency + " "), fund.unit


def _lower_first(label: str) -> str:
    """'Net IRR' -> 'net IRR' for use mid-sentence; acronyms are left alone."""
    return label[:1].lower() + label[1:] if len(label) > 1 and label[1].islower() else label


def _format_value(value: float, unit: str, currency: str = "$", money_unit: str = "m") -> str:
    if unit == "pct":
        return fmt_pct(value)
    if unit == "multiple":
        return fmt_multiple(value)
    if unit == "money":
        return fmt_money(value, currency, money_unit)
    if unit == "years":
        return f"{value:.1f}y" if np.isfinite(value) else "n/a"
    return f"{value:,.2f}"


#: Parameters expressed as a change from base, shown with an explicit sign.
SIGNED_PARAMETERS = ("exit_multiple",)


def _format_input(value: float, unit: str, signed: bool = False) -> str:
    """Label for an assumption value on an axis or in a tornado row."""
    if unit == "pct":
        return f"{value * 100:+g}%" if signed and value != 0 else f"{value * 100:g}%"
    if unit == "pct_points":
        return f"{value * 100:+g} pts" if value != 0 else "0 pts"
    if unit == "quarters":
        return f"{int(value):+d}q" if value != 0 else "0q"
    return f"{value:g}"


def _axis_values(values: Iterable[float], unit: str, signed: bool) -> list[str]:
    return [_format_input(float(value), unit, signed) for value in values]


# --------------------------------------------------------------------------- #
# Figure scaffolding
# --------------------------------------------------------------------------- #
def _figure(title: str, subtitle: str, *, ncols: int = 1, figsize: tuple[float, float] = FIGSIZE,
            left: float = 0.085, right: float = 0.965, top: float = 0.77, bottom: float = 0.13,
            wspace: float = 0.22, sharey: bool = False):  # fmt: skip
    fig, axes = plt.subplots(1, ncols, figsize=figsize, sharey=sharey)
    fig.subplots_adjust(left=left, right=right, top=top, bottom=bottom, wspace=wspace)
    fig.text(EDGE, 0.945, title, fontsize=14.5, fontweight="bold", color=INK, va="center")
    fig.text(EDGE, 0.89, subtitle, fontsize=10, color=INK_SECONDARY, va="center")
    return fig, axes


def _footnote(fig, text: str) -> None:
    fig.text(EDGE, 0.03, text, fontsize=8, color=INK_MUTED, va="center")


def _legend(fig, entries: Sequence[tuple[str, str, str]], y: float = 0.835) -> None:
    """Horizontal legend row.  ``entries`` are ``(label, colour, kind)`` with kind ``bar`` or ``line``."""
    handles = []
    for label, colour, kind in entries:
        if kind == "line":
            handles.append(plt.Line2D([0], [0], color=colour, lw=2.0 * PT, label=label))
        else:
            handles.append(plt.Rectangle((0, 0), 1, 1, facecolor=colour, edgecolor="none", label=label))
    fig.legend(
        handles=handles, loc="center left", bbox_to_anchor=(EDGE - 0.006, y), ncol=len(handles), fontsize=9,
        handlelength=1.1, handleheight=0.9, columnspacing=1.6, handletextpad=0.5, labelcolor=INK_SECONDARY,
    )  # fmt: skip


def _data_per_px(ax) -> tuple[float, float]:
    """Size of one design pixel in data units along x and y."""
    fig = ax.figure
    fig.canvas.draw()
    inverse = ax.transData.inverted()
    origin = inverse.transform((0.0, 0.0))
    step = inverse.transform((fig.dpi * PX, fig.dpi * PX))
    return abs(step[0] - origin[0]), abs(step[1] - origin[1])


def _bar(ax, lo: float, hi: float, centre: float, thickness: float, colour: str, *, horizontal: bool = False,
         round_end: bool = True, radius_px: float = 4.0, gap: bool = True) -> None:  # fmt: skip
    """Bar from ``lo`` to ``hi`` with a rounded data end and a square baseline end.

    ``lo`` is the end nearer the baseline, ``hi`` the data end; ``hi < lo`` draws
    a bar pointing the other way.  A thin surface-coloured edge provides the gap
    between touching bars and stacked segments.
    """
    if abs(hi - lo) < 1e-12:
        return
    px_x, px_y = _data_per_px(ax)
    along, across = (px_x, px_y) if horizontal else (px_y, px_x)
    r_along = min(radius_px * along, abs(hi - lo)) if round_end else 0.0
    r_across = min(radius_px * across, thickness / 2.0) if round_end else 0.0
    direction = 1.0 if hi >= lo else -1.0
    a0, a1 = centre - thickness / 2.0, centre + thickness / 2.0
    # Outline in (across, along) coordinates, starting at the baseline.
    points = [
        (a0, lo), (a0, hi - direction * r_along), (a0, hi), (a0 + r_across, hi),
        (a1 - r_across, hi), (a1, hi), (a1, hi - direction * r_along), (a1, lo), (a0, lo),
    ]  # fmt: skip
    codes = [MplPath.MOVETO, MplPath.LINETO, MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO,
             MplPath.CURVE3, MplPath.CURVE3, MplPath.LINETO, MplPath.CLOSEPOLY]  # fmt: skip
    vertices = [(y, x) for x, y in points] if horizontal else points
    ax.add_patch(
        PathPatch(
            MplPath(vertices, codes),
            facecolor=colour,
            edgecolor=SURFACE if gap else "none",
            linewidth=2.0 * PT if gap else 0.0,
            zorder=BAR_Z,
        )  # fmt: skip
    )


def _thickness(ax, band: float, *, horizontal: bool = False, max_px: float = 24.0, fill: float = 0.62) -> float:
    """Bar thickness in data units: a share of the band, capped at ``max_px`` design pixels."""
    px_x, px_y = _data_per_px(ax)
    return min(fill * band, max_px * (px_y if horizontal else px_x))


def _grid(ax, axis: str = "y") -> None:
    ax.grid(True, axis=axis)
    ax.tick_params(axis="both", labelsize=9)


def _save(fig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def _year_positions(result: FundResult) -> np.ndarray:
    """Decimal calendar year of each quarter-end, e.g. 2026.75 for 30 September 2026."""
    return np.array([d.year + (d.month / 12.0) for d in result.dates])


# --------------------------------------------------------------------------- #
# 1. J-curve
# --------------------------------------------------------------------------- #
def chart_j_curve(result: FundResult, path: Path) -> Path:
    currency, unit = _currency_symbol(result)
    a = result.arrays
    frame = result.quarterly.assign(year=lambda d: d["date"].dt.year)
    yearly = frame.groupby("year")[["lp_contributions", "lp_distributions"]].sum()
    years = yearly.index.to_numpy()
    cumulative = a["cum_net_cash_flow"]
    x_quarters = _year_positions(result)

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "J-curve: LP cash flows over the life of the fund",
            "Capital called and distributed each year, with cumulative net cash flow to LPs",
        )
        low = min(cumulative.min(), -yearly["lp_contributions"].max()) * 1.22
        high = max(cumulative.max(), yearly["lp_distributions"].max()) * 1.12
        ax.set_xlim(years.min() - 0.15, years.max() + 1.15)
        ax.set_ylim(low, high)
        _grid(ax)
        ax.spines["bottom"].set_visible(False)
        thickness = _thickness(ax, 1.0)
        for year, row in yearly.iterrows():
            # Calls point down and distributions up from the same zero line, so they share a column.
            _bar(ax, 0.0, -row["lp_contributions"], year + 0.5, thickness, ORANGE)
            _bar(ax, 0.0, row["lp_distributions"], year + 0.5, thickness, BLUE)
        ax.axhline(0.0, color=BASELINE, lw=1.0 * PT, zorder=REFERENCE_Z)
        ax.plot(x_quarters, cumulative, color=AQUA, zorder=LINE_Z)

        trough = int(np.argmin(cumulative))
        ax.plot(
            x_quarters[trough], cumulative[trough], "o", ms=8 * PT, color=AQUA, mec=SURFACE, mew=2 * PT, zorder=MARKER_Z
        )
        ax.annotate(
            f"Deepest point {fmt_money(cumulative[trough], currency, unit)}, {result.dates[trough]:%b %Y}",
            (x_quarters[trough], cumulative[trough]), xytext=(10, -9), textcoords="offset points",
            ha="left", va="top", fontsize=9, color=INK_SECONDARY, bbox=LABEL_BOX,
        )  # fmt: skip
        ax.plot(x_quarters[-1], cumulative[-1], "o", ms=8 * PT, color=AQUA, mec=SURFACE, mew=2 * PT, zorder=MARKER_Z)
        ax.annotate(
            f"Net gain {fmt_money(cumulative[-1], currency, unit)}",
            (x_quarters[-1], cumulative[-1]), xytext=(-8, 10), textcoords="offset points",
            ha="right", va="bottom", fontsize=9, color=INK_SECONDARY, bbox=LABEL_BOX,
        )  # fmt: skip
        positive = np.nonzero(cumulative > 0)[0]
        if len(positive) and positive[0] > 0:
            q = int(positive[0])
            ax.annotate(
                f"Cumulative break-even, {result.dates[q]:%b %Y}", (x_quarters[q], cumulative[q]), xytext=(14, -34),
                textcoords="offset points", ha="left", va="top", fontsize=9, color=INK_SECONDARY, bbox=LABEL_BOX,
                arrowprops={"arrowstyle": "-", "color": INK_MUTED, "lw": 1.0 * PT},
            )  # fmt: skip
        ax.set_xticks(years + 0.5)
        ax.set_xticklabels([str(y) for y in years])
        ax.yaxis.set_major_formatter(lambda v, _: fmt_money(v, currency, unit))
        _legend(fig, [("Capital called", ORANGE, "bar"), ("Distributions", BLUE, "bar"),
                      ("Cumulative net cash flow", AQUA, "line")])  # fmt: skip
        _footnote(fig, "LP class, after fees, expenses and carried interest. Table: quarterly_cash_flows.csv")
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 2. TVPI build-up
# --------------------------------------------------------------------------- #
def chart_tvpi_build(result: FundResult, path: Path) -> Path:
    frame = result.quarterly.assign(year=lambda d: d["date"].dt.year)
    year_end = frame.groupby("year")[["dpi", "rvpi", "tvpi"]].last()
    years = year_end.index.to_numpy()
    valuation_year = result.dates[result.valuation_quarter].year

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "From paper value to cash: how net TVPI builds",
            "Year-end DPI (cash returned) and RVPI (remaining value) per unit of paid-in capital",
        )
        ax.set_xlim(years.min() - 0.6, years.max() + 0.6)
        ax.set_ylim(0, float(np.nanmax(year_end["tvpi"])) * 1.16)
        _grid(ax)
        thickness = _thickness(ax, 1.0)
        for year, row in year_end.iterrows():
            dpi, rvpi = float(np.nan_to_num(row["dpi"])), float(np.nan_to_num(row["rvpi"]))
            _bar(ax, 0.0, dpi, year, thickness, BLUE, round_end=rvpi <= 1e-9)
            _bar(ax, dpi, dpi + rvpi, year, thickness, ORANGE)
            ax.text(year, dpi + rvpi + 0.012 * ax.get_ylim()[1], f"{dpi + rvpi:.2f}x", ha="center", va="bottom",
                    fontsize=9, color=INK, fontweight="bold" if year == valuation_year else "normal",
                    bbox=LABEL_BOX)  # fmt: skip
        ax.axhline(1.0, color=INK_MUTED, lw=1.0 * PT, zorder=REFERENCE_Z)
        ax.set_xticks(years)
        ax.set_xticklabels([str(y) for y in years])
        ax.yaxis.set_major_locator(MultipleLocator(0.5))
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.1f}x")
        _legend(fig, [("DPI - distributed", BLUE, "bar"), ("RVPI - residual value", ORANGE, "bar")])
        _footnote(fig, f"Line at 1.0x: paid-in capital. Bold label: the valuation year ({valuation_year}). "
                       "Table: quarterly_cash_flows.csv")  # fmt: skip
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 3. Waterfall tiers, whole-of-fund versus deal-by-deal
# --------------------------------------------------------------------------- #
def chart_waterfall_tiers(european: FundResult, american: FundResult, path: Path) -> Path:
    currency, unit = _currency_symbol(european)
    series = [
        ("Return of capital", NEUTRAL, ["tier1_return_of_capital"]),
        ("Preferred return", BLUE, ["tier2_preferred_return"]),
        ("LP share of profit split", AQUA, ["tier3_catch_up_lp", "tier4_split_lp"]),
        ("GP carried interest", ORANGE, ["tier3_catch_up_gp", "tier4_split_gp"]),
    ]

    def yearly(result: FundResult) -> pd.DataFrame:
        frame = result.quarterly.assign(year=lambda d: d["date"].dt.year)
        out = pd.DataFrame({name: frame.groupby("year")[cols].sum().sum(axis=1) for name, _, cols in series})
        return out[out.sum(axis=1) > 1e-9]

    tables = [yearly(european), yearly(american)]
    top = max(float(t.sum(axis=1).max()) for t in tables) * 1.12
    years = sorted(set(tables[0].index) | set(tables[1].index))

    with plt.rc_context(_style()):
        fig, axes = _figure(
            "Who is paid when: distributions by waterfall tier",
            "Same fund, same deals. Whole-of-fund pays carry last; deal-by-deal pays it from the first exit",
            ncols=2, sharey=True, left=0.075, wspace=0.08,
        )  # fmt: skip
        panels = [("Whole-of-fund (European)", european), ("Deal-by-deal (American)", american)]
        for ax, table, (label, result) in zip(axes, tables, panels, strict=True):
            ax.set_xlim(min(years) - 0.6, max(years) + 0.6)
            ax.set_ylim(0, top)
            _grid(ax)
            thickness = _thickness(ax, 1.0)
            for year, row in table.iterrows():
                base = 0.0
                present = [(name, colour, float(row[name])) for name, colour, _ in series if row[name] > 1e-9]
                for index, (_, colour, value) in enumerate(present):
                    _bar(ax, base, base + value, year, thickness, colour, round_end=index == len(present) - 1)
                    base += value
            first = result.totals["first_carry_date"]
            carry = fmt_money(result.totals["carry_gross"], currency, unit, 1)
            note = f"Carry {carry}, first paid {first:%b %Y}" if first else "No carry paid"
            ax.text(0.0, 1.125, label, transform=ax.transAxes, fontsize=10.5, fontweight="bold", color=INK)
            ax.text(0.0, 1.045, note, transform=ax.transAxes, fontsize=9.5, color=INK_SECONDARY)
            ax.set_xticks(years)
            ax.set_xticklabels([f"'{str(y)[2:]}" for y in years])
            ax.yaxis.set_major_formatter(lambda v, _: fmt_money(v, currency, unit))
        fig.subplots_adjust(top=0.675)
        _legend(fig, [(name, colour, "bar") for name, colour, _ in series], y=0.83)
        same = abs(european.totals["carry_net"] - american.totals["carry_net"]) < 0.05
        verdict = ("Total carry is the same under both here; only its timing differs."
                   if same else "Total carry differs between the two methods as well as its timing.")  # fmt: skip
        _footnote(fig, f"LP-class distributions by calendar year. {verdict} Table: waterfall_comparison.csv")
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 4. Gross-to-net bridge
# --------------------------------------------------------------------------- #
def chart_gross_to_net(bridge: pd.DataFrame, path: Path) -> Path:
    steps = bridge.iloc[1:-1]
    steps = steps[steps["irr_impact"].abs() > 5e-6]  # hide steps with no effect (e.g. no subscription line)
    labels = ["Gross IRR", *steps["step"], "Net IRR"]
    gross, net = float(bridge["irr_after"].iloc[0]), float(bridge["irr_after"].iloc[-1])

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "Gross-to-net bridge: what fund costs take out of the return",
            f"From a {fmt_pct(gross)} gross IRR on the deals to a {fmt_pct(net)} net IRR for LPs",
            bottom=0.155,
        )
        n = len(labels)
        ax.set_xlim(-0.6, n - 0.4)
        ax.set_ylim(0, max(gross, net, float(bridge["irr_after"].max())) * 1.2)
        _grid(ax)
        thickness = _thickness(ax, 1.0, max_px=44, fill=0.5)
        offset = 0.012 * ax.get_ylim()[1]
        _bar(ax, 0.0, gross, 0, thickness, TOTAL)
        ax.text(0, gross + offset, fmt_pct(gross), ha="center", va="bottom", fontsize=10, fontweight="bold")
        level = gross
        for i, (_, row) in enumerate(steps.iterrows(), start=1):
            after = float(row["irr_after"])
            colour = RED if after < level else BLUE
            _bar(ax, level, after, i, thickness, colour)
            ax.plot(
                [i - 1 + thickness / 2, i - thickness / 2],
                [level, level],
                color=BASELINE,
                lw=1.0 * PT,
                zorder=REFERENCE_Z,
            )
            ax.text(i, max(level, after) + offset, fmt_bps(row["irr_impact_bps"]), ha="center", va="bottom",
                    fontsize=9.5, color=INK)  # fmt: skip
            level = after
        ax.plot(
            [n - 2 + thickness / 2, n - 1 - thickness / 2],
            [level, level],
            color=BASELINE,
            lw=1.0 * PT,
            zorder=REFERENCE_Z,
        )
        _bar(ax, 0.0, net, n - 1, thickness, TOTAL)
        ax.text(n - 1, net + offset, fmt_pct(net), ha="center", va="bottom", fontsize=10, fontweight="bold")
        ax.set_xticks(range(n))
        ax.set_xticklabels([label.replace(" ", "\n", 1) if len(label) > 12 else label for label in labels])
        ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
        entries = [("Return level", TOTAL, "bar"), ("Reduces the LP return", RED, "bar")]
        if (steps["irr_impact"] > 0).any():
            entries.append(("Adds to the LP return", BLUE, "bar"))
        _legend(fig, entries)
        _footnote(
            fig, "Each step re-measures the LPs' IRR with one more cost switched on. Table: gross_to_net_bridge.csv"
        )
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 5. Split of gross profit
# --------------------------------------------------------------------------- #
def chart_value_split(split: pd.DataFrame, result: FundResult, path: Path) -> Path:
    currency, unit = _currency_symbol(result)
    data = split.sort_values("amount", ascending=True)
    gross_profit = split.attrs["gross_profit"]

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "Where each dollar of gross profit goes",
            f"Split of the fund's {fmt_money(gross_profit, currency, unit)} gross profit between LPs, the GP and costs",
            left=0.215,
        )
        ax.set_xlim(0, float(data["amount"].max()) * 1.3)
        ax.set_ylim(-0.6, len(data) - 0.4)
        _grid(ax, "x")
        ax.spines["bottom"].set_visible(False)
        ax.spines["left"].set_visible(True)
        thickness = _thickness(ax, 1.0, horizontal=True)
        for position, (_, row) in enumerate(data.iterrows()):
            _bar(ax, 0.0, float(row["amount"]), position, thickness, BLUE, horizontal=True)
            ax.text(float(row["amount"]), position,
                    f"  {fmt_money(row['amount'], currency, unit, 1)}  ({fmt_pct(row['share_of_gross_profit'])})",
                    va="center", ha="left", fontsize=9.5, color=INK, bbox=LABEL_BOX)  # fmt: skip
        ax.set_yticks(range(len(data)))
        ax.set_yticklabels([f"{c}\n({r})" for c, r in zip(data["component"], data["recipient"], strict=True)],
                           fontsize=9.5, linespacing=1.4)  # fmt: skip
        ax.xaxis.set_major_formatter(lambda v, _: fmt_money(v, currency, unit))
        _footnote(
            fig, "Carried interest is net of any clawback. The five lines sum to gross profit. Table: value_split.csv"
        )
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 6. Two-way sensitivity heatmap
# --------------------------------------------------------------------------- #
def chart_sensitivity(grid: pd.DataFrame, path: Path, title: str | None = None, currency: str = "$",
                      money_unit: str = "m") -> Path:  # fmt: skip
    attrs = grid.attrs
    unit = attrs.get("metric_unit", "pct")
    values = grid.to_numpy(dtype=float)
    cmap = LinearSegmentedColormap.from_list("sequential_blue", SEQUENTIAL)

    with plt.rc_context(_style()):
        fig, ax = _figure(
            title or f"{attrs.get('metric_label', 'Metric')} sensitivity",
            f"{attrs.get('metric_label')} for each combination of {_lower_first(attrs.get('x_label', ''))} "
            f"and {_lower_first(attrs.get('y_label', ''))}",
            left=0.16, top=0.80, bottom=0.17,
        )  # fmt: skip
        finite = values[np.isfinite(values)]
        vmin, vmax = (float(finite.min()), float(finite.max())) if len(finite) else (0.0, 1.0)
        if vmax - vmin < 1e-12:
            vmax = vmin + 1e-9
        rows, cols = values.shape
        for i in range(rows):
            for j in range(cols):
                value = values[i, j]
                shade = (value - vmin) / (vmax - vmin) if np.isfinite(value) else 0.0
                # 2px surface gap between cells instead of borders.
                ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, facecolor=cmap(shade), edgecolor=SURFACE,
                                           linewidth=2.0 * PT))  # fmt: skip
                is_base = np.isclose(grid.columns[j], attrs.get("x_base", np.nan)) and np.isclose(
                    grid.index[i], attrs.get("y_base", np.nan)
                )
                ax.text(j, i, _format_value(value, unit, currency, money_unit), ha="center", va="center",
                        fontsize=9.5, color="#ffffff" if shade > 0.45 else INK,
                        fontweight="bold" if is_base else "normal")  # fmt: skip
                if is_base:
                    ax.text(j, i + 0.3, "base", ha="center", va="center", fontsize=7.5,
                            color="#ffffff" if shade > 0.45 else INK_SECONDARY)  # fmt: skip
        ax.set_xlim(-0.5, cols - 0.5)
        ax.set_ylim(rows - 0.5, -0.5)
        ax.spines["bottom"].set_visible(False)
        ax.set_xticks(range(cols))
        ax.set_xticklabels(
            _axis_values(grid.columns, attrs.get("x_unit", "number"), attrs.get("x") in SIGNED_PARAMETERS)
        )
        ax.set_yticks(range(rows))
        ax.set_yticklabels(_axis_values(grid.index, attrs.get("y_unit", "number"), attrs.get("y") in SIGNED_PARAMETERS))
        ax.set_xlabel(attrs.get("x_label", ""), labelpad=8)
        ax.set_ylabel(attrs.get("y_label", ""), labelpad=8)
        ax.tick_params(labelsize=9.5)
        _footnote(
            fig,
            "Every cell is a full re-run of the fund model. Darker = higher. Table: sensitivity CSV of the same name",
        )
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 7. Tornado
# --------------------------------------------------------------------------- #
def chart_tornado(tornado: pd.DataFrame, path: Path) -> Path:
    attrs = tornado.attrs
    unit = attrs.get("metric_unit", "pct")
    scale = 1e4 if unit == "pct" else 1.0
    data = tornado.iloc[::-1].reset_index(drop=True)

    with plt.rc_context(_style()):
        fig, ax = _figure(
            f"What moves {_lower_first(attrs.get('metric_label', 'the result'))} most",
            f"Change from the {_format_value(attrs.get('base', np.nan), unit)} base case when one assumption "
            "moves at a time",
            left=0.30,
        )  # fmt: skip
        extent = float(np.nanmax(np.abs(data[["low_delta", "high_delta"]].to_numpy()))) * scale
        ax.set_xlim(-extent * 1.45, extent * 1.45)
        ax.set_ylim(-0.6, len(data) - 0.4)
        _grid(ax, "x")
        ax.spines["bottom"].set_visible(False)
        thickness = _thickness(ax, 1.0, horizontal=True)
        labels = []
        for position, row in data.iterrows():
            for delta, result_value in ((row["low_delta"], row["low_result"]), (row["high_delta"], row["high_result"])):
                value = delta * scale
                if abs(value) < 1e-9:
                    continue
                _bar(ax, 0.0, value, position, thickness, BLUE if value > 0 else RED, horizontal=True)
                ax.text(value, position, f"  {_format_value(result_value, unit)}  ", va="center",
                        ha="left" if value > 0 else "right", fontsize=9, color=INK, bbox=LABEL_BOX)  # fmt: skip
            signed = row["parameter"] in SIGNED_PARAMETERS
            labels.append(
                f"{row['label']}\n{_format_input(row['low_input'], row['unit'], signed)} / "
                f"{_format_input(row['high_input'], row['unit'], signed)}"
            )
        ax.axvline(0.0, color=INK_MUTED, lw=1.0 * PT, zorder=LINE_Z)
        ax.set_yticks(range(len(data)))
        ax.set_yticklabels(labels, fontsize=9.5, linespacing=1.4)
        ax.xaxis.set_major_formatter(
            lambda v, _: "0" if abs(v) < 1e-9 else (f"{v:+,.0f} bps" if unit == "pct" else f"{v:+,.2f}")
        )
        _legend(fig, [("Lowers the result", RED, "bar"), ("Raises the result", BLUE, "bar")])
        _footnote(fig, "Bar labels show the resulting value; the two inputs tested are listed under each assumption. "
                       "Table: tornado.csv")  # fmt: skip
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 8. Carry timing under the two waterfalls
# --------------------------------------------------------------------------- #
def chart_carry_timing(timeline: pd.DataFrame, result: FundResult, path: Path) -> Path:
    currency, unit = _currency_symbol(result)
    x = timeline["date"].dt.year + timeline["date"].dt.month / 12.0
    european, american = timeline["european_cumulative_carry"], timeline["american_cumulative_carry"]

    with plt.rc_context(_style()):
        top = float(max(european.max(), american.max()))
        same = abs(float(european.iloc[-1]) - float(american.iloc[-1])) < 0.06 * max(top, 1e-9)
        fig, ax = _figure(
            "Carry timing: the same carry, years apart" if same else "Carry timing: deal-by-deal pays the GP earlier",
            "Cumulative carried interest paid to the GP under each waterfall",
            right=0.90,
        )
        ax.set_xlim(float(x.min()), float(x.max()) + 0.1)
        ax.set_ylim(0, top * 1.14)
        _grid(ax)
        ax.step(x, american, where="post", color=ORANGE, zorder=LINE_Z)
        ax.step(x, european, where="post", color=BLUE, zorder=LINE_Z)
        ax.fill_between(x, european, american, step="post", color=ORANGE, alpha=0.10, lw=0, zorder=2)
        x_end = float(x.iloc[-1])
        ends = [(float(american.iloc[-1]), ORANGE, "Deal-by-deal"), (float(european.iloc[-1]), BLUE, "Whole-of-fund")]
        for value, colour, _ in ends:
            ax.plot(x_end, value, "o", ms=8 * PT, color=colour, mec=SURFACE, mew=2 * PT, zorder=MARKER_Z, clip_on=False)
        if abs(ends[0][0] - ends[1][0]) < 0.06 * top:
            # Both lines finish together: one shared label instead of two that collide.
            ax.annotate(f"Both end at\n{fmt_money(ends[1][0], currency, unit, 1)}", (x_end, ends[1][0]), xytext=(9, 0),
                        textcoords="offset points", fontsize=9, color=INK_SECONDARY, va="center",
                        annotation_clip=False)  # fmt: skip
        else:
            for value, _, label in ends:
                ax.annotate(f"{label}\n{fmt_money(value, currency, unit, 1)}", (x_end, value), xytext=(9, 0),
                            textcoords="offset points", fontsize=9, color=INK_SECONDARY, va="center",
                            annotation_clip=False)  # fmt: skip
        firsts = ((american, "First carry, deal-by-deal", (-12, 26), "right"),
                  (european, "First carry, whole-of-fund", (14, -46), "left"))  # fmt: skip
        for values, label, offset, align in firsts:
            paid = np.nonzero(values.to_numpy() > 1e-9)[0]
            if len(paid):
                q = int(paid[0])
                ax.annotate(f"{label}\n{timeline['date'].iloc[q]:%b %Y}", (float(x.iloc[q]), float(values.iloc[q])),
                            xytext=offset, textcoords="offset points", ha=align, fontsize=9, color=INK_SECONDARY,
                            bbox=LABEL_BOX, arrowprops={"arrowstyle": "-", "color": INK_MUTED, "lw": 1.0 * PT})  # fmt: skip
        years = sorted(set(timeline["date"].dt.year))
        ax.set_xticks([y + 1 for y in years[:-1]])
        ax.set_xticklabels([f"Dec {y}" for y in years[:-1]], fontsize=8.5)
        ax.yaxis.set_major_formatter(lambda v, _: fmt_money(v, currency, unit))
        _legend(fig, [("Deal-by-deal (American)", ORANGE, "line"), ("Whole-of-fund (European)", BLUE, "line")])
        _footnote(fig, "Shaded area: carry the GP holds earlier under deal-by-deal, which is what a clawback has to recover. "
                       "Table: carry_timeline.csv")  # fmt: skip
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 9. Monte Carlo distribution
# --------------------------------------------------------------------------- #
def chart_monte_carlo(sims: pd.DataFrame, stats: dict[str, float], pref_rate: float, path: Path) -> Path:
    irr = sims["net_irr"].fillna(-1.0).to_numpy() * 100.0
    low, high = np.floor(np.percentile(irr, 1.0)), np.ceil(np.percentile(irr, 99.0))
    width = 1.0 if (high - low) <= 36 else 2.0
    edges = np.arange(low, high + width, width)
    counts, _ = np.histogram(np.clip(irr, low, high - 1e-9), bins=edges)
    share = counts / counts.sum()

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "Monte Carlo: distribution of LP net IRR",
            f"{stats['n_sims']:,} simulated portfolios; exit multiples, write-offs and holding periods redrawn each time",
            right=0.70, bottom=0.165,
        )  # fmt: skip
        ax.set_xlim(low - width, high + width)
        ax.set_ylim(0, float(share.max()) * 1.32)
        _grid(ax)
        thickness = _thickness(ax, width, fill=0.86)
        for left_edge, value in zip(edges[:-1], share, strict=True):
            _bar(ax, 0.0, float(value), left_edge + width / 2, thickness, BLUE, radius_px=3.0)
        top = ax.get_ylim()[1]
        markers = [
            (pref_rate * 100.0, f"Hurdle {pref_rate:.0%}", "right"),
            (stats["net_irr_median"] * 100.0, f"Median {stats['net_irr_median']:.1%}", "left"),
        ]
        for value, label, align in markers:
            ax.plot([value, value], [0, top * 0.93], color=INK_SECONDARY, lw=1.0 * PT, zorder=LINE_Z)
            ax.text(value, top * 0.97, label, ha=align, va="center", fontsize=9, color=INK, bbox=LABEL_BOX)
        ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
        ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
        ax.set_xlabel("LP net IRR", labelpad=6)
        ax.set_ylabel("Share of trials", labelpad=8)

        # Key figures beside the plot, so no text sits on top of the data.
        tiles = [
            (f"{stats['net_irr_p5']:.1%} to {stats['net_irr_p95']:.1%}", "5th to 95th percentile of net IRR"),
            (f"{stats['prob_net_irr_below_pref']:.1%}", "of trials miss the hurdle,\nso no carry is paid"),
            (f"{stats['prob_capital_loss']:.1%}", "of trials lose LP capital\n(net TVPI below 1.0x)"),
            (f"{stats['prob_clawback_american']:.1%}", "of trials need a material\nclawback under deal-by-deal"),
        ]
        y = 0.735
        for value, label in tiles:
            fig.text(0.735, y, value, fontsize=13, fontweight="bold", color=INK, va="top")
            fig.text(0.735, y - 0.052, label, fontsize=9, color=INK_SECONDARY, va="top", linespacing=1.4)
            y -= 0.148 if "\n" in label else 0.118
        _footnote(fig, "Whole-of-fund waterfall. Trials bunch just above the hurdle, where the GP catch-up absorbs every "
                       "extra dollar. End bars hold the outer 1% tails.")  # fmt: skip
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 10. Deal outcomes
# --------------------------------------------------------------------------- #
def chart_deal_moic(deals: pd.DataFrame, path: Path) -> Path:
    data = deals.sort_values("gross_moic", ascending=True).reset_index(drop=True)

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "Deal outcomes: gross multiple of invested capital",
            "Each deal's gross MOIC, with gross IRR and holding period",
            left=0.19,
        )
        ax.set_xlim(0, float(data["gross_moic"].max()) * 1.38)
        ax.set_ylim(-0.6, len(data) - 0.4)
        _grid(ax, "x")
        ax.spines["bottom"].set_visible(False)
        thickness = _thickness(ax, 1.0, horizontal=True, max_px=18)
        for position, row in data.iterrows():
            _bar(ax, 0.0, float(row["gross_moic"]), position, thickness, BLUE, horizontal=True)
            irr = f"{fmt_pct(row['gross_irr'])} IRR" if np.isfinite(row["gross_irr"]) else "written off"
            ax.text(float(row["gross_moic"]), position, f"  {fmt_multiple(row['gross_moic'])}  ·  {irr}  ·  "
                    f"{row['hold_years']:.1f} yrs", va="center", ha="left", fontsize=9, color=INK,
                    bbox=LABEL_BOX, zorder=MARKER_Z)  # fmt: skip
        ax.axvline(1.0, color=INK_MUTED, lw=1.0 * PT, zorder=LINE_Z)
        ax.text(1.0, len(data) - 0.42, " 1.0x = cost", fontsize=8.5, color=INK_MUTED, va="bottom", ha="left")
        ax.set_yticks(range(len(data)))
        ax.set_yticklabels(data["deal"], fontsize=9.5)
        ax.xaxis.set_major_formatter(lambda v, _: f"{v:.1f}x")
        _footnote(fig, "Gross of fund-level fees, expenses and carried interest. Table: deals.csv")
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 11. Value-creation bridge
# --------------------------------------------------------------------------- #
def chart_value_creation(drivers: pd.DataFrame, result: FundResult, path: Path) -> Path:
    currency, unit = _currency_symbol(result)
    data = drivers[drivers["amount"].abs() > 1e-9].reset_index(drop=True)
    total = drivers.attrs["total_gain"]
    labels = [*data["driver"], "Total gain"]

    with plt.rc_context(_style()):
        fig, ax = _figure(
            "Value creation: what drove the portfolio's gain",
            f"Build-up of the {fmt_money(total, currency, unit)} gross gain across all deals",
            bottom=0.155,
        )
        n = len(labels)
        running = np.concatenate([[0.0], np.cumsum(data["amount"].to_numpy())])
        ax.set_xlim(-0.6, n - 0.4)
        ax.set_ylim(min(0.0, float(running.min())) * 1.1, max(float(running.max()), total) * 1.16)
        _grid(ax)
        thickness = _thickness(ax, 1.0, max_px=44, fill=0.5)
        offset = 0.012 * (ax.get_ylim()[1] - ax.get_ylim()[0])
        for i, row in data.iterrows():
            lo, hi = running[i], running[i + 1]
            _bar(ax, lo, hi, i, thickness, BLUE if hi >= lo else RED)
            ax.text(i, max(lo, hi) + offset, f"{fmt_money(row['amount'], currency, unit)}\n{fmt_pct(row['share_of_gain'], 0)}",
                    ha="center", va="bottom", fontsize=9, color=INK, linespacing=1.35)  # fmt: skip
            ax.plot(
                [i + thickness / 2, i + 1 - thickness / 2], [hi, hi], color=BASELINE, lw=1.0 * PT, zorder=REFERENCE_Z
            )
        _bar(ax, 0.0, total, n - 1, thickness, TOTAL)
        ax.text(n - 1, total + offset, fmt_money(total, currency, unit), ha="center", va="bottom", fontsize=10,
                fontweight="bold")  # fmt: skip
        ax.axhline(0.0, color=BASELINE, lw=1.0 * PT, zorder=REFERENCE_Z)
        ax.set_xticks(range(n))
        wrapped = {
            "EBITDA growth": "EBITDA\ngrowth",
            "Multiple expansion / (contraction)": "Multiple\nexpansion",
            "Combined growth x multiple": "Growth x\nmultiple",
            "Deleveraging (debt paydown)": "Debt\npaydown",
            "Equity floor (limited liability)": "Equity\nfloor",
            "Direct-MOIC positions": "Growth-equity\npositions",
        }
        ax.set_xticklabels([wrapped.get(label, label.replace(" ", "\n", 1)) for label in labels], linespacing=1.3)
        ax.yaxis.set_major_formatter(lambda v, _: fmt_money(v, currency, unit))
        entries = [("Adds value", BLUE, "bar"), ("Total", TOTAL, "bar")]
        if (data["amount"] < 0).any():
            entries.insert(1, ("Destroys value", RED, "bar"))
        _legend(fig, entries)
        _footnote(fig, "Operating bridge for buyout deals; growth-equity positions modelled on a direct multiple are one line. "
                       "Table: value_creation.csv")  # fmt: skip
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# 12. Scenario comparison
# --------------------------------------------------------------------------- #
def chart_scenarios(table: pd.DataFrame, pref_rate: float, path: Path) -> Path:
    data = table.iloc[::-1].reset_index(drop=True)
    names = list(data["label"]) if "label" in data else [name.replace("_", " ") for name in data["scenario"]]

    with plt.rc_context(_style()):
        fig, axes = _figure(
            "Scenarios: LP outcomes under different markets and terms",
            "Net IRR and net TVPI at liquidation for each named scenario",
            ncols=2, left=0.235, wspace=0.16, sharey=True, figsize=(9.6, 5.8), top=0.80, bottom=0.11,
        )  # fmt: skip
        panels = [
            ("net_irr", "Net IRR", lambda v: fmt_pct(v), pref_rate, f"Hurdle {pref_rate:.0%}"),
            ("tvpi", "Net TVPI", lambda v: fmt_multiple(v), 1.0, "1.0x"),
        ]
        for ax, (column, label, formatter, reference, reference_label) in zip(axes, panels, strict=True):
            values = data[column].to_numpy(dtype=float)
            ax.set_xlim(0, float(np.nanmax(values)) * 1.3)
            ax.set_ylim(-0.6, len(data) - 0.4)
            _grid(ax, "x")
            ax.spines["bottom"].set_visible(False)
            thickness = _thickness(ax, 1.0, horizontal=True, max_px=16)
            for position, value in enumerate(values):
                is_base = position == len(values) - 1
                _bar(ax, 0.0, float(value), position, thickness, BLUE if is_base else NEUTRAL, horizontal=True)
                ax.text(float(value), position, f"  {formatter(value)}", va="center", ha="left", fontsize=9, color=INK,
                        fontweight="bold" if is_base else "normal", bbox=LABEL_BOX, zorder=MARKER_Z)  # fmt: skip
            ax.axvline(reference, color=INK_MUTED, lw=1.0 * PT, zorder=LINE_Z)
            ax.text(reference, len(data) - 0.42, f" {reference_label}", fontsize=8.5, color=INK_MUTED, va="bottom")
            ax.set_title(label, loc="left", fontsize=10, color=INK_SECONDARY, pad=16)
            ax.set_yticks(range(len(data)))
            ax.set_yticklabels(names, fontsize=9.5)
            if column == "net_irr":
                ax.xaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
            else:
                ax.xaxis.set_major_formatter(lambda v, _: f"{v:.1f}x")
        _footnote(fig, "Base case highlighted. Each scenario is a full re-run of the fund model. Table: scenarios.csv")
        return _save(fig, path)


# --------------------------------------------------------------------------- #
# Convenience: render the full set
# --------------------------------------------------------------------------- #
def render_all(bundle: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    """Render every chart from a pipeline bundle (see ``pefund.pipeline.build_bundle``)."""
    out = Path(out_dir)
    result: FundResult = bundle["result"]
    currency, unit = _currency_symbol(result)
    paths = {
        "j_curve": chart_j_curve(result, out / "j_curve.png"),
        "tvpi_build": chart_tvpi_build(result, out / "tvpi_build.png"),
        "waterfall_tiers": chart_waterfall_tiers(
            bundle["comparison"]["runs"]["european"],
            bundle["comparison"]["runs"]["american"],
            out / "waterfall_tiers.png",
        ),  # fmt: skip
        "gross_to_net": chart_gross_to_net(bundle["bridge"], out / "gross_to_net_bridge.png"),
        "value_split": chart_value_split(bundle["value_split"], result, out / "value_split.png"),
        "carry_timing": chart_carry_timing(bundle["comparison"]["timeline"], result, out / "carry_timing.png"),
        "deal_moic": chart_deal_moic(result.deals, out / "deal_moic.png"),
        "value_creation": chart_value_creation(bundle["value_creation"], result, out / "value_creation.png"),
    }
    if bundle.get("scenario_table") is not None:
        paths["scenarios"] = chart_scenarios(bundle["scenario_table"], result.config.waterfall.pref_rate,
                                             out / "scenarios.png")  # fmt: skip
    if bundle.get("tornado") is not None:
        paths["tornado"] = chart_tornado(bundle["tornado"], out / "tornado.png")
    titles = {
        "exit_multiple_x_holding_period__net_irr": "Net IRR: exit multiples against holding period",
        "exit_multiple_x_holding_period__carry_net": "Carried interest: exit multiples against holding period",
        "carry_x_preferred_return__net_irr": "Net IRR: carry rate against preferred return",
        "management_fee_x_carry__net_irr": "Net IRR: management fee against carry rate",
        "catch_up_x_preferred_return__carry_net": "Carried interest: catch-up against preferred return",
        "exit_multiple_x_ebitda_growth__net_irr": "Net IRR: exit multiples against EBITDA growth",
    }
    for key, title in titles.items():
        grid = (bundle.get("grids") or {}).get(key)
        if grid is not None:
            paths[f"sensitivity_{key}"] = chart_sensitivity(grid, out / f"sensitivity_{key}.png", title, currency, unit)
    if bundle.get("monte_carlo") is not None:
        paths["monte_carlo"] = chart_monte_carlo(
            bundle["monte_carlo"], bundle["monte_carlo_stats"], result.config.waterfall.pref_rate,
            out / "monte_carlo_net_irr.png",
        )  # fmt: skip
    return paths
