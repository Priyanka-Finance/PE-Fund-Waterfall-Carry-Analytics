"""End-to-end pipeline: run every analysis and write every output.

``build_bundle`` runs the fund model and all analytics and returns the results
in one dictionary; ``write_outputs`` turns that dictionary into CSV tables, a
JSON summary and charts.  The command line, the Excel builder, the Gen AI layer
and the Streamlit app all consume the same bundle, so every surface reports the
same numbers.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import analytics
from . import scenarios as sc
from .config import FundConfig
from .fund import run_fund
from .montecarlo import MonteCarloSettings, run_monte_carlo, summarize_monte_carlo


def build_bundle(
    cfg: FundConfig,
    scenarios: dict[str, dict[str, Any]] | None = None,
    *,
    sensitivities: bool = True,
    monte_carlo: MonteCarloSettings | None | bool = True,
    benchmark_return: float = 0.08,
) -> dict[str, Any]:
    """Run the fund and every analysis on it.

    Parameters
    ----------
    scenarios
        Named scenarios (see ``pefund.scenarios.load_scenarios``); skipped if ``None``.
    sensitivities
        Compute the two-way grids, the tornado and the breakevens.
    monte_carlo
        ``True`` for default settings, a :class:`MonteCarloSettings`, or ``False`` / ``None`` to skip.
    benchmark_return
        Constant annual return of the public-market benchmark used for the PME.
    """
    result = run_fund(cfg)
    valuation_q = result.valuation_quarter
    bundle: dict[str, Any] = {
        "config": cfg,
        "result": result,
        "summary": result.summary,
        "valuation": result.metrics(valuation_q),
        "bridge": analytics.gross_to_net_bridge(result),
        "bridge_valuation": analytics.gross_to_net_bridge(result, valuation_q),
        "value_split": analytics.value_split(result),
        "economics": analytics.lp_gp_economics(result),
        "carry_profile": analytics.carry_profile(result),
        "value_creation": analytics.value_creation(result),
        "concentration": analytics.concentration(result),
        "sectors": analytics.sector_summary(result),
        "annual": analytics.annual_summary(result),
        "investors": analytics.investor_allocation(result, valuation_q),
        "comparison": analytics.compare_waterfalls(cfg),
        "pme": analytics.public_market_equivalent(result, benchmark_return),
        "benchmark_return": benchmark_return,
        "scenario_table": None,
        "scenario_results": None,
        "grids": None,
        "tornado": None,
        "breakevens": None,
        "monte_carlo": None,
        "monte_carlo_stats": None,
    }
    if scenarios:
        bundle["scenario_table"], bundle["scenario_results"] = sc.run_scenarios(cfg, scenarios)
    if sensitivities:
        bundle["grids"] = sc.run_default_grids(cfg)
        bundle["tornado"] = sc.tornado(cfg)
        bundle["breakevens"] = sc.breakevens(cfg)
    if monte_carlo:
        settings = monte_carlo if isinstance(monte_carlo, MonteCarloSettings) else MonteCarloSettings()
        sims = run_monte_carlo(cfg, settings)
        bundle["monte_carlo"] = sims
        bundle["monte_carlo_stats"] = summarize_monte_carlo(sims)
    return bundle


def _jsonable(value: Any) -> Any:
    """Convert numpy / pandas / date values into plain JSON types."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime, pd.Timestamp)):
        return value.isoformat()[:10]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def summary_dict(bundle: dict[str, Any]) -> dict[str, Any]:
    """Headline results as a JSON-ready dictionary."""
    result = bundle["result"]
    return _jsonable(
        {
            "fund": result.config.fund.name,
            "currency": result.config.fund.currency,
            "unit": result.config.fund.unit,
            "waterfall": result.config.waterfall.type,
            "at_liquidation": bundle["summary"],
            "at_valuation_date": bundle["valuation"],
            "carry_profile": bundle["carry_profile"],
            "concentration": bundle["concentration"],
            "public_market_equivalent": {"ks_pme": bundle["pme"], "benchmark_return": bundle["benchmark_return"]},
            "monte_carlo": bundle["monte_carlo_stats"],
            "flags": result.flags,
        }
    )


def write_outputs(bundle: dict[str, Any], out_dir: str | Path, *, charts: bool = True) -> dict[str, Path]:
    """Write CSV tables, the JSON summary and (optionally) the charts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = bundle["result"]
    written: dict[str, Path] = {}

    def save(name: str, frame: pd.DataFrame | None, index: bool = False) -> None:
        if frame is None or (hasattr(frame, "empty") and frame.empty):
            return
        path = out / f"{name}.csv"
        frame.to_csv(path, index=index, float_format="%.6f")
        written[name] = path

    save("quarterly_cash_flows", result.quarterly)
    save("annual_summary", bundle["annual"])
    save("deals", result.deals)
    save("gross_to_net_bridge", bundle["bridge"])
    save("gross_to_net_bridge_valuation_date", bundle["bridge_valuation"])
    save("value_split", bundle["value_split"])
    save("lp_gp_economics", bundle["economics"])
    save("value_creation", bundle["value_creation"])
    save("sector_summary", bundle["sectors"])
    save("investor_capital_accounts", bundle["investors"])
    save("waterfall_comparison", bundle["comparison"]["table"])
    save("carry_timeline", bundle["comparison"]["timeline"])
    save("scenarios", bundle["scenario_table"])
    save("tornado", bundle["tornado"])
    save("breakevens", bundle["breakevens"])
    for name, grid in (bundle["grids"] or {}).items():
        save(f"sensitivity_{name}", grid, index=True)
    if bundle["monte_carlo"] is not None:
        save("monte_carlo_trials", bundle["monte_carlo"])
        stats = pd.DataFrame(list(bundle["monte_carlo_stats"].items()), columns=["statistic", "value"])
        save("monte_carlo_summary", stats)

    summary_path = out / "summary.json"
    summary_path.write_text(json.dumps(summary_dict(bundle), indent=2), encoding="utf-8")
    written["summary"] = summary_path

    if charts:
        from .reporting.charts import render_all

        for name, path in render_all(bundle, out / "charts").items():
            written[f"chart_{name}"] = path
    return written
