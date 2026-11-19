"""The Excel model: structure, round-trip as an input file, and reconciliation with Python."""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from pefund import FundConfig, load_config, run_fund
from pefund import scenarios as sc
from pefund.pipeline import build_bundle
from pefund.reporting import excel_model as xm
from pefund.reporting.excel_inputs import read_config_workbook
from pefund.reporting.excel_verify import libreoffice_binary, read_checks, recalculate

needs_libreoffice = pytest.mark.skipif(libreoffice_binary() is None, reason="LibreOffice is not installed")


@pytest.fixture(scope="module")
def bundle(base_config):
    return build_bundle(base_config, None, sensitivities=False, monte_carlo=False)


@pytest.fixture(scope="module")
def workbook_path(bundle, tmp_path_factory):
    path = tmp_path_factory.mktemp("excel") / "model.xlsx"
    return xm.build_workbook(bundle, path, commentary="# Heading\nBody text.")


def test_workbook_has_every_sheet_in_order(workbook_path):
    names = load_workbook(workbook_path).sheetnames
    assert names[:3] == [xm.S_README, xm.S_DASH, xm.S_INPUTS]
    for sheet in (xm.S_DEALS, xm.S_CASH, xm.S_EU, xm.S_DBD, xm.S_DBD_IN, xm.S_FLOWS, xm.S_RETURNS, xm.S_ANNUAL,
                  xm.S_LPS, xm.S_BRIDGE, xm.S_AI, xm.S_CHECKS):  # fmt: skip
        assert sheet in names
    assert names[-1] == xm.S_CHECKS


def test_waterfall_sheets_are_formulas_not_pasted_values(workbook_path, bundle):
    wb = load_workbook(workbook_path)
    last = xm.ROW0 + bundle["result"].n_quarters - 1
    for sheet in (xm.S_EU, xm.S_DBD):
        ws = wb[sheet]
        for column in ("t1", "t2", "t3", "t4_gp", "lp", "gp", "sh_cap", "gp_cash"):
            for row in (xm.ROW0 + 1, last):
                value = ws[f"{xm.WF[column]}{row}"].value
                assert isinstance(value, str) and value.startswith("="), (sheet, column, row, value)
    # The levers are referenced by name, never typed into a formula.
    tier4 = wb[xm.S_EU][f"{xm.WF['t4_gp']}{last}"].value
    assert "carry_rate" in tier4 and "0.2" not in tier4


def test_defined_names_point_at_the_levers(workbook_path, base_config):
    wb = load_workbook(workbook_path)
    for name in ("pref_rate", "carry_rate", "catch_up", "pref_comp", "wf_type", "escrow_pct", "cb_enabled",
                 "cb_after_tax", "cb_tax_rate", "comp_m", "lp_commitment", "checks_status"):  # fmt: skip
        assert name in wb.defined_names, name
    sheet, cell = wb.defined_names["pref_rate"].attr_text.split("!")
    assert wb[sheet][cell.replace("$", "")].value == base_config.waterfall.pref_rate


def test_inputs_follow_the_colour_convention(workbook_path):
    ws = load_workbook(workbook_path)[xm.S_INPUTS]
    rows = {row[5].value: row for row in ws.iter_rows(min_row=xm.INPUT_FIRST_ROW) if row[5].value}
    carry, fund_size = rows["waterfall.carry_rate"][1], rows["fund.committed_capital"][1]
    assert carry.font.color.rgb.endswith("0000FF") and fund_size.font.color.rgb.endswith("0000FF")  # blue inputs
    assert carry.fill.start_color.rgb.endswith("FFFF00")  # a live lever is highlighted
    assert not str(fund_size.fill.start_color.rgb).endswith("FFFF00")  # an engine input is not
    assert carry.font.name == "Arial"


def test_workbook_round_trips_as_an_input_file(workbook_path, base_config):
    restored = FundConfig.from_dict(read_config_workbook(workbook_path))
    assert restored.to_dict() == base_config.to_dict()
    assert load_config(workbook_path).to_dict() == base_config.to_dict()
    assert run_fund(restored).summary["net_irr"] == pytest.approx(run_fund(base_config).summary["net_irr"])


def _edit_inputs(source, target, changes: dict) -> None:
    wb = load_workbook(source)
    for row in wb[xm.S_INPUTS].iter_rows(min_row=xm.INPUT_FIRST_ROW):
        if row[5].value in changes:
            row[1].value = changes[row[5].value]
    wb.save(target)


def test_edited_inputs_are_picked_up(workbook_path, tmp_path):
    edited = tmp_path / "edited.xlsx"
    _edit_inputs(workbook_path, edited, {"waterfall.carry_rate": 0.25, "management_fee.rate_investment_period": 0.015,
                                         "subscription_line.enabled": True})  # fmt: skip
    cfg = load_config(edited)
    assert cfg.waterfall.carry_rate == 0.25
    assert cfg.management_fee.rate_investment_period == 0.015
    assert cfg.subscription_line.enabled is True


def test_an_inconsistent_edit_is_rejected_with_a_clear_message(workbook_path, tmp_path):
    """Raising the fund size without updating the LP register must not run silently."""
    edited = tmp_path / "inconsistent.xlsx"
    _edit_inputs(workbook_path, edited, {"fund.committed_capital": 600.0})
    with pytest.raises(Exception, match="Investor commitments sum to"):
        load_config(edited)


def test_too_many_events_for_the_template_is_reported(base_config):
    raw = base_config.to_dict()
    raw["deals"][0]["proceeds_events"] = [
        {"date": "2024-06-30", "kind": "dividend_recap", "share": 0.1},
        {"date": "2024-12-31", "kind": "dividend_recap", "share": 0.1},
        {"date": "2025-06-30", "kind": "dividend_recap", "share": 0.1},
    ]
    bundle = build_bundle(FundConfig.from_dict(raw), None, sensitivities=False, monte_carlo=False)
    with pytest.raises(Exception, match="interim events"):
        xm.build_workbook(bundle, "unused.xlsx")


# --------------------------------------------------------------------------- #
# Independent recalculation with LibreOffice
# --------------------------------------------------------------------------- #
@needs_libreoffice
def test_excel_formulas_reconcile_to_the_python_engine(workbook_path, tmp_path):
    out = read_checks(recalculate(workbook_path, tmp_path / "recalculated.xlsx"))
    assert out["errors"] == []
    assert len(out["checks"]) >= 30
    assert [c for c in out["checks"] if c["status"] != "OK"] == []
    assert out["overall"] == "All checks pass"


@needs_libreoffice
@pytest.mark.parametrize(
    "levers",
    [
        {"waterfall.type": "american", "waterfall.pref_compounding": "quarterly", "clawback.escrow_pct": 0.3},
        {"waterfall.pref_rate": 0.10, "waterfall.carry_rate": 0.25, "waterfall.catch_up_rate": 0.8},
        {"waterfall.pref_compounding": "simple", "waterfall.catch_up_rate": 0.0},
    ],
)
def test_changing_levers_in_excel_matches_a_python_rerun(base_config, tmp_path, levers):
    """Start from a loss-making fund so that the clawback and the escrow are exercised too."""
    scenario = sc.load_scenarios("configs/scenarios.yaml")["late_losses_deal_by_deal"]["overrides"]
    cfg = base_config.with_overrides(scenario)
    path = xm.build_workbook(build_bundle(cfg, None, sensitivities=False, monte_carlo=False), tmp_path / "model.xlsx")
    wb = load_workbook(path)
    for row in wb[xm.S_INPUTS].iter_rows(min_row=xm.INPUT_FIRST_ROW):
        if row[5].value in levers:
            row[1].value = levers[row[5].value]
    wb.save(path)
    recalculate(path)

    values = load_workbook(path, data_only=True)
    n = run_fund(cfg, interim=False).n_quarters
    anchor = xm.ROW0 + n - 1 + 3
    for sheet, kind in ((xm.S_EU, "european"), (xm.S_DBD, "american")):
        expected = run_fund(cfg.with_values({**levers, "waterfall.type": kind}), interim=False).totals
        ws = values[sheet]

        def cell(key: str, ws=ws) -> float:
            return ws[f"E{anchor + xm.SUMMARY[key]}"].value

        assert cell("carry") == pytest.approx(expected["carry_gross"], abs=1e-7)
        assert cell("uncapped") == pytest.approx(expected["clawback_uncapped"], abs=1e-7)
        assert cell("payable") == pytest.approx(expected["clawback_paid"], abs=1e-7)
        assert cell("lp_incl") == pytest.approx(expected["total_lp_distributions"], abs=1e-7)
        assert cell("shortfall") == pytest.approx(expected["clawback_shortfall"], abs=1e-7)
    status = read_checks(path)
    assert status["errors"] == []
    assert status["overall"].startswith("Levers changed")  # the workbook says the Python values are now stale
