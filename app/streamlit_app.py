"""Interactive dashboard for the fund model.

Run from the repository root::

    streamlit run app/streamlit_app.py

Every control re-runs the full fund model, so what you see is the engine's
answer for the terms and shocks in the sidebar, not an approximation.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # allow running without `pip install -e .`
    sys.path.insert(0, str(ROOT / "src"))

from pefund import FundConfig, load_config  # noqa: E402
from pefund import scenarios as sc  # noqa: E402
from pefund.ai.commentary import answer_question, generate_commentary  # noqa: E402
from pefund.ai.llm import LLMError, detect_provider  # noqa: E402
from pefund.ai.narrator import explain_distribution  # noqa: E402
from pefund.config import ConfigError  # noqa: E402
from pefund.montecarlo import MonteCarloSettings, run_monte_carlo, summarize_monte_carlo  # noqa: E402
from pefund.pipeline import build_bundle  # noqa: E402

st.set_page_config(page_title="PE fund waterfall and carry analytics", layout="wide")

# --------------------------------------------------------------------------- #
# Look and feel: one validated palette, chosen per theme, used by every chart
# --------------------------------------------------------------------------- #
try:
    DARK = st.context.theme.type == "dark"
except Exception:  # older Streamlit, or running outside a session
    DARK = False
BLUE, ORANGE, AQUA = ("#3987e5", "#d95926", "#199e70") if DARK else ("#2a78d6", "#eb6834", "#1baf7a")
RED = "#e66767" if DARK else "#e34948"
NEUTRAL = "#6f6e68" if DARK else "#c3c2b7"
TOTAL = "#c3c2b7" if DARK else "#52514e"
GRID = "#2c2c2a" if DARK else "#e1e0d9"
SURFACE = "#0e1117" if DARK else "#ffffff"  # page background; drawn as a thin gap between touching bars
SEQUENTIAL = (["#0d366b", "#184f95", "#256abf", "#3987e5", "#6da7ec"] if DARK
              else ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"])  # fmt: skip
STRETCH = (
    {"width": "stretch"} if "width" in inspect.signature(st.plotly_chart).parameters else {"use_container_width": True}
)


def style(fig: go.Figure, title: str, height: int = 380, y_format: str | None = None, x_format: str | None = None,
          legend: bool = True, bar_width: float | None = 0.42) -> go.Figure:  # fmt: skip
    """Shared chart chrome: thin marks, hairline grid, legend above the plot, one axis."""
    fig.update_layout(
        title={"text": title, "x": 0, "xanchor": "left", "font": {"size": 15}},
        height=height, margin={"l": 10, "r": 10, "t": 70 if legend else 50, "b": 10},
        legend={"orientation": "h", "y": 1.02, "yanchor": "bottom", "x": 0, "xanchor": "left", "traceorder": "normal"},
        showlegend=legend, barcornerradius=4, hoverlabel={"namelength": -1},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    )  # fmt: skip
    # Slim bars with a thin surface-coloured gap between stacked segments.
    fig.update_traces(selector={"type": "bar"}, marker_line_color=SURFACE, marker_line_width=1.5)
    if bar_width is not None:
        fig.update_traces(selector={"type": "bar"}, width=bar_width)
        fig.update_traces(selector={"type": "waterfall"}, width=bar_width)
    # Gridlines follow the value axis: horizontal bars are read against vertical lines.
    horizontal = any(getattr(trace, "orientation", None) == "h" for trace in fig.data)
    fig.update_xaxes(showgrid=horizontal, gridcolor=GRID, gridwidth=1, tickformat=x_format, zeroline=False, tickangle=0)
    fig.update_yaxes(showgrid=not horizontal, gridcolor=GRID, gridwidth=1, tickformat=y_format, zeroline=not horizontal,
                     zerolinecolor=GRID)  # fmt: skip
    return fig


def show(fig: go.Figure) -> None:
    st.plotly_chart(fig, theme="streamlit", **STRETCH)


def table(frame: pd.DataFrame, formats: dict | None = None) -> None:
    """Show a table with values formatted as text, so blanks read as a dash rather than 'None'."""
    shown = frame.copy()
    for column in shown.columns:
        formatter = (formats or {}).get(column)

        def render(value, formatter=formatter):
            if value is None or (isinstance(value, float) and np.isnan(value)):
                return "-"
            if formatter is None:
                return value
            return formatter(value) if callable(formatter) else formatter.format(value)

        shown[column] = shown[column].map(render)
    st.dataframe(shown, hide_index=True, **STRETCH)


def change(value: float, text: str) -> str | None:
    """Delta label for a headline tile; nothing when the case is unchanged."""
    return None if abs(value) < 1e-9 else text


def wrap(label: str) -> str:
    """Break a long axis label over two lines instead of letting it rotate."""
    short = {
        "Multiple expansion / (contraction)": "Multiple<br>expansion", "Combined growth x multiple": "Growth x<br>multiple",
        "Deleveraging (debt paydown)": "Debt<br>paydown", "Equity floor (limited liability)": "Equity<br>floor",
        "Direct-MOIC positions": "Growth-equity<br>positions", "EBITDA growth": "EBITDA<br>growth",
    }  # fmt: skip
    return short.get(label, label.replace(" ", "<br>", 1) if len(label) > 10 else label)


# --------------------------------------------------------------------------- #
# Cached model runs (keyed by the configuration, so repeat views are instant)
# --------------------------------------------------------------------------- #
@st.cache_resource(max_entries=64, show_spinner=False)
def cached_bundle(config_json: str, scenario_path: str) -> dict:
    cfg = FundConfig.from_dict(json.loads(config_json))
    path = Path(scenario_path)
    scenarios = sc.load_scenarios(path) if path.is_file() else None
    return build_bundle(cfg, scenarios, sensitivities=False, monte_carlo=False)


@st.cache_resource(max_entries=16, show_spinner=False)
def cached_grid(config_json: str, x_name: str, y_name: str, metric: str) -> pd.DataFrame:
    cfg = FundConfig.from_dict(json.loads(config_json))
    axes = {
        "exit_multiple": [-0.30, -0.20, -0.10, 0.0, 0.10, 0.20, 0.30],
        "hold_shift": [-4, -2, 0, 2, 4, 6, 8],
        "ebitda_growth": [-0.04, -0.02, 0.0, 0.02, 0.04],
        "carry_rate": [0.10, 0.15, 0.20, 0.25, 0.30],
        "pref_rate": [0.0, 0.06, 0.08, 0.10, 0.12],
        "catch_up_rate": [0.0, 0.5, 0.8, 1.0],
        "fee_rate": [0.010, 0.015, 0.020, 0.025],
    }
    return sc.sensitivity_grid(cfg, (x_name, axes[x_name]), (y_name, axes[y_name]), metric)


@st.cache_resource(max_entries=8, show_spinner=False)
def cached_tornado(config_json: str) -> pd.DataFrame:
    return sc.tornado(FundConfig.from_dict(json.loads(config_json)))


@st.cache_resource(max_entries=8, show_spinner=False)
def cached_monte_carlo(config_json: str, n_sims: int, seed: int) -> pd.DataFrame:
    cfg = FundConfig.from_dict(json.loads(config_json))
    return run_monte_carlo(cfg, MonteCarloSettings(n_sims=n_sims, seed=seed))


# --------------------------------------------------------------------------- #
# Sidebar: choose the fund, then flex its terms and the market
# --------------------------------------------------------------------------- #
st.sidebar.title("Fund model")
uploaded = st.sidebar.file_uploader("Configuration (.yaml or the Excel model)", type=["yaml", "yml", "xlsx"])
try:
    if uploaded is not None:
        with tempfile.NamedTemporaryFile(suffix=Path(uploaded.name).suffix, delete=False) as handle:
            handle.write(uploaded.getvalue())
        base = load_config(handle.name)
    else:
        base = load_config(ROOT / "configs" / "base_case.yaml")
except ConfigError as exc:
    st.error(f"That configuration could not be loaded: {exc}")
    st.stop()

st.sidebar.subheader("Waterfall terms")
wf_type = st.sidebar.radio(
    "Waterfall", ["european", "american"], index=0 if base.waterfall.type == "european" else 1, horizontal=True,
    format_func=lambda v: "Whole-of-fund" if v == "european" else "Deal-by-deal",
)  # fmt: skip
pref = st.sidebar.slider("Preferred return (%)", 0.0, 15.0, base.waterfall.pref_rate * 100, 0.5)
carry = st.sidebar.slider("Carried interest (%)", 0.0, 30.0, base.waterfall.carry_rate * 100, 1.0)
catch_up_choices = [0, 50, 80, 100]
base_catch_up = int(round(base.waterfall.catch_up_rate * 100))
catch_up = st.sidebar.select_slider(
    "GP catch-up (%)", options=catch_up_choices,
    value=base_catch_up if base_catch_up in catch_up_choices else 100,
)  # fmt: skip
compounding = st.sidebar.selectbox(
    "Pref compounding", ["annual", "quarterly", "simple"],
    index=["annual", "quarterly", "simple"].index(base.waterfall.pref_compounding),
)  # fmt: skip
st.sidebar.subheader("Fees and protections")
fee = st.sidebar.slider(
    "Management fee, investment period (%)", 0.0, 3.0, base.management_fee.rate_investment_period * 100, 0.25
)
escrow = st.sidebar.slider("Carry escrow (%)", 0, 50, int(round(base.clawback.escrow_pct * 100)), 5)
after_tax = st.sidebar.checkbox("Limit clawback to after-tax carry", base.clawback.after_tax_limit)
line = st.sidebar.checkbox("Subscription line", base.subscription_line.enabled)
st.sidebar.subheader("Market shocks")
exit_shock = st.sidebar.slider("Exit multiples (% change)", -60, 60, int(round(base.shocks.exit_multiple_pct * 100)), 5)
growth_shock = st.sidebar.slider("EBITDA growth (points)", -6.0, 6.0, base.shocks.ebitda_growth_shift * 100, 0.5)
hold_shock = st.sidebar.slider("Holding periods (quarters)", -4, 8, base.shocks.hold_shift_quarters, 1)

if catch_up != 0 and catch_up <= carry:
    st.sidebar.warning("A catch-up at or below the carry rate can never complete; using no catch-up instead.")
    catch_up = 0
step_down = base.management_fee.rate_investment_period - base.management_fee.rate_post_investment_period
overrides = {
    "waterfall.type": wf_type, "waterfall.pref_rate": pref / 100, "waterfall.carry_rate": carry / 100,
    "waterfall.catch_up_rate": catch_up / 100, "waterfall.pref_compounding": compounding,
    "management_fee.rate_investment_period": fee / 100,
    "management_fee.rate_post_investment_period": max(fee / 100 - step_down, 0.0),
    "clawback.escrow_pct": escrow / 100, "clawback.after_tax_limit": after_tax,
    "subscription_line.enabled": line, "shocks.exit_multiple_pct": exit_shock / 100,
    "shocks.ebitda_growth_shift": growth_shock / 100, "shocks.hold_shift_quarters": hold_shock,
}  # fmt: skip
try:
    cfg = base.with_values(overrides)
    config_json = json.dumps(cfg.to_dict(), sort_keys=True)
    base_json = json.dumps(base.to_dict(), sort_keys=True)
    scenario_path = str(ROOT / "configs" / "scenarios.yaml")
    with st.spinner("Running the fund model ..."):
        bundle = cached_bundle(config_json, scenario_path)
        reference = cached_bundle(base_json, scenario_path)
except ConfigError as exc:
    st.error(f"These settings are not a valid fund: {exc}")
    st.stop()

result, summary, valuation = bundle["result"], bundle["summary"], bundle["valuation"]
unit = f"{cfg.fund.currency} {cfg.fund.unit}"
symbol = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(cfg.fund.currency.upper(), "")
money_fmt = "{:,.1f}"

# --------------------------------------------------------------------------- #
# Header and headline figures
# --------------------------------------------------------------------------- #
st.title("Private equity fund waterfall and carry analytics")
st.caption(
    f"{cfg.fund.name} · {'whole-of-fund' if wf_type == 'european' else 'deal-by-deal'} waterfall · money in {unit} · "
    "hypothetical fund, for illustration only"
)
ref = reference["summary"]
tiles = st.columns(6)
d_irr, d_tvpi = summary["net_irr"] - ref["net_irr"], summary["tvpi"] - ref["tvpi"]
d_gross, d_moic = summary["gross_irr"] - ref["gross_irr"], summary["gross_moic"] - ref["gross_moic"]
d_carry = summary["carry_net"] - ref["carry_net"]
d_fees = summary["total_management_fees"] - ref["total_management_fees"]
tiles[0].metric("Net IRR", f"{summary['net_irr']:.1%}", change(d_irr, f"{d_irr * 1e4:+,.0f} bps"))
tiles[1].metric("Net TVPI", f"{summary['tvpi']:.2f}x", change(d_tvpi, f"{d_tvpi:+.2f}x"))
tiles[2].metric("Gross IRR", f"{summary['gross_irr']:.1%}", change(d_gross, f"{d_gross * 1e4:+,.0f} bps"))
tiles[3].metric("Gross MOIC", f"{summary['gross_moic']:.2f}x", change(d_moic, f"{d_moic:+.2f}x"))
tiles[4].metric("Carry, net of clawback", f"{symbol}{summary['carry_net']:,.1f}{cfg.fund.unit}",
                change(d_carry, f"{d_carry:+,.1f}"), delta_color="off")  # fmt: skip
tiles[5].metric("Management fees", f"{symbol}{summary['total_management_fees']:,.1f}{cfg.fund.unit}",
                change(d_fees, f"{d_fees:+,.1f}"), delta_color="off")  # fmt: skip
st.caption("Changes are measured against the configuration as loaded, before any sidebar adjustment.")
for flag in result.flags:
    st.info(flag)

tabs = st.tabs(["Overview", "Waterfall", "Deals", "Gross to net", "Scenarios", "Sensitivity", "Monte Carlo",
                "AI commentary", "Export"])  # fmt: skip
quarterly = result.quarterly
annual = bundle["annual"]

# ---- Overview ---------------------------------------------------------------
with tabs[0]:
    left, right = st.columns(2)
    with left:
        fig = go.Figure()
        fig.add_bar(x=annual["year"], y=-annual["lp_contributions"], name="Capital called", marker_color=ORANGE,
                    hovertemplate="%{x}: %{y:,.1f}<extra>Capital called</extra>")  # fmt: skip
        fig.add_bar(x=annual["year"], y=annual["lp_distributions"], name="Distributions", marker_color=BLUE,
                    hovertemplate="%{x}: %{y:,.1f}<extra>Distributions</extra>")  # fmt: skip
        years = quarterly["date"].dt.year + (quarterly["date"].dt.month - 6.5) / 12.0
        fig.add_scatter(x=years, y=quarterly["cum_net_cash_flow"], name="Cumulative net cash flow", mode="lines",
                        line={"color": AQUA, "width": 2}, customdata=quarterly["date"].dt.strftime("%b %Y"),
                        hovertemplate="%{customdata}: %{y:,.1f}<extra>Cumulative net cash flow</extra>")  # fmt: skip
        fig.update_layout(barmode="relative")
        show(style(fig, f"J-curve: LP cash flows ({unit})", y_format=",.0f"))
    with right:
        year_end = (
            quarterly.assign(year=quarterly["date"].dt.year).groupby("year")[["dpi", "rvpi"]].last().reset_index()
        )
        fig = go.Figure()
        fig.add_bar(x=year_end["year"], y=year_end["dpi"], name="DPI - distributed", marker_color=BLUE,
                    hovertemplate="%{x}: %{y:.2f}x<extra>DPI</extra>")  # fmt: skip
        fig.add_bar(x=year_end["year"], y=year_end["rvpi"], name="RVPI - residual value", marker_color=ORANGE,
                    hovertemplate="%{x}: %{y:.2f}x<extra>RVPI</extra>")  # fmt: skip
        fig.update_layout(barmode="stack")
        show(style(fig, "Net TVPI build-up: DPI plus RVPI at each year-end", y_format=".1f"))
    st.subheader(f"Snapshot at {valuation['as_of']:%d %B %Y}")
    snap = st.columns(6)
    snap[0].metric("DPI", f"{valuation['dpi']:.2f}x")
    snap[1].metric("RVPI", f"{valuation['rvpi']:.2f}x")
    snap[2].metric("Net TVPI", f"{valuation['tvpi']:.2f}x")
    snap[3].metric("Net IRR", f"{valuation['net_irr']:.1%}")
    snap[4].metric("LP net asset value", f"{valuation['nav']:,.1f}")
    snap[5].metric("Accrued carry", f"{valuation['carry_accrued']:,.1f}")
    st.caption("NAV is fair value less the carry that would be due if the fund were liquidated at that value.")

# ---- Waterfall --------------------------------------------------------------
with tabs[1]:
    tiers = [("Return of capital", "tier1_return_of_capital", NEUTRAL), ("Preferred return", "tier2_preferred_return", BLUE),
             ("LP share of profit split", None, AQUA), ("GP carried interest", "gp_carry", ORANGE)]  # fmt: skip
    paid = annual[annual["lp_distributions"] + annual["gp_carry"] > 1e-9]
    fig = go.Figure()
    for name, column, colour in tiers:
        values = paid[column] if column else paid["tier3_catch_up_lp"] + paid["tier4_split_lp"]
        fig.add_bar(x=paid["year"], y=values, name=name, marker_color=colour,
                    hovertemplate="%{x}: %{y:,.1f}<extra>" + name + "</extra>")  # fmt: skip
    fig.update_layout(barmode="stack")
    show(style(fig, f"Distributions by waterfall tier ({unit})", y_format=",.0f"))

    timeline = bundle["comparison"]["timeline"]
    fig = go.Figure()
    fig.add_scatter(x=timeline["date"], y=timeline["american_cumulative_carry"], name="Deal-by-deal", mode="lines",
                    line={"color": ORANGE, "width": 2, "shape": "hv"})  # fmt: skip
    fig.add_scatter(x=timeline["date"], y=timeline["european_cumulative_carry"], name="Whole-of-fund", mode="lines",
                    line={"color": BLUE, "width": 2, "shape": "hv"})  # fmt: skip
    fig.update_layout(hovermode="x unified")
    show(style(fig, f"Cumulative carry paid to the GP under each waterfall ({unit})", height=340, y_format=",.0f"))
    comparison = bundle["comparison"]["table"][["waterfall", "net_irr", "tvpi", "carry_gross", "clawback_paid",
                                                "carry_net", "first_carry_date", "carry_present_value"]]  # fmt: skip
    table(comparison.rename(columns={
        "waterfall": "Waterfall", "net_irr": "Net IRR", "tvpi": "Net TVPI", "carry_gross": "Carry paid",
        "clawback_paid": "Clawback", "carry_net": "Carry, net", "first_carry_date": "First carry",
        "carry_present_value": "Carry PV at 10%",
    }), {"Net IRR": "{:.1%}", "Net TVPI": "{:.2f}x", "Carry paid": money_fmt, "Clawback": money_fmt,
         "Carry, net": money_fmt, "Carry PV at 10%": money_fmt})  # fmt: skip

    st.subheader("Explain one distribution")
    active = quarterly[quarterly["lp_distributable"] > 1e-9]
    if active.empty:
        st.write("No cash was distributed.")
    else:
        labels = active["date"].dt.strftime("%d %b %Y").tolist()
        first_carry = active.index[active["gp_carry"] > 1e-9]
        default = labels.index(f"{quarterly.loc[first_carry[0], 'date']:%d %b %Y}") if len(first_carry) else 0
        choice = st.selectbox("Quarter", labels, index=default)
        st.text(explain_distribution(result, int(active.index[labels.index(choice)])))
    with st.expander("Quarter-by-quarter waterfall"):
        columns = ["date", "lp_contributions", "lp_distributable", "tier1_return_of_capital", "tier2_preferred_return",
                   "tier3_catch_up_gp", "tier4_split_lp", "tier4_split_gp", "gp_carry", "lp_capital_outstanding",
                   "lp_pref_outstanding", "lp_nav", "dpi", "tvpi"]  # fmt: skip
        view = quarterly[columns].copy()
        view["date"] = view["date"].dt.strftime("%Y-%m-%d")
        view.columns = ["Quarter end", "LP capital called", "LP distributable", "Tier 1 Return of capital",
                        "Tier 2 Preferred return", "Tier 3 GP catch-up", "Tier 4 to LPs", "Tier 4 to GP", "GP carry",
                        "Capital still to return", "Pref still to pay", "LP NAV", "DPI", "TVPI"]  # fmt: skip
        table(view, dict.fromkeys(view.columns[1:12], money_fmt) | {"DPI": "{:.2f}x", "TVPI": "{:.2f}x"})

# ---- Deals ------------------------------------------------------------------
with tabs[2]:
    deals = result.deals.sort_values("gross_moic")
    left, right = st.columns(2)
    with left:
        fig = go.Figure()
        fig.add_bar(y=deals["deal"], x=deals["gross_moic"], orientation="h", marker_color=BLUE,
                    customdata=np.stack([deals["invested"], deals["proceeds"], deals["hold_years"]], axis=-1),
                    hovertemplate="%{y}: %{x:.2f}x<br>Cost %{customdata[0]:,.1f}, proceeds %{customdata[1]:,.1f}"
                                  "<br>Held %{customdata[2]:.1f} years<extra></extra>")  # fmt: skip
        fig.add_vline(x=1.0, line_color=TOTAL, line_width=1)
        show(style(fig, "Gross MOIC by deal (line at 1.0x = cost)", height=430, legend=False, x_format=".1f",
                   bar_width=0.5))  # fmt: skip
    with right:
        drivers = bundle["value_creation"]
        drivers = drivers[drivers["amount"].abs() > 1e-9]
        fig = go.Figure(go.Waterfall(
            x=[*(wrap(d) for d in drivers["driver"]), "Total<br>gain"], y=[*drivers["amount"], 0],
            measure=["relative"] * len(drivers) + ["total"], connector={"line": {"color": GRID, "width": 1}},
            increasing={"marker": {"color": BLUE}}, decreasing={"marker": {"color": RED}},
            totals={"marker": {"color": TOTAL}}, hovertemplate="%{x}: %{y:,.1f}<extra></extra>",
        ))  # fmt: skip
        show(
            style(fig, f"Value creation: what drove the gross gain ({unit})", height=430, legend=False, y_format=",.0f")
        )
    view = result.deals[["deal", "sector", "strategy", "entry_date", "exit_date", "hold_years", "invested", "proceeds",
                         "gross_moic", "gross_irr", "share_of_gain", "status_at_valuation"]].copy()  # fmt: skip
    for column in ("entry_date", "exit_date"):
        view[column] = view[column].map(lambda d: f"{d:%Y-%m-%d}")
    view.columns = ["Deal", "Sector", "Strategy", "Entry", "Exit", "Hold (years)", "Invested", "Proceeds", "Gross MOIC",
                    "Gross IRR", "Share of gain", "Status at valuation date"]  # fmt: skip
    table(view, {"Hold (years)": "{:.1f}", "Invested": money_fmt, "Proceeds": money_fmt, "Gross MOIC": "{:.2f}x",
                 "Gross IRR": "{:.1%}", "Share of gain": "{:.1%}"})  # fmt: skip

# ---- Gross to net -----------------------------------------------------------
with tabs[3]:
    bridge = bundle["bridge"]
    steps = bridge.iloc[1:-1]
    steps = steps[steps["irr_impact"].abs() > 5e-6]
    left, right = st.columns(2)
    with left:
        fig = go.Figure(go.Waterfall(
            x=["Gross IRR", *(wrap(step) for step in steps["step"]), "Net IRR"],
            y=[bridge["irr_after"].iloc[0], *steps["irr_impact"], 0],
            measure=["absolute"] + ["relative"] * len(steps) + ["total"],
            connector={"line": {"color": GRID, "width": 1}}, increasing={"marker": {"color": BLUE}},
            decreasing={"marker": {"color": RED}}, totals={"marker": {"color": TOTAL}},
            hovertemplate="%{x}: %{y:.2%}<extra></extra>",
        ))  # fmt: skip
        show(style(fig, "Gross-to-net bridge: IRR", legend=False, y_format=".0%"))
    with right:
        split = bundle["value_split"].sort_values("amount")
        fig = go.Figure()
        fig.add_bar(y=split["component"], x=split["amount"], orientation="h", marker_color=BLUE,
                    customdata=split["share_of_gross_profit"],
                    hovertemplate="%{y}: %{x:,.1f} (%{customdata:.1%})<extra></extra>")  # fmt: skip
        show(style(fig, f"Where the gross profit goes ({unit})", legend=False, x_format=",.0f", bar_width=0.45))
    table(bundle["economics"].rename(columns={
        "party": "Party", "contributions": "Contributions", "distributions": "Distributions", "profit": "Profit",
        "multiple": "Multiple", "irr": "IRR", "share_of_gross_profit": "Share of gross profit",
    }), {"Contributions": money_fmt, "Distributions": money_fmt, "Profit": money_fmt, "Multiple": "{:.2f}x",
         "IRR": "{:.1%}", "Share of gross profit": "{:.1%}"})  # fmt: skip
    st.caption(f"Kaplan-Schoar PME against a benchmark compounding at {bundle['benchmark_return']:.0%}: "
               f"{bundle['pme']:.2f}x (above 1.0x means the fund beat it).")  # fmt: skip

# ---- Scenarios --------------------------------------------------------------
with tabs[4]:
    scenario_table = bundle["scenario_table"]
    if scenario_table is None:
        st.write("No scenario file was found at configs/scenarios.yaml.")
    else:
        st.caption("Each scenario applies its overrides on top of the terms and shocks set in the sidebar.")
        ordered = scenario_table.iloc[::-1]
        fig = go.Figure()
        fig.add_bar(y=ordered["label"], x=ordered["net_irr"], orientation="h",
                    marker_color=[BLUE if name == scenario_table["label"].iloc[0] else NEUTRAL for name in ordered["label"]],
                    customdata=ordered["tvpi"], hovertemplate="%{y}: %{x:.1%} net IRR, %{customdata:.2f}x TVPI<extra></extra>")  # fmt: skip
        fig.add_vline(x=cfg.waterfall.pref_rate, line_color=TOTAL, line_width=1)
        show(style(fig, "Net IRR by scenario (line = preferred return)", height=420, legend=False, x_format=".0%",
                   bar_width=0.5))  # fmt: skip
        view = scenario_table[["label", "description", "waterfall", "gross_moic", "net_irr", "tvpi", "carry_net",
                               "clawback_paid", "net_irr_vs_base"]].rename(columns={
            "label": "Scenario", "description": "What changes", "waterfall": "Waterfall", "gross_moic": "Gross MOIC",
            "net_irr": "Net IRR", "tvpi": "Net TVPI", "carry_net": "Carry, net", "clawback_paid": "Clawback",
            "net_irr_vs_base": "Net IRR vs base",
        })  # fmt: skip
        table(view, {"Gross MOIC": "{:.2f}x", "Net IRR": "{:.1%}", "Net TVPI": "{:.2f}x", "Carry, net": money_fmt,
                     "Clawback": money_fmt, "Net IRR vs base": lambda v: f"{v * 1e4:+,.0f} bps"})  # fmt: skip

# ---- Sensitivity ------------------------------------------------------------
with tabs[5]:
    names = {key: parameter.label for key, parameter in sc.PARAMETERS.items()
             if key in ("exit_multiple", "hold_shift", "ebitda_growth", "carry_rate", "pref_rate", "catch_up_rate", "fee_rate")}  # fmt: skip
    metrics = {"net_irr": "Net IRR", "tvpi": "Net TVPI", "carry_net": "Carry, net of clawback",
               "gp_total_economics": "GP total economics"}  # fmt: skip
    c1, c2, c3 = st.columns(3)
    x_name = c1.selectbox("Across", list(names), format_func=names.get, index=0)
    y_name = c2.selectbox("Down", [k for k in names if k != x_name], format_func=names.get, index=0)
    metric = c3.selectbox("Metric", list(metrics), format_func=metrics.get)
    if st.button("Run the two-way table"):
        st.session_state["grid_request"] = (config_json, x_name, y_name, metric)
    if st.session_state.get("grid_request", (None,))[0] == config_json:
        _, gx, gy, gm = st.session_state["grid_request"]
        with st.spinner("Re-running the fund for every cell ..."):
            grid = cached_grid(config_json, gx, gy, gm)
        unit_kind = sc.METRICS[gm][1]
        text_format = ".1%" if unit_kind == "pct" else (".2f" if unit_kind == "multiple" else ",.1f")

        def axis_labels(values, parameter: str) -> list[str]:
            kind = sc.PARAMETERS[parameter].unit
            if kind == "quarters":
                return [f"{int(v):+d}q" for v in values]
            if kind == "pct_points":
                return [f"{v * 100:+g} pts" for v in values]
            return [f"{v * 100:+g}%" if parameter == "exit_multiple" else f"{v * 100:g}%" for v in values]

        fig = go.Figure(go.Heatmap(
            z=grid.to_numpy(), x=axis_labels(grid.columns, gx), y=axis_labels(grid.index, gy),
            colorscale=[[i / (len(SEQUENTIAL) - 1), c] for i, c in enumerate(SEQUENTIAL)],
            texttemplate="%{z:" + text_format + "}", xgap=2, ygap=2, showscale=False,
            hovertemplate=f"{names[gx]} %{{x}}<br>{names[gy]} %{{y}}<br>{metrics[gm]} %{{z:{text_format}}}<extra></extra>",
        ))  # fmt: skip
        fig.update_yaxes(autorange="reversed", title=names[gy])
        fig.update_xaxes(title=names[gx], type="category")
        fig.update_yaxes(type="category")
        show(style(fig, f"{metrics[gm]}: {names[gx]} against {names[gy]}", height=420, legend=False, bar_width=None))
    st.subheader("One assumption at a time")
    if st.button("Run the tornado"):
        st.session_state["tornado_request"] = config_json
    if st.session_state.get("tornado_request") == config_json:
        tornado = cached_tornado(config_json).iloc[::-1]
        fig = go.Figure()
        fig.add_bar(y=tornado["label"], x=tornado["low_delta"] * 1e4, orientation="h", name="Unfavourable case",
                    marker_color=RED, hovertemplate="%{y}: %{x:+,.0f} bps<extra>Unfavourable</extra>")  # fmt: skip
        fig.add_bar(y=tornado["label"], x=tornado["high_delta"] * 1e4, orientation="h", name="Favourable case",
                    marker_color=BLUE, hovertemplate="%{y}: %{x:+,.0f} bps<extra>Favourable</extra>")  # fmt: skip
        fig.update_layout(barmode="relative")
        show(style(fig, "Change in net IRR from the current case (bps)", height=400, x_format="+,.0f"))

# ---- Monte Carlo ------------------------------------------------------------
with tabs[6]:
    c1, c2 = st.columns(2)
    n_sims = c1.select_slider("Trials", options=[250, 500, 1000, 2000, 5000], value=1000)
    seed = c2.number_input("Random seed", value=42, step=1)
    if st.button("Run the simulation"):
        st.session_state["mc_request"] = (config_json, int(n_sims), int(seed))
    if st.session_state.get("mc_request", (None,))[0] == config_json:
        _, run_sims, run_seed = st.session_state["mc_request"]
        with st.spinner(f"Simulating {run_sims:,} portfolios under both waterfalls ..."):
            sims = cached_monte_carlo(config_json, run_sims, run_seed)
        stats = summarize_monte_carlo(sims)
        m = st.columns(5)
        m[0].metric("Median net IRR", f"{stats['net_irr_median']:.1%}")
        m[1].metric("5th to 95th percentile", f"{stats['net_irr_p5']:.1%} to {stats['net_irr_p95']:.1%}")
        m[2].metric("Below the hurdle", f"{stats['prob_net_irr_below_pref']:.1%}")
        m[3].metric("Capital loss", f"{stats['prob_capital_loss']:.1%}")
        m[4].metric("Material clawback, deal-by-deal", f"{stats['prob_clawback_american']:.1%}")
        fig = go.Figure(go.Histogram(x=sims["net_irr"].fillna(-1.0), xbins={"size": 0.01}, marker_color=BLUE,
                                     histnorm="probability", hovertemplate="%{x:.0%}: %{y:.1%} of trials<extra></extra>"))  # fmt: skip
        fig.add_vline(x=cfg.waterfall.pref_rate, line_color=TOTAL, line_width=1, annotation_text="Hurdle")
        fig.update_layout(bargap=0.12)
        show(style(fig, "Distribution of LP net IRR", legend=False, x_format=".0%", y_format=".0%", bar_width=None))
        st.caption("Trials bunch just above the hurdle because the GP catch-up takes every extra dollar there.")

# ---- AI commentary ----------------------------------------------------------
with tabs[7]:
    try:
        provider = detect_provider()
    except LLMError as exc:
        provider = "offline"
        st.warning(str(exc))
    if provider == "offline":
        st.info("No API key is set, so the deterministic writer is used. Set ANTHROPIC_API_KEY, or OPENAI_API_KEY "
                "with PEFUND_LLM_MODEL, to have a language model write the commentary.")  # fmt: skip
    else:
        st.info(f"Commentary will be written by the {provider} provider and checked against the model output.")
    if st.button("Write commentary"):
        with st.spinner("Writing and verifying ..."):
            st.session_state["commentary"] = (config_json, generate_commentary(bundle))
    if st.session_state.get("commentary", (None,))[0] == config_json:
        commentary = st.session_state["commentary"][1]
        if commentary.grounding.passed:
            st.success(f"Numeric grounding: {commentary.grounding.summary()}")
        else:
            st.warning(f"Numeric grounding: {commentary.grounding.summary()}")
        for note in commentary.notes:
            st.caption(note)
        st.markdown(commentary.text)
    st.subheader("Ask about this fund")
    question = st.text_input("Question", placeholder="Why does the GP receive carry so late?")
    if question:
        answer, report = answer_question(bundle, question)
        st.write(answer)
        if report.total:
            st.caption(f"Numeric grounding: {report.summary()}")

# ---- Export -----------------------------------------------------------------
with tabs[8]:
    st.write("Download the current case.")
    st.download_button("Quarterly cash flows (CSV)", quarterly.to_csv(index=False).encode("utf-8"),
                       "quarterly_cash_flows.csv", "text/csv")  # fmt: skip
    st.download_button("Configuration (YAML)", yaml.safe_dump(cfg.to_dict(), sort_keys=False).encode("utf-8"),
                       "fund_config.yaml", "text/yaml")  # fmt: skip
    if st.button("Build the Excel model"):
        from pefund.reporting.excel_model import build_workbook

        with st.spinner("Building the workbook ..."):
            buffer = io.BytesIO()
            with tempfile.TemporaryDirectory() as tmp:
                path = build_workbook(bundle, Path(tmp) / "model.xlsx", commentary=generate_commentary(
                    bundle, provider="offline").to_markdown(), source="the dashboard")  # fmt: skip
                buffer.write(path.read_bytes())
        st.session_state["workbook"] = (config_json, buffer.getvalue())
    if st.session_state.get("workbook", (None,))[0] == config_json:
        st.download_button("Download the Excel model", st.session_state["workbook"][1], "PE_Fund_Waterfall_Model.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")  # fmt: skip
        st.caption("Open it in Excel to calculate the formulas; the Checks sheet compares them with this run.")
