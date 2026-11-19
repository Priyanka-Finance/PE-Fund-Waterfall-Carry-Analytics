"""Regenerate the results block in README.md from the base case.

    python scripts/update_readme.py

``tests/test_readme.py`` fails when the block is out of date.
"""

from pathlib import Path

from pefund import load_config
from pefund.pipeline import build_bundle
from pefund.reporting.results_block import update_readme
from pefund.scenarios import load_scenarios

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    cfg = load_config(ROOT / "configs" / "base_case.yaml")
    scenarios = load_scenarios(ROOT / "configs" / "scenarios.yaml")
    bundle = build_bundle(cfg, scenarios, sensitivities=False, monte_carlo=False)
    changed = update_readme(ROOT / "README.md", bundle)
    print("README.md updated" if changed else "README.md is already up to date")


if __name__ == "__main__":
    main()
