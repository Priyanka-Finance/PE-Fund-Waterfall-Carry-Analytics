"""Configuration loading, validation and overrides."""

from __future__ import annotations

import copy
import datetime as dt
import json

import pytest
import yaml

from pefund import ConfigError, FundConfig, load_config
from pefund.timeline import add_quarters, quarter_end, quarter_grid, quarter_index, to_date, year_fractions


def test_base_case_loads_and_round_trips(base_config):
    assert len(base_config.deals) == 12
    assert base_config.fund.lp_commitment == pytest.approx(490.0)
    assert base_config.fund.investment_period_quarters == 20
    again = FundConfig.from_dict(base_config.to_dict())
    assert again.to_dict() == base_config.to_dict()
    json.dumps(base_config.to_dict())  # plain types only


def test_json_and_yaml_files_are_equivalent(base_config, tmp_path):
    as_json = tmp_path / "fund.json"
    as_json.write_text(json.dumps(base_config.to_dict()), encoding="utf-8")
    as_yaml = tmp_path / "fund.yml"
    as_yaml.write_text(yaml.safe_dump(base_config.to_dict()), encoding="utf-8")
    assert load_config(as_json).to_dict() == load_config(as_yaml).to_dict() == base_config.to_dict()
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")
    (tmp_path / "fund.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="Unsupported"):
        load_config(tmp_path / "fund.txt")


def test_overrides_are_merged_without_touching_the_original(base_config):
    changed = base_config.with_overrides({"waterfall": {"carry_rate": 0.25}, "shocks": {"hold_shift_quarters": 4}})
    assert changed.waterfall.carry_rate == 0.25 and changed.shocks.hold_shift_quarters == 4
    assert changed.waterfall.pref_rate == base_config.waterfall.pref_rate  # the rest of the section is kept
    assert base_config.waterfall.carry_rate == 0.20
    assert base_config.with_overrides(None) is base_config
    dotted = base_config.with_values({"waterfall.type": "american", "clawback.escrow_pct": 0.3})
    assert dotted.waterfall.type == "american" and dotted.clawback.escrow_pct == 0.3


def edit(base_config, mutate) -> dict:
    raw = copy.deepcopy(base_config.to_dict())
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r["waterfall"].update(carry=0.2), "Unknown key"),
        (lambda r: r.update(waterfal={}), "Unknown top-level section"),
        (lambda r: r["waterfall"].update(type="asian"), "waterfall.type must be one of"),
        (lambda r: r["waterfall"].update(catch_up_rate=0.15), "can never catch up"),
        (lambda r: r["waterfall"].update(pref_rate=8), "pref_rate must be in"),
        (lambda r: r["management_fee"].update(basis_investment_period="gross"), "must be one of"),
        (lambda r: r["fund"].update(committed_capital=-1), "must be positive"),
        (lambda r: r["fund"].update(term_years=3), "at least the investment period"),
        (lambda r: r["fund"].update(investment_period_years=4.1), "whole number of quarters"),
        (lambda r: r["investors"][0].update(commitment=1.0), "Investor commitments sum to"),
        (lambda r: r["deals"][1].update(name=r["deals"][0]["name"]), "must be unique"),
        (lambda r: r["deals"][0].update(gross_moic=2.0), "exactly one of"),
        (lambda r: r["deals"][0].pop("operating"), "exactly one of"),
        (lambda r: r["deals"][0].update(exit_date="2022-03-31"), "before the exit"),
        (lambda r: r["deals"][0].update(exit_date="2040-03-31"), "after the fund term"),
        (
            lambda r: (
                r["deals"][0]["investments"][0].update(date="2029-06-30"),
                r["deals"][0].update(exit_date="2031-06-30"),
            ),
            "within the investment period",
        ),
        (lambda r: r["deals"][0]["investments"][0].update(amount=500.0), "exceeds entry equity value"),
        (lambda r: r["deals"][0]["operating"].update(entry_leverage=12.0), "below the entry EV multiple"),
        (lambda r: r["deals"][2]["proceeds_events"][0].update(share=1.2), "between 0 and 1"),
        (lambda r: r["deals"][2]["proceeds_events"][0].pop("stake_sold"), "need stake_sold"),
        (lambda r: r["shocks"].update(deal_overrides={"Project Nobody": {"write_off": True}}), "unknown deal"),
        (lambda r: r["shocks"].update(deal_overrides={"Project Atlas": {"moic": 2}}), "Unknown key"),
        (lambda r: r.update(deals=[]), "At least one deal"),
        (lambda r: r["subscription_line"].update(quarters_outstanding=0), "between 1 and 8"),
    ],
)
def test_invalid_configurations_are_rejected_with_a_reason(base_config, mutate, message):
    with pytest.raises(ConfigError, match=message):
        FundConfig.from_dict(edit(base_config, mutate))


def test_dates_snap_to_quarter_ends(base_config):
    raw = edit(base_config, lambda r: r["deals"][0]["investments"][0].update(date="2022-05-17"))
    assert FundConfig.from_dict(raw).deals[0].entry_date == dt.date(2022, 6, 30)


def test_quarter_calendar_helpers():
    assert quarter_end(dt.date(2024, 2, 10)) == dt.date(2024, 3, 31)
    assert quarter_end(dt.date(2024, 12, 31)) == dt.date(2024, 12, 31)
    assert add_quarters(dt.date(2023, 12, 31), 1) == dt.date(2024, 3, 31)
    assert add_quarters(dt.date(2024, 3, 31), -5) == dt.date(2022, 12, 31)
    assert quarter_index(dt.date(2022, 3, 31), dt.date(2024, 9, 30)) == 10
    grid = quarter_grid(dt.date(2023, 12, 31), 3)
    assert grid == [dt.date(2023, 12, 31), dt.date(2024, 3, 31), dt.date(2024, 6, 30)]
    assert year_fractions(grid).tolist() == pytest.approx([0.0, 91 / 365, 182 / 365])  # leap-year February
    assert to_date("2024-06-30T00:00:00") == dt.date(2024, 6, 30)
    assert to_date(dt.datetime(2024, 6, 30, 12)) == dt.date(2024, 6, 30)
    with pytest.raises(TypeError):
        to_date(20240630)
