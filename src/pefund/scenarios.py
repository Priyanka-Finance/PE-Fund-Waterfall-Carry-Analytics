"""Scenario, sensitivity, tornado and breakeven analysis.

Everything here follows one pattern: change some assumptions, re-run the whole
fund model and compare.  Nothing is approximated - a scenario that lengthens
holding periods really does charge more management fee, accrue more preferred
return and push carry later, because the engine is re-run end to end.

Parameters can be addressed either by a friendly name from ``PARAMETERS``
(``"exit_multiple"``, ``"carry_rate"`` ...) or by a dotted configuration path
(``"waterfall.pref_rate"``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from .config import ConfigError, FundConfig
from .fund import FundResult, run_fund


# --------------------------------------------------------------------------- #
# Parameters and metrics
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Parameter:
    """An assumption that can be flexed, with how to label and apply it."""

    label: str
    unit: str  # "pct" | "pct_points" | "quarters" | "multiple"
    apply: Callable[[FundConfig, float], dict[str, Any]]
    base: Callable[[FundConfig], float]


def _fee_rate(cfg: FundConfig, value: float) -> dict[str, Any]:
    """Move the investment-period fee and keep the step-down to the later rate."""
    step_down = cfg.management_fee.rate_investment_period - cfg.management_fee.rate_post_investment_period
    return {
        "management_fee.rate_investment_period": value,
        "management_fee.rate_post_investment_period": max(value - step_down, 0.0),
    }


def _catch_up(cfg: FundConfig, value: float) -> dict[str, Any]:
    return {"waterfall.catch_up_rate": value}


PARAMETERS: dict[str, Parameter] = {
    "exit_multiple": Parameter(
        "Exit multiple change", "pct",
        lambda cfg, v: {"shocks.exit_multiple_pct": float(v)}, lambda cfg: cfg.shocks.exit_multiple_pct,
    ),
    "hold_shift": Parameter(
        "Holding period change (quarters)", "quarters",
        lambda cfg, v: {"shocks.hold_shift_quarters": int(round(v))}, lambda cfg: cfg.shocks.hold_shift_quarters,
    ),
    "ebitda_growth": Parameter(
        "EBITDA growth change", "pct_points",
        lambda cfg, v: {"shocks.ebitda_growth_shift": float(v)}, lambda cfg: cfg.shocks.ebitda_growth_shift,
    ),
    "carry_rate": Parameter(
        "Carried interest rate", "pct",
        lambda cfg, v: {"waterfall.carry_rate": float(v)}, lambda cfg: cfg.waterfall.carry_rate,
    ),
    "pref_rate": Parameter(
        "Preferred return", "pct",
        lambda cfg, v: {"waterfall.pref_rate": float(v)}, lambda cfg: cfg.waterfall.pref_rate,
    ),
    "catch_up_rate": Parameter(
        "GP catch-up rate", "pct", _catch_up, lambda cfg: cfg.waterfall.catch_up_rate,
    ),
    "fee_rate": Parameter(
        "Management fee rate", "pct", _fee_rate, lambda cfg: cfg.management_fee.rate_investment_period,
    ),
    "gp_commitment": Parameter(
        "GP commitment", "pct",
        lambda cfg, v: {"fund.gp_commitment_pct": float(v)}, lambda cfg: cfg.fund.gp_commitment_pct,
    ),
    "escrow": Parameter(
        "Carry escrow", "pct",
        lambda cfg, v: {"clawback.escrow_pct": float(v)}, lambda cfg: cfg.clawback.escrow_pct,
    ),
}  # fmt: skip

#: Liquidation-basis outputs every scenario reports: name -> (label, unit).
METRICS: dict[str, tuple[str, str]] = {
    "gross_moic": ("Gross MOIC", "multiple"),
    "gross_irr": ("Gross IRR", "pct"),
    "net_irr": ("Net IRR", "pct"),
    "tvpi": ("Net TVPI", "multiple"),
    "dpi": ("DPI", "multiple"),
    "gross_to_net_irr_spread": ("Gross-to-net spread", "pct"),
    "total_lp_profit": ("LP net profit", "money"),
    "total_management_fees": ("Management fees", "money"),
    "carry_gross": ("Carry distributed", "money"),
    "clawback_paid": ("Clawback paid", "money"),
    "clawback_shortfall": ("Clawback shortfall", "money"),
    "carry_net": ("Carry, net of clawback", "money"),
    "gp_total_economics": ("GP total economics", "money"),
    "gp_share_of_gross_profit": ("GP share of gross profit", "pct"),
    "first_carry_year": ("First carry (year of fund life)", "years"),
    "fund_life_years": ("Fund life (years)", "years"),
}


def headline(result: FundResult) -> dict[str, float]:
    """The standard liquidation-basis metric set for one run."""
    summary = result.summary
    out = {name: float(summary[name]) for name in METRICS if name in summary}
    q = summary["first_carry_quarter"]
    out["first_carry_year"] = float(result.schedule.t[q]) if q >= 0 else float("nan")
    return out


def overrides_for(cfg: FundConfig, parameter: str, value: float) -> dict[str, Any]:
    """Dotted-path overrides that set ``parameter`` to ``value``."""
    if parameter in PARAMETERS:
        return PARAMETERS[parameter].apply(cfg, value)
    if "." not in parameter:
        raise ConfigError(f"Unknown parameter '{parameter}'. Known names: {sorted(PARAMETERS)} or a dotted path.")
    return {parameter: value}


def parameter_label(parameter: str) -> str:
    return PARAMETERS[parameter].label if parameter in PARAMETERS else parameter


def parameter_unit(parameter: str) -> str:
    return PARAMETERS[parameter].unit if parameter in PARAMETERS else "number"


def run_with(cfg: FundConfig, values: dict[str, float], *, interim: bool = False) -> FundResult:
    """Run the fund with several parameters changed at once."""
    dotted: dict[str, Any] = {}
    for parameter, value in values.items():
        dotted.update(overrides_for(cfg, parameter, value))
    return run_fund(cfg.with_values(dotted), interim=interim)


# --------------------------------------------------------------------------- #
# Named scenarios
# --------------------------------------------------------------------------- #
def default_label(name: str) -> str:
    """'severe_downside' -> 'Severe downside'."""
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def load_scenarios(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read a scenario file: ``{name: {label: ..., description: ..., overrides: {...}}}``."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = raw.get("scenarios", raw)
    if not isinstance(scenarios, dict) or not scenarios:
        raise ConfigError(f"No scenarios found in {path}")
    for name, spec in scenarios.items():
        spec = spec or {}
        unknown = sorted(set(spec) - {"label", "description", "overrides"})
        if unknown:
            raise ConfigError(
                f"Scenario '{name}': unknown key(s) {unknown}; use 'label', 'description' and 'overrides'"
            )
        scenarios[name] = {
            "label": spec.get("label") or default_label(name),
            "description": spec.get("description", ""),
            "overrides": spec.get("overrides") or {},
        }
    return scenarios


def run_scenarios(
    cfg: FundConfig, scenarios: dict[str, dict[str, Any]], *, interim: bool = True
) -> tuple[pd.DataFrame, dict[str, FundResult]]:
    """Run every named scenario and tabulate the results against the first one.

    The first scenario is the reference ("base") case for the ``*_vs_base`` columns.
    """
    results: dict[str, FundResult] = {}
    rows = []
    for name, spec in scenarios.items():
        scenario_cfg = cfg.with_overrides(spec.get("overrides"))
        result = run_fund(scenario_cfg, interim=interim)
        results[name] = result
        row: dict[str, Any] = {
            "scenario": name,
            "label": spec.get("label") or default_label(name),
            "description": spec.get("description", ""),
            "waterfall": scenario_cfg.waterfall.type,
        }
        row.update(headline(result))
        rows.append(row)
    table = pd.DataFrame(rows)
    base = table.iloc[0]
    for metric in ("net_irr", "tvpi", "carry_net", "total_lp_profit", "gross_moic"):
        table[f"{metric}_vs_base"] = table[metric] - base[metric]
    return table, results


# --------------------------------------------------------------------------- #
# Sensitivities
# --------------------------------------------------------------------------- #
def one_way(
    cfg: FundConfig, parameter: str, values: Sequence[float], metrics: Sequence[str] = ("net_irr", "tvpi", "carry_net")
) -> pd.DataFrame:
    """Flex one parameter and report several metrics."""
    rows = []
    for value in values:
        result = run_with(cfg, {parameter: value})
        stats = headline(result)
        rows.append({"parameter": parameter, "value": value, **{m: stats[m] for m in metrics}})
    return pd.DataFrame(rows)


def sensitivity_grid(
    cfg: FundConfig,
    x: tuple[str, Sequence[float]],
    y: tuple[str, Sequence[float]],
    metric: str = "net_irr",
) -> pd.DataFrame:
    """Two-way data table: ``metric`` for every combination of two parameters.

    Rows are the ``y`` parameter, columns the ``x`` parameter.  Labels, units and
    the base values are stored in ``DataFrame.attrs`` for charting.
    """
    x_name, x_values = x
    y_name, y_values = y
    grid = np.full((len(y_values), len(x_values)), np.nan)
    for i, y_value in enumerate(y_values):
        for j, x_value in enumerate(x_values):
            grid[i, j] = headline(run_with(cfg, {x_name: x_value, y_name: y_value}))[metric]
    frame = pd.DataFrame(grid, index=list(y_values), columns=list(x_values))
    frame.index.name, frame.columns.name = y_name, x_name
    frame.attrs.update(
        metric=metric,
        metric_label=METRICS[metric][0],
        metric_unit=METRICS[metric][1],
        x=x_name, x_label=parameter_label(x_name), x_unit=parameter_unit(x_name),
        y=y_name, y_label=parameter_label(y_name), y_unit=parameter_unit(y_name),
    )  # fmt: skip
    if x_name in PARAMETERS:
        frame.attrs["x_base"] = PARAMETERS[x_name].base(cfg)
    if y_name in PARAMETERS:
        frame.attrs["y_base"] = PARAMETERS[y_name].base(cfg)
    return frame


#: The standard set of two-way tables produced by the pipeline.
DEFAULT_GRIDS: list[dict[str, Any]] = [
    {
        "name": "exit_multiple_x_holding_period",
        "x": ("exit_multiple", [-0.30, -0.20, -0.10, 0.0, 0.10, 0.20, 0.30]),
        "y": ("hold_shift", [-4, -2, 0, 2, 4, 6, 8]),
        "metrics": ["net_irr", "tvpi", "carry_net"],
    },
    {
        "name": "carry_x_preferred_return",
        "x": ("carry_rate", [0.10, 0.15, 0.20, 0.25, 0.30]),
        "y": ("pref_rate", [0.0, 0.06, 0.08, 0.10, 0.12]),
        "metrics": ["net_irr", "carry_net"],
    },
    {
        "name": "management_fee_x_carry",
        "x": ("fee_rate", [0.010, 0.015, 0.020, 0.025]),
        "y": ("carry_rate", [0.10, 0.15, 0.20, 0.25, 0.30]),
        "metrics": ["net_irr", "tvpi", "gp_total_economics"],
    },
    {
        "name": "exit_multiple_x_ebitda_growth",
        "x": ("exit_multiple", [-0.30, -0.20, -0.10, 0.0, 0.10, 0.20, 0.30]),
        "y": ("ebitda_growth", [-0.04, -0.02, 0.0, 0.02, 0.04]),
        "metrics": ["net_irr", "gross_moic"],
    },
    {
        "name": "catch_up_x_preferred_return",
        "x": ("catch_up_rate", [0.0, 0.5, 0.8, 1.0]),
        "y": ("pref_rate", [0.0, 0.06, 0.08, 0.10, 0.12]),
        "metrics": ["carry_net", "net_irr"],
    },
]


def run_default_grids(cfg: FundConfig, grids: list[dict[str, Any]] | None = None) -> dict[str, pd.DataFrame]:
    """Compute every table in ``DEFAULT_GRIDS``; keys are ``<grid>__<metric>``."""
    out: dict[str, pd.DataFrame] = {}
    for spec in grids or DEFAULT_GRIDS:
        x_name, x_values = spec["x"]
        y_name, y_values = spec["y"]
        cells = {
            (i, j): headline(run_with(cfg, {x_name: x_value, y_name: y_value}))
            for i, y_value in enumerate(y_values)
            for j, x_value in enumerate(x_values)
        }
        for metric in spec["metrics"]:
            grid = np.array([[cells[i, j][metric] for j in range(len(x_values))] for i in range(len(y_values))])
            frame = pd.DataFrame(grid, index=list(y_values), columns=list(x_values))
            frame.index.name, frame.columns.name = y_name, x_name
            frame.attrs.update(
                metric=metric, metric_label=METRICS[metric][0], metric_unit=METRICS[metric][1],
                x=x_name, x_label=parameter_label(x_name), x_unit=parameter_unit(x_name),
                y=y_name, y_label=parameter_label(y_name), y_unit=parameter_unit(y_name),
                x_base=PARAMETERS[x_name].base(cfg), y_base=PARAMETERS[y_name].base(cfg),
            )  # fmt: skip
            out[f"{spec['name']}__{metric}"] = frame
    return out


#: One-at-a-time shocks for the tornado chart: (parameter, low value, high value).
DEFAULT_TORNADO: list[tuple[str, float, float]] = [
    ("exit_multiple", -0.15, 0.15),
    ("ebitda_growth", -0.02, 0.02),
    ("hold_shift", 4, -4),
    ("fee_rate", 0.025, 0.015),
    ("carry_rate", 0.25, 0.15),
    ("pref_rate", 0.06, 0.10),
    ("catch_up_rate", 1.0, 0.0),
]


def tornado(
    cfg: FundConfig, shocks: Sequence[tuple[str, float, float]] | None = None, metric: str = "net_irr"
) -> pd.DataFrame:
    """One-at-a-time sensitivity, sorted by the size of the swing.

    For each parameter the model is run at an unfavourable and a favourable
    value; the table shows the resulting metric and its change from base.
    """
    base_value = headline(run_fund(cfg, interim=False))[metric]
    rows = []
    for parameter, low_value, high_value in shocks or DEFAULT_TORNADO:
        low = headline(run_with(cfg, {parameter: low_value}))[metric]
        high = headline(run_with(cfg, {parameter: high_value}))[metric]
        rows.append(
            {
                "parameter": parameter,
                "label": parameter_label(parameter),
                "unit": parameter_unit(parameter),
                "low_input": low_value,
                "high_input": high_value,
                "low_result": low,
                "high_result": high,
                "low_delta": low - base_value,
                "high_delta": high - base_value,
                "swing": abs(high - low),
            }
        )
    frame = pd.DataFrame(rows).sort_values("swing", ascending=False).reset_index(drop=True)
    frame.attrs.update(metric=metric, metric_label=METRICS[metric][0], metric_unit=METRICS[metric][1], base=base_value)
    return frame


# --------------------------------------------------------------------------- #
# Breakevens
# --------------------------------------------------------------------------- #
def solve_for(
    cfg: FundConfig,
    parameter: str,
    metric: str,
    target: float,
    low: float,
    high: float,
    tolerance: float = 1e-5,
) -> float:
    """Value of ``parameter`` in ``[low, high]`` at which ``metric`` equals ``target``.

    Uses bisection, so the metric must move in one direction across the range
    (true of return metrics against exit value).  Returns ``nan`` if the target
    is not bracketed.
    """

    def gap(value: float) -> float:
        got = headline(run_with(cfg, {parameter: value}))[metric]
        return (got if np.isfinite(got) else -1.0) - target

    f_low, f_high = gap(low), gap(high)
    if f_low * f_high > 0:
        return float("nan")
    for _ in range(60):
        mid = 0.5 * (low + high)
        f_mid = gap(mid)
        if abs(high - low) < tolerance:
            break
        if f_low * f_mid <= 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return 0.5 * (low + high)


def breakevens(cfg: FundConfig) -> pd.DataFrame:
    """How far exit values can fall before key thresholds are crossed.

    Each row gives the change in exit multiples (applied to every deal) at which
    the fund just reaches a threshold, and the gross MOIC that implies.
    """
    pref = cfg.waterfall.pref_rate
    cases = [
        ("LPs get their capital back (net TVPI = 1.0x)", "tvpi", 1.0),
        (f"LPs earn the preferred return (net IRR = {pref:.0%}); carry starts above this", "net_irr", pref),
        ("LPs earn a 15% net IRR", "net_irr", 0.15),
        ("LPs earn a 20% net IRR", "net_irr", 0.20),
    ]
    rows = []
    for label, metric, target in cases:
        shock = solve_for(cfg, "exit_multiple", metric, target, -0.95, 3.0)
        if np.isfinite(shock):
            stats = headline(run_with(cfg, {"exit_multiple": shock}))
            rows.append(
                {
                    "threshold": label,
                    "exit_multiple_change": shock,
                    "gross_moic": stats["gross_moic"],
                    "gross_irr": stats["gross_irr"],
                    "net_irr": stats["net_irr"],
                    "tvpi": stats["tvpi"],
                    "carry_net": stats["carry_net"],
                }
            )
        else:
            rows.append({"threshold": label, "exit_multiple_change": np.nan})
    return pd.DataFrame(rows)
