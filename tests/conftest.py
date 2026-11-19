"""Shared fixtures: the sample fund and a builder for tiny hand-checkable funds."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from pefund import FundConfig, load_config

ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = ROOT / "configs" / "base_case.yaml"

#: A fund with no fees, no expenses and no GP commitment, so every number in a
#: test can be reproduced with a pocket calculator.
PLAIN_FUND = {
    "fund": {
        "name": "Test Fund",
        "first_close": "2020-12-31",
        "committed_capital": 1000.0,
        "gp_commitment_pct": 0.0,
        "investment_period_years": 5,
        "term_years": 10,
        "max_extension_years": 2,
    },
    "management_fee": {"rate_investment_period": 0.0, "rate_post_investment_period": 0.0},
    "expenses": {"organizational": 0.0, "annual_operating": 0.0},
    "clawback": {"enabled": True, "after_tax_limit": False},
    "deals": [],
}


def make_config(deals: list[dict], **sections: dict) -> FundConfig:
    """Build a plain fund from a list of deals plus optional section overrides."""
    raw = copy.deepcopy(PLAIN_FUND)
    raw["deals"] = deals
    for section, values in sections.items():
        raw.setdefault(section, {}).update(values)
    return FundConfig.from_dict(raw)


def one_deal(amount: float = 100.0, moic: float = 2.0, entry: str = "2020-12-31", exit: str = "2025-12-31") -> dict:
    return {"name": "Solo", "investments": [{"date": entry, "amount": amount}], "exit_date": exit, "gross_moic": moic}


@pytest.fixture(scope="session")
def base_config() -> FundConfig:
    return load_config(BASE_CONFIG)
