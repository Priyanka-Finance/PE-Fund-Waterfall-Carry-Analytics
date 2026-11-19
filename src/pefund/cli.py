"""Command-line interface.

Examples::

    pefund run                                   # headline results for the base case
    pefund run --set waterfall.type=american     # same fund, deal-by-deal waterfall
    pefund compare                               # whole-of-fund against deal-by-deal
    pefund scenarios                             # every named scenario
    pefund sensitivity                           # two-way tables, tornado, breakevens
    pefund montecarlo --sims 5000
    pefund explain --date 2029-12-31             # tier-by-tier walk through one quarter
    pefund commentary                            # grounded commentary (offline without a key)
    pefund ask "Why is carry paid so late?"
    pefund excel                                 # build the Excel model
    pefund all                                   # everything: tables, charts, Excel, commentary
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from . import __version__, analytics
from . import scenarios as sc
from .config import ConfigError, FundConfig, load_config
from .fund import FundResult, run_fund
from .montecarlo import MonteCarloSettings, run_monte_carlo, summarize_monte_carlo

DEFAULT_CONFIG = "configs/base_case.yaml"
DEFAULT_SCENARIOS = "configs/scenarios.yaml"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{round(value * 100, 1) + 0.0:.1f}%"  # "+ 0.0" turns a rounded -0.0 into 0.0


def _mult(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:.2f}x"


def _money(value: Any) -> str:
    return "n/a" if value is None or pd.isna(value) else f"{value:,.1f}"


def _table(rows: list[tuple[str, str]], title: str | None = None) -> str:
    width = max(len(label) for label, _ in rows)
    lines = [title, "-" * len(title)] if title else []
    lines += [f"{label.ljust(width)}  {value}" for label, value in rows]
    return "\n".join(lines)


HEADERS = {
    "label": "Scenario", "waterfall": "Waterfall", "gross_moic": "Gross MOIC", "gross_irr": "Gross IRR",
    "net_irr": "Net IRR", "tvpi": "Net TVPI", "carry_net": "Carry (net)", "carry_gross": "Carry paid",
    "clawback_paid": "Clawback", "total_management_fees": "Mgmt fees", "net_irr_vs_base": "vs base",
    "lp_distributions": "LP distributions", "first_carry_date": "First carry",
    "average_carry_timing_years": "Avg carry timing (yrs)", "carry_present_value": "Carry PV @10%",
    "peak_clawback_exposure": "Peak clawback exposure", "low_input": "Low input", "high_input": "High input",
    "low_result": "Result at low", "high_result": "Result at high", "swing": "Swing", "threshold": "Threshold",
    "exit_multiple_change": "Exit multiple change",
}  # fmt: skip


def _frame(frame: pd.DataFrame, formats: dict[str, Any]) -> str:
    shown = frame.copy()
    for column, formatter in formats.items():
        if column in shown:
            shown[column] = shown[column].map(formatter)
    return shown.rename(columns=HEADERS).to_string(index=False)


def _headline(result: FundResult) -> str:
    cfg, s = result.config, result.summary
    unit = f"{cfg.fund.currency} {cfg.fund.unit}"
    kind = "whole-of-fund (European)" if cfg.waterfall.type == "european" else "deal-by-deal (American)"
    valuation = result.metrics(result.valuation_quarter)
    first_carry = f"{s['first_carry_date']:%b %Y}" if s["first_carry_date"] else "none paid"
    blocks = [
        f"{cfg.fund.name}\nWaterfall: {kind}; pref {cfg.waterfall.pref_rate:.1%}, catch-up "
        f"{cfg.waterfall.catch_up_rate:.0%}, carry {cfg.waterfall.carry_rate:.0%}.  Money in {unit}.",
        _table(
            [
                ("Net IRR", _pct(s["net_irr"])),
                ("Net TVPI", _mult(s["tvpi"])),
                ("DPI", _mult(s["dpi"])),
                ("Gross IRR", _pct(s["gross_irr"])),
                ("Gross MOIC", _mult(s["gross_moic"])),
                ("Gross-to-net spread", f"{s['gross_to_net_irr_spread'] * 1e4:,.0f} bps"),
                ("LP paid-in", _money(s["paid_in"])),
                ("LP distributions", _money(s["distributed"])),
                ("Fund life", f"{s['fund_life_years']:.1f} years"),
            ],
            "At liquidation",
        ),  # fmt: skip
        _table(
            [
                ("Tier 1 Return of capital", _money(s["tier1_return_of_capital"])),
                ("Tier 2 Preferred return", _money(s["tier2_preferred_return"])),
                ("Tier 3 GP catch-up", _money(s["tier3_catch_up_gp"])),
                ("Tier 4 to LPs", _money(s["tier4_split_lp"])),
                ("Tier 4 to GP", _money(s["tier4_split_gp"])),
                ("Carry distributed", _money(s["carry_gross"])),
                ("Clawback paid", _money(s["clawback_paid"])),
                ("Carry, net of clawback", _money(s["carry_net"])),
                ("First carry", first_carry),
                ("Management fees", _money(s["total_management_fees"])),
                ("GP share of gross profit", _pct(s["gp_share_of_gross_profit"])),
            ],
            "Waterfall and GP economics",
        ),  # fmt: skip
        _table(
            [
                ("DPI", _mult(valuation["dpi"])),
                ("RVPI", _mult(valuation["rvpi"])),
                ("Net TVPI", _mult(valuation["tvpi"])),
                ("Net IRR", _pct(valuation["net_irr"])),
                ("LP net asset value", _money(valuation["nav"])),
                ("Accrued carry", _money(valuation["carry_accrued"])),
            ],
            f"Snapshot at {valuation['as_of']:%d %b %Y}",
        ),  # fmt: skip
    ]
    if result.flags:
        blocks.append("Notes:\n" + "\n".join(f"  - {flag}" for flag in result.flags))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Shared set-up
# --------------------------------------------------------------------------- #
def _parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigError(f"--set expects key=value, got '{pair}'")
        key, raw = pair.split("=", 1)
        overrides[key.strip()] = yaml.safe_load(raw)
    return overrides


def _config(args: argparse.Namespace) -> FundConfig:
    cfg = load_config(args.config)
    overrides = _parse_overrides(getattr(args, "set", None))
    return cfg.with_values(overrides) if overrides else cfg


def _bundle(args: argparse.Namespace, cfg: FundConfig) -> dict[str, Any]:
    from .pipeline import build_bundle

    scenarios_path = Path(getattr(args, "scenarios", DEFAULT_SCENARIOS) or "")
    scenarios = sc.load_scenarios(scenarios_path) if scenarios_path.is_file() else None
    monte_carlo: Any = False
    if not getattr(args, "no_monte_carlo", False):
        monte_carlo = MonteCarloSettings(n_sims=getattr(args, "sims", 2000), seed=getattr(args, "seed", 42))
    return build_bundle(cfg, scenarios, monte_carlo=monte_carlo)


def _commentary(args: argparse.Namespace, bundle: dict[str, Any]):
    from .ai.commentary import generate_commentary

    return generate_commentary(bundle, getattr(args, "provider", None), getattr(args, "model", None))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_run(args: argparse.Namespace) -> int:
    result = run_fund(_config(args))
    print(_headline(result))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        result.quarterly.to_csv(out / "quarterly_cash_flows.csv", index=False, float_format="%.6f")
        result.deals.to_csv(out / "deals.csv", index=False, float_format="%.6f")
        print(f"\nWrote quarterly_cash_flows.csv and deals.csv to {out}/")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    comparison = analytics.compare_waterfalls(_config(args))
    table = comparison["table"]
    print(_frame(table, {
        "net_irr": _pct, "tvpi": _mult, "lp_distributions": _money, "carry_gross": _money, "clawback_paid": _money,
        "carry_net": _money, "average_carry_timing_years": lambda v: f"{v:.1f}", "carry_present_value": _money,
        "peak_clawback_exposure": _money,
    }))  # fmt: skip
    european, american = table.iloc[0], table.iloc[1]
    print(
        f"\nDeal-by-deal costs LPs {(european['net_irr'] - american['net_irr']) * 1e4:,.0f} bps of net IRR and pays "
        f"the average dollar of carry "
        f"{european['average_carry_timing_years'] - american['average_carry_timing_years']:.1f} years earlier."
    )
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    table, _ = sc.run_scenarios(_config(args), sc.load_scenarios(args.scenarios), interim=False)
    columns = ["label", "waterfall", "gross_moic", "gross_irr", "net_irr", "tvpi", "carry_net", "clawback_paid",
               "total_management_fees", "net_irr_vs_base"]  # fmt: skip
    print(_frame(table[columns], {
        "gross_moic": _mult, "gross_irr": _pct, "net_irr": _pct, "tvpi": _mult, "carry_net": _money,
        "clawback_paid": _money, "total_management_fees": _money,
        "net_irr_vs_base": lambda v: "base" if abs(v) < 1e-12 else f"{v * 1e4:+,.0f} bps",
    }))  # fmt: skip
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        table.to_csv(Path(args.out) / "scenarios.csv", index=False, float_format="%.6f")
    return 0


def cmd_sensitivity(args: argparse.Namespace) -> int:
    cfg = _config(args)
    grids = sc.run_default_grids(cfg)
    for name in ("exit_multiple_x_holding_period__net_irr", "catch_up_x_preferred_return__carry_net"):
        grid = grids[name]
        attrs = grid.attrs
        print(f"\n{attrs['metric_label']}: {attrs['x_label']} (across) against {attrs['y_label']} (down)")
        formatter = _pct if attrs["metric_unit"] == "pct" else _money
        print(grid.map(formatter).to_string())
    tornado = sc.tornado(cfg)
    print(f"\nOne assumption at a time (base net IRR {_pct(tornado.attrs['base'])})")
    print(_frame(tornado[["label", "low_input", "high_input", "low_result", "high_result", "swing"]].rename(
        columns={"label": "Assumption"}), {
        "low_result": _pct, "high_result": _pct, "swing": lambda v: f"{v * 1e4:,.0f} bps",
    }))  # fmt: skip
    print("\nBreakevens: change in exit multiples at which each threshold is reached")
    print(_frame(sc.breakevens(cfg)[["threshold", "exit_multiple_change", "gross_moic", "net_irr", "tvpi"]], {
        "exit_multiple_change": lambda v: f"{v * 100:+.1f}%", "gross_moic": _mult, "net_irr": _pct, "tvpi": _mult,
    }))  # fmt: skip
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, grid in grids.items():
            grid.to_csv(out / f"sensitivity_{name}.csv", float_format="%.6f")
        tornado.to_csv(out / "tornado.csv", index=False, float_format="%.6f")
    return 0


def cmd_montecarlo(args: argparse.Namespace) -> int:
    cfg = _config(args)
    sims = run_monte_carlo(cfg, MonteCarloSettings(n_sims=args.sims, seed=args.seed))
    stats = summarize_monte_carlo(sims)
    print(_table([
        ("Trials", f"{stats['n_sims']:,}"),
        ("Net IRR, median", _pct(stats["net_irr_median"])),
        ("Net IRR, 5th to 95th percentile", f"{_pct(stats['net_irr_p5'])} to {_pct(stats['net_irr_p95'])}"),
        ("Net TVPI, 5th to 95th percentile", f"{_mult(stats['tvpi_p5'])} to {_mult(stats['tvpi_p95'])}"),
        ("Probability net IRR is below the hurdle", _pct(stats["prob_net_irr_below_pref"])),
        ("Probability of LP capital loss", _pct(stats["prob_capital_loss"])),
        ("Carry, mean", _money(stats["carry_mean"])),
        ("Material clawback, deal-by-deal", _pct(stats["prob_clawback_american"])),
        ("Average clawback when triggered", _money(stats["clawback_mean_when_triggered"])),
        ("Material clawback, whole-of-fund", _pct(stats["prob_clawback_european"])),
        ("Average LP IRR cost of deal-by-deal", f"{stats['american_irr_drag_mean'] * 1e4:,.0f} bps"),
    ], "Monte Carlo simulation"))  # fmt: skip
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        sims.to_csv(Path(args.out) / "monte_carlo_trials.csv", index=False, float_format="%.6f")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from .ai.narrator import explain_distribution
    from .timeline import to_date

    result = run_fund(_config(args))
    print(explain_distribution(result, to_date(args.date) if args.date else None))
    return 0


def cmd_commentary(args: argparse.Namespace) -> int:
    cfg = _config(args)
    result = _commentary(args, _bundle(args, cfg))
    document = result.to_markdown()
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(document, encoding="utf-8")
        print(f"Wrote {path}  ({result.grounding.summary()})")
    else:
        print(document)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from .ai.commentary import answer_question
    from .pipeline import build_bundle

    cfg = _config(args)
    scenarios = sc.load_scenarios(args.scenarios) if Path(args.scenarios).is_file() else None
    bundle = build_bundle(cfg, scenarios, monte_carlo=False)
    answer, report = answer_question(bundle, args.question, args.provider, args.model)
    print(answer)
    if report.total:
        print(f"\n[{report.summary()}]")
    return 0


def cmd_excel(args: argparse.Namespace) -> int:
    from .reporting.excel_model import build_workbook

    cfg = _config(args)
    bundle = _bundle(args, cfg)
    commentary = _commentary(args, bundle)
    path = build_workbook(bundle, args.out, commentary=commentary.to_markdown(), source=str(args.config))
    print(f"Wrote {path}")
    print("Open it in Excel to calculate the formulas, then read the Checks sheet.")
    return 0


def cmd_verify_excel(args: argparse.Namespace) -> int:
    from .reporting.excel_verify import read_checks, recalculate

    target = recalculate(args.workbook, args.out)
    report = read_checks(target)
    for check in report["checks"]:
        print(f"{check['status']:<14} {check['item']}")
    print(f"\nFormula errors: {len(report['errors'])}")
    print(f"Overall: {report['overall']}")
    failed = report["errors"] or any(check["status"] == "DIFFERENCE" for check in report["checks"])
    return 1 if failed else 0


def cmd_all(args: argparse.Namespace) -> int:
    from .pipeline import write_outputs
    from .reporting.excel_model import build_workbook

    cfg = _config(args)
    out = Path(args.out)
    print("Running the fund model, scenarios, sensitivities and Monte Carlo ...")
    bundle = _bundle(args, cfg)
    print(_headline(bundle["result"]))
    written = write_outputs(bundle, out, charts=not args.no_charts)
    commentary = _commentary(args, bundle)
    commentary_path = out / "ai_commentary.md"
    commentary_path.write_text(commentary.to_markdown(), encoding="utf-8")
    workbook = build_workbook(bundle, args.excel, commentary=commentary.to_markdown(), source=str(args.config))
    charts = sum(1 for name in written if name.startswith("chart_"))
    print(f"\nWrote {len(written) - charts} tables and {charts} charts to {out}/")
    print(f"Wrote {commentary_path}  ({commentary.grounding.summary()})")
    print(f"Wrote {workbook}")
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pefund", description="Private equity fund waterfall and carry analytics.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )  # fmt: skip
    parser.add_argument("--version", action="version", version=f"pefund {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=DEFAULT_CONFIG, help="fund configuration (.yaml, .json or the Excel model)")
        p.add_argument("--set", action="append", metavar="KEY=VALUE",
                       help="override an assumption, e.g. --set waterfall.carry_rate=0.25 (repeatable)")  # fmt: skip

    def analysis(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scenarios", default=DEFAULT_SCENARIOS, help="scenario definitions (.yaml)")
        p.add_argument("--sims", type=int, default=2000, help="Monte Carlo trials")
        p.add_argument("--seed", type=int, default=42, help="Monte Carlo random seed")
        p.add_argument("--no-monte-carlo", action="store_true", help="skip the Monte Carlo simulation")

    def model(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", choices=["anthropic", "openai", "offline"],
                       help="commentary back-end (default: detected from the environment)")  # fmt: skip
        p.add_argument("--model", help="model name (default: PEFUND_LLM_MODEL)")

    p = sub.add_parser("run", help="run the fund and print headline results")
    common(p)
    p.add_argument("--out", help="directory for the quarterly and deal CSVs")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("compare", help="whole-of-fund against deal-by-deal")
    common(p)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("scenarios", help="run every named scenario")
    common(p)
    p.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    p.add_argument("--out")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("sensitivity", help="two-way tables, tornado and breakevens")
    common(p)
    p.add_argument("--out")
    p.set_defaults(func=cmd_sensitivity)

    p = sub.add_parser("montecarlo", help="simulate deal outcomes")
    common(p)
    p.add_argument("--sims", type=int, default=2000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out")
    p.set_defaults(func=cmd_montecarlo)

    p = sub.add_parser("explain", help="walk through the waterfall for one quarter")
    common(p)
    p.add_argument("--date", help="quarter to explain, e.g. 2029-12-31 (default: first carry payment)")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("commentary", help="write grounded commentary")
    common(p)
    analysis(p)
    model(p)
    p.add_argument("--out", help="Markdown file to write (default: print)")
    p.set_defaults(func=cmd_commentary)

    p = sub.add_parser("ask", help="ask a question about the fund")
    common(p)
    model(p)
    p.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    p.add_argument("question")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("excel", help="build the Excel model")
    common(p)
    analysis(p)
    model(p)
    p.add_argument("--out", default="excel/PE_Fund_Waterfall_Model.xlsx")
    p.set_defaults(func=cmd_excel)

    p = sub.add_parser("verify-excel", help="recalculate a workbook with LibreOffice and read its Checks sheet")
    p.add_argument("workbook")
    p.add_argument("--out", help="write the recalculated copy here instead of in place")
    p.set_defaults(func=cmd_verify_excel)

    p = sub.add_parser("all", help="run everything: tables, charts, Excel model and commentary")
    common(p)
    analysis(p)
    model(p)
    p.add_argument("--out", default="outputs")
    p.add_argument("--excel", default="excel/PE_Fund_Waterfall_Model.xlsx")
    p.add_argument("--no-charts", action="store_true")
    p.set_defaults(func=cmd_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"File not found: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
