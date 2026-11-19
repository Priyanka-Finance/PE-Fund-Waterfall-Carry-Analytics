"""Command-line interface and the output pipeline."""

from __future__ import annotations

import json

import pytest

from pefund import scenarios as sc
from pefund.cli import main
from pefund.montecarlo import MonteCarloSettings
from pefund.pipeline import build_bundle, write_outputs


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """The CLI tests never call a language model."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "PEFUND_LLM_PROVIDER", "PEFUND_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_run_prints_headline_results(capsys, tmp_path):
    code, out, _ = run(capsys, "run", "--out", str(tmp_path))
    assert code == 0
    assert "Net IRR" in out and "15.2%" in out and "1.83x" in out and "Dec 2029" in out
    assert (tmp_path / "quarterly_cash_flows.csv").exists() and (tmp_path / "deals.csv").exists()


def test_set_overrides_assumptions(capsys):
    _, out, _ = run(capsys, "run", "--set", "waterfall.type=american", "--set", "waterfall.carry_rate=0.25")
    assert "deal-by-deal (American)" in out and "carry 25%" in out


def test_configuration_errors_exit_with_a_message(capsys):
    code, _, err = run(capsys, "run", "--set", "waterfall.catch_up_rate=0.1")
    assert code == 2 and "Configuration error" in err and "can never catch up" in err
    code, _, err = run(capsys, "run", "--config", "does/not/exist.yaml")
    assert code == 2 and "not found" in err
    code, _, err = run(capsys, "run", "--set", "nonsense")
    assert code == 2 and "key=value" in err


def test_compare_and_scenarios_commands(capsys):
    code, out, _ = run(capsys, "compare")
    assert code == 0 and "Whole-of-fund (European)" in out and "68 bps" in out
    code, out, _ = run(capsys, "scenarios")
    assert code == 0 and "Late losses, deal-by-deal" in out and "base" in out


def test_explain_and_ask_commands(capsys):
    code, out, _ = run(capsys, "explain", "--date", "2029-12-31")
    assert code == 0 and "GP catch-up: $36.01m" in out
    code, out, _ = run(capsys, "ask", "What is the net IRR?")
    assert code == 0 and "net_irr_pct" in out


def test_montecarlo_command(capsys, tmp_path):
    code, out, _ = run(capsys, "montecarlo", "--sims", "40", "--seed", "1", "--out", str(tmp_path))
    assert code == 0 and "Monte Carlo simulation" in out and "Trials" in out
    assert (tmp_path / "monte_carlo_trials.csv").exists()


def test_commentary_command_writes_markdown(capsys, tmp_path):
    target = tmp_path / "commentary.md"
    code, out, _ = run(capsys, "commentary", "--no-monte-carlo", "--out", str(target))
    assert code == 0 and "(100%)" in out
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Fund commentary") and "## Executive summary" in text


def test_all_command_produces_tables_workbook_and_commentary(capsys, tmp_path):
    out_dir, workbook = tmp_path / "out", tmp_path / "model.xlsx"
    code, out, _ = run(capsys, "all", "--out", str(out_dir), "--excel", str(workbook), "--sims", "40", "--no-charts")
    assert code == 0, out
    for name in ("quarterly_cash_flows.csv", "deals.csv", "scenarios.csv", "tornado.csv", "breakevens.csv",
                 "gross_to_net_bridge.csv", "value_split.csv", "monte_carlo_summary.csv", "summary.json",
                 "ai_commentary.md", "sensitivity_exit_multiple_x_holding_period__net_irr.csv"):  # fmt: skip
        assert (out_dir / name).exists(), name
    summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["at_liquidation"]["net_irr"] == pytest.approx(0.1522, abs=1e-4)
    assert summary["at_valuation_date"]["as_of"] == "2026-09-30"
    assert workbook.exists() and workbook.stat().st_size > 50_000


def test_charts_are_rendered(base_config, tmp_path):
    bundle = build_bundle(base_config, sc.load_scenarios("configs/scenarios.yaml"),
                          monte_carlo=MonteCarloSettings(n_sims=60, seed=2))  # fmt: skip
    written = write_outputs(bundle, tmp_path)
    charts = [path for name, path in written.items() if name.startswith("chart_")]
    assert len(charts) >= 15
    for path in charts:
        assert path.suffix == ".png" and path.stat().st_size > 15_000, path


def test_cli_reads_the_excel_model_as_its_configuration(capsys, tmp_path):
    workbook = tmp_path / "model.xlsx"
    assert run(capsys, "excel", "--out", str(workbook), "--no-monte-carlo")[0] == 0
    code, out, _ = run(capsys, "run", "--config", str(workbook))
    assert code == 0 and "15.2%" in out
