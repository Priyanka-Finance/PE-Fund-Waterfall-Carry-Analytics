"""The results quoted in README.md are generated from the model and must stay current."""

from pathlib import Path

import pytest

from pefund import load_config
from pefund.pipeline import build_bundle
from pefund.reporting.results_block import END, START, current_block, results_markdown, update_readme
from pefund.scenarios import load_scenarios

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bundle():
    cfg = load_config(ROOT / "configs" / "base_case.yaml")
    scenarios = load_scenarios(ROOT / "configs" / "scenarios.yaml")
    return build_bundle(cfg, scenarios, sensitivities=False, monte_carlo=False)


def test_readme_results_are_current(bundle):
    assert current_block(ROOT / "README.md") == results_markdown(bundle), (
        "README.md is out of date: run `python scripts/update_readme.py`"
    )


def test_block_reports_headline_figures(bundle):
    block = results_markdown(bundle)
    summary = bundle["summary"]
    assert block.startswith(START) and block.endswith(END)
    assert f"{summary['net_irr']:.1%} / {summary['tvpi']:.2f}x" in block
    assert "Late losses, deal-by-deal" in block


def test_update_rewrites_only_the_block(tmp_path, bundle):
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n\nBefore.\n\n{START}\nstale\n{END}\n\nAfter.\n", encoding="utf-8")
    assert update_readme(readme, bundle) is True
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("# Title\n\nBefore.\n\n") and text.endswith("\n\nAfter.\n")
    assert "stale" not in text and current_block(readme) == results_markdown(bundle)
    assert update_readme(readme, bundle) is False  # second run is a no-op


def test_block_without_scenarios_omits_the_scenario_table(bundle):
    assert "**Scenarios**" not in results_markdown({**bundle, "scenario_table": None})


def test_python_example_in_the_readme_runs(monkeypatch, capsys):
    """The scenario-engine snippet is executed as written, from the repository root."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    snippet = text.split("```python\n", 1)[1].split("```", 1)[0]
    monkeypatch.chdir(ROOT)
    exec(compile(snippet, "README.md", "exec"), {})  # our own documentation, not external input
    printed = capsys.readouterr().out.strip().splitlines()
    assert 0.0 < float(printed[0]) < 0.15  # the downside scenario's net IRR
    assert float(printed[-1]) == pytest.approx(-0.258, abs=0.001)  # breakeven against the 8% hurdle
