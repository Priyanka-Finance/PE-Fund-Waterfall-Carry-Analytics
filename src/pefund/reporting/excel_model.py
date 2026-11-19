"""Build the Excel model: a live-formula waterfall that reconciles to the Python engine.

Division of labour
------------------
* **Python** produces what depends on the deal schedule and the funding
  mechanics: quarterly investments and proceeds, management fees, capital calls
  and the cash distributable to the LP class.  Those arrive as values.
* **Excel** computes everything downstream of that with live formulas: both
  waterfalls tier by tier, escrow, the clawback test, LP and GP cash flows,
  IRR and multiples.  Changing the preferred return, carry, catch-up,
  compounding, escrow or clawback terms on the ``Inputs`` sheet recalculates
  the whole workbook.

Two different calculation methods are used on purpose.  The whole-of-fund sheet
is the textbook running ledger; the deal-by-deal sheet uses the engine's
cumulative-hurdle formula with eligibility weights.  The ``Checks`` sheet
compares every total with the Python run, so the workbook doubles as an
independent verification of the engine.

The ``Inputs``, ``Deals`` and ``Investors`` sheets are also a complete input
file: ``pefund run --config <workbook>.xlsx`` reads them back.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from ..config import (
    EXPENSE_ALLOCATION,
    FEE_BASES,
    PREF_COMPOUNDING,
    PROCEEDS_KINDS,
    WATERFALL_TYPES,
    ConfigError,
    FundConfig,
)
from ..fund import FundResult

# --------------------------------------------------------------------------- #
# Layout constants
# --------------------------------------------------------------------------- #
ROW0 = 8  # first quarter row on every quarterly sheet, so row r is the same quarter everywhere
MAX_INVESTMENTS, MAX_EVENTS = 3, 2

S_README, S_DASH, S_INPUTS, S_DEALS, S_INVESTORS = "README", "Dashboard", "Inputs", "Deals", "Investors"
S_CASH, S_EU, S_DBD, S_DBD_IN = "CashFlows", "WF_WholeFund", "WF_DealByDeal", "DbD_Inputs"
S_FLOWS, S_RETURNS, S_ANNUAL, S_LPS = "LP_GP_Flows", "Returns", "Annual", "LP_Accounts"
S_BRIDGE, S_SCEN, S_SENS, S_MC, S_AI, S_CHECKS = (
    "Bridge", "Scenarios", "Sensitivity", "MonteCarlo", "AI_Commentary", "Checks",
)  # fmt: skip


def _letters(names: list[str]) -> dict[str, str]:
    return {name: get_column_letter(i + 1) for i, name in enumerate(names)}


#: Column of each field on the CashFlows sheet.
CF = _letters([
    "q", "date", "year", "inv", "proc", "relief", "cost_out", "fv", "fee_basis", "fee_rate", "fee_gross",
    "fee_check", "fee_offset", "fee_net", "expenses", "line_int", "lp_called", "gp_called", "netted",
    "lp_cash", "gp_dist", "lp_unfunded",
])  # fmt: skip

#: Column of each field on both waterfall sheets (identical from ``cap_out`` onwards).
WF = _letters([
    "q", "date", "days", "called", "cash", "m1", "m2", "m3", "m4", "cap_out", "pref_out", "t1", "t2", "rem",
    "cu_need", "t3", "t3_gp", "t3_lp", "t4", "t4_gp", "t4_lp", "lp", "gp", "check", "cum_profit", "cum_carry",
    "cap_after", "pref_after", "escrowed", "escrow_bal", "sh_accr", "sh_cap", "sh_pref", "cb_recv", "lp_incl",
    "gp_cash",
])  # fmt: skip

#: Column of each field on the LP_GP_Flows sheet.
FL = _letters([
    "q", "date", "year", "called", "dist", "net", "cum_net", "cum_called", "cum_dist", "dpi", "t1", "t2",
    "t3_gp", "t3_lp", "t4_lp", "t4_gp", "carry", "carry_cash", "gp_called", "gp_dist", "fee", "gross",
])  # fmt: skip

#: Offsets of the liquidation / clawback block rows below each waterfall table.
SUMMARY = {
    "called": 1, "lp_before": 2, "carry": 3, "profit": 4, "test_a": 5, "pref_accrued": 6, "test_b": 7,
    "uncapped": 8, "cap": 9, "payable": 10, "escrow": 11, "from_escrow": 12, "from_gp": 13, "released": 14,
    "carry_net": 15, "lp_incl": 16, "shortfall": 17,
}  # fmt: skip

# --------------------------------------------------------------------------- #
# Styles (financial-model conventions)
# --------------------------------------------------------------------------- #
FONT = "Arial"
BLUE, BLACK, GREEN, GREY, WHITE = "0000FF", "000000", "008000", "595959", "FFFFFF"
NAVY, LIGHT, YELLOW, PALE = "1F3864", "F2F2F2", "FFFF00", "DDEBF7"

F_INPUT = Font(name=FONT, size=10, color=BLUE)
F_FORMULA = Font(name=FONT, size=10, color=BLACK)
F_LINK = Font(name=FONT, size=10, color=GREEN)
F_PYTHON = Font(name=FONT, size=10, color=GREY)
F_LABEL = Font(name=FONT, size=10, color=BLACK)
F_BOLD = Font(name=FONT, size=10, color=BLACK, bold=True)
F_HEADER = Font(name=FONT, size=10, color=WHITE, bold=True)
F_TITLE = Font(name=FONT, size=14, color=NAVY, bold=True)
F_SECTION = Font(name=FONT, size=11, color=NAVY, bold=True)
F_NOTE = Font(name=FONT, size=9, color=GREY, italic=True)
F_KPI = Font(name=FONT, size=16, color=NAVY, bold=True)

FILL_HEADER = PatternFill("solid", start_color=NAVY)
FILL_SUB = PatternFill("solid", start_color=PALE)
FILL_KEY = PatternFill("solid", start_color=YELLOW)
FILL_TOTAL = PatternFill("solid", start_color=LIGHT)
THIN = Side(style="thin", color="BFBFBF")
TOP_BORDER = Border(top=THIN)

MONEY = '#,##0.0;(#,##0.0);"-"'
MONEY2 = '#,##0.00;(#,##0.00);"-"'
PCT = '0.0%;(0.0%);"-"'
PCT2 = '0.00%;(0.00%);"-"'
MULT = '0.00"x";(0.00"x");"-"'
DATE = "dd-mmm-yy"
INDEX = "0"
YEARS = "0.0"
BPS = '#,##0;(#,##0);"-"'

WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
MIDDLE = Alignment(horizontal="center")
RIGHT = Alignment(horizontal="right")
INDENT = Alignment(horizontal="left", indent=1)

KIND_FONT = {"input": F_INPUT, "formula": F_FORMULA, "link": F_LINK, "python": F_PYTHON, "label": F_LABEL,
             "bold": F_BOLD, "note": F_NOTE, "section": F_SECTION, "title": F_TITLE}  # fmt: skip


def _put(ws: Worksheet, ref: str, value: Any, kind: str = "label", fmt: str | None = None,
         fill: PatternFill | None = None, align: Alignment | None = None, bold: bool = False) -> None:  # fmt: skip
    """Write one cell with the font for its role (input / formula / link / python value / label)."""
    cell = ws[ref]
    if isinstance(value, (np.floating, np.integer)):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        value = None
    if isinstance(value, pd.Timestamp):
        value = value.date()
    cell.value = value
    font = KIND_FONT[kind]
    cell.font = Font(name=font.name, size=font.size, color=font.color, bold=True, italic=font.italic) if bold else font
    if fmt:
        cell.number_format = fmt
    if fill:
        cell.fill = fill
    if align:
        cell.alignment = align


def _header(ws: Worksheet, row: int, labels: list[str], start_col: int = 1, height: float = 42.0) -> None:
    for offset, label in enumerate(labels):
        cell = ws.cell(row, start_col + offset, label)
        cell.font, cell.fill, cell.alignment = F_HEADER, FILL_HEADER, CENTER
    ws.row_dimensions[row].height = height


def _widths(ws: Worksheet, widths: dict[str, float]) -> None:
    for column, width in widths.items():
        ws.column_dimensions[column].width = width


def _title(ws: Worksheet, title: str, subtitle: str) -> None:
    _put(ws, "A1", title, "title")
    _put(ws, "A2", subtitle, "note")
    ws.row_dimensions[1].height = 24


def _total_row(ws: Worksheet, row: int, first: int, last: int, columns: dict[str, str], label_col: str = "A") -> None:
    """Totals row: ``columns`` maps a column letter to its number format."""
    _put(ws, f"{label_col}{row}", "Total", "bold", fill=FILL_TOTAL)
    for column, fmt in columns.items():
        _put(ws, f"{column}{row}", f"=SUM({column}{first}:{column}{last})", "formula", fmt, FILL_TOTAL, bold=True)
        ws[f"{column}{row}"].border = TOP_BORDER


def _name(wb: Workbook, name: str, sheet: str, ref: str) -> None:
    column = "".join(ch for ch in ref if ch.isalpha())
    row = "".join(ch for ch in ref if ch.isdigit())
    wb.defined_names[name] = DefinedName(name, attr_text=f"{sheet}!${column}${row}")


# --------------------------------------------------------------------------- #
# Inputs sheet (also the machine-readable configuration)
# --------------------------------------------------------------------------- #
LIVE, PYTHON = "Excel formulas (live)", "Python engine (re-run)"

#: (config key, label, kind, defined name, note).  A ``None`` key starts a section.
INPUT_ROWS: list[tuple[str | None, str, str, str | None, str]] = [
    (None, "Fund", "", None, ""),
    ("fund.name", "Fund name", "text", None, "Hypothetical fund used for demonstration."),
    ("fund.currency", "Currency", "text", None, "Reporting currency."),
    ("fund.unit", "Unit", "text", None, "m = millions. All money amounts in this workbook use this unit."),
    ("fund.first_close", "First close", "date", None, "Quarter 0 of the model. Snaps to a quarter-end."),
    ("fund.committed_capital", "Committed capital", "money", "committed_capital",
     "Total commitments including the GP's own commitment."),
    ("fund.gp_commitment_pct", "GP commitment", "pct", "gp_pct",
     "Share of commitments made by the GP. Fee-free and carry-free."),
    ("fund.investment_period_years", "Investment period (years)", "years", None,
     "Fees are charged on commitments until it ends."),
    ("fund.term_years", "Fund term (years)", "years", None, "Before extensions."),
    ("fund.max_extension_years", "Maximum extensions (years)", "years", None,
     "Exits are forced by the end of the extended term."),
    ("fund.valuation_date", "Valuation date", "date", None, "As-of date for the interim snapshot."),
    (None, "Management fee", "", None, ""),
    ("management_fee.rate_investment_period", "Fee rate, investment period", "pct", None, "Per annum, charged quarterly in advance."),
    ("management_fee.basis_investment_period", "Fee basis, investment period", "choice:" + ",".join(FEE_BASES), None,
     "committed = LP commitments; net_invested = cost of unrealised deals; nav = fair value."),
    ("management_fee.rate_post_investment_period", "Fee rate, after investment period", "pct", None, "Step-down rate."),
    ("management_fee.basis_post_investment_period", "Fee basis, after investment period",
     "choice:" + ",".join(FEE_BASES), None, "Usually net_invested."),
    ("management_fee.offset_pct", "Fee offset", "pct", None,
     "Share of portfolio-company fees credited against the management fee."),
    ("management_fee.transaction_fee_pct", "Transaction fee", "pct", None,
     "Charged by the manager to portfolio companies, as a share of equity invested."),
    ("management_fee.monitoring_fee_pct", "Monitoring fee", "pct", None, "Per annum, on cost still invested."),
    (None, "Partnership expenses", "", None, ""),
    ("expenses.organizational", "Organisational expenses", "money", None, "One-off, at first close."),
    ("expenses.annual_operating", "Annual operating expenses", "money", None, "Audit, administration, legal."),
    (None, "Distribution waterfall", "", None, ""),
    ("waterfall.type", "Waterfall type", "choice:" + ",".join(WATERFALL_TYPES), "wf_type",
     "european = whole-of-fund; american = deal-by-deal. Selects which waterfall sheet drives Returns."),
    ("waterfall.pref_rate", "Preferred return (hurdle)", "pct", "pref_rate", "Per annum, on unreturned LP capital."),
    ("waterfall.pref_compounding", "Preferred return compounding", "choice:" + ",".join(PREF_COMPOUNDING),
     "pref_comp", "annual and quarterly compound; simple accrues on capital only."),
    ("waterfall.catch_up_rate", "GP catch-up rate", "pct", "catch_up",
     "100% = full catch-up; 0% = none (hard hurdle). Must be 0 or above the carry rate."),
    ("waterfall.carry_rate", "Carried interest rate", "pct", "carry_rate", "GP share of profit."),
    ("waterfall.expense_allocation", "Deal-by-deal expense allocation", "choice:" + ",".join(EXPENSE_ALLOCATION),
     None, "How fees and expenses enter the realised capital base under deal-by-deal."),
    ("waterfall.writedowns_as_realized", "Treat write-downs as realised", "bool", None,
     "Deal-by-deal: unrealised write-downs must be recovered before carry is paid."),
    (None, "Clawback and escrow", "", None, ""),
    ("clawback.enabled", "GP clawback enabled", "bool", "cb_enabled", "Tested at liquidation."),
    ("clawback.after_tax_limit", "Limit clawback to after-tax carry", "bool", "cb_after_tax",
     "Caps the clawback at carry received net of assumed tax."),
    ("clawback.assumed_tax_rate", "Assumed tax rate on carry", "pct", "cb_tax_rate", "Used by the after-tax cap."),
    ("clawback.escrow_pct", "Carry escrow", "pct", "escrow_pct",
     "Share of each carry distribution held back until liquidation."),
    (None, "Recycling", "", None, ""),
    ("recycling.enabled", "Recycling enabled", "bool", None, "Cost returned in the investment period can be re-called."),
    ("recycling.cap_pct_of_commitments", "Recycling cap", "pct", None, "As a share of commitments."),
    (None, "Subscription line", "", None, ""),
    ("subscription_line.enabled", "Subscription line enabled", "bool", None, "Bridges capital calls in the investment period."),
    ("subscription_line.quarters_outstanding", "Quarters outstanding", "int", None, "How long each drawing is carried."),
    ("subscription_line.interest_rate", "Interest rate", "pct", None, "Per annum, simple."),
    (None, "Valuation policy", "", None, ""),
    ("valuation.hold_at_cost_quarters", "Hold at cost (quarters)", "int", None,
     "Then marks converge linearly to the value still to be realised."),
    (None, "Scenario shocks", "", None, ""),
    ("shocks.exit_multiple_pct", "Exit multiple change", "pct", None, "Relative change applied to every deal's exit valuation."),
    ("shocks.ebitda_growth_shift", "EBITDA growth change", "pct", None, "Added to every operating deal's EBITDA growth."),
    ("shocks.hold_shift_quarters", "Holding period change (quarters)", "int", None, "Added to every holding period."),
]  # fmt: skip

INPUT_FORMATS = {"money": MONEY, "pct": PCT2, "years": YEARS, "int": "0", "date": DATE}
INPUT_FIRST_ROW = 6


def _config_value(cfg_dict: dict[str, Any], key: str) -> Any:
    section, field = key.split(".", 1)
    return cfg_dict[section].get(field)


def _inputs_sheet(wb: Workbook, cfg: FundConfig) -> None:
    ws = wb.create_sheet(S_INPUTS)
    _title(ws, "Inputs", "Blue = input. Yellow = lever that recalculates this workbook live. "
                         "Other inputs feed the Python engine: change them, then re-run `pefund excel`.")  # fmt: skip
    _header(ws, INPUT_FIRST_ROW - 1, ["Assumption", "Value", "Allowed values", "Used by", "What it does", "Config key"],
            height=24)  # fmt: skip
    raw = cfg.to_dict()
    row = INPUT_FIRST_ROW
    for key, label, kind, defined, note in INPUT_ROWS:
        if key is None:
            _put(ws, f"A{row}", label, "section", fill=FILL_SUB)
            for column in "BCDEF":
                ws[f"{column}{row}"].fill = FILL_SUB
            row += 1
            continue
        value = _config_value(raw, key)
        if kind == "date" and value is not None:
            value = dt.date.fromisoformat(value)
        live = defined in ("wf_type", "pref_rate", "pref_comp", "catch_up", "carry_rate", "cb_enabled",
                           "cb_after_tax", "cb_tax_rate", "escrow_pct")  # fmt: skip
        long_text = kind == "text" and len(str(value)) > 16  # would be cut off if right-aligned
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", value, "input", INPUT_FORMATS.get(kind), FILL_KEY if live else None,
             Alignment(horizontal="left" if long_text else "right"))  # fmt: skip
        if kind.startswith("choice:"):
            choices = kind.split(":", 1)[1]
            _put(ws, f"C{row}", choices.replace(",", " | "), "note", align=INDENT)
            validation = DataValidation(type="list", formula1=f'"{choices}"', allow_blank=False)
            ws.add_data_validation(validation)
            validation.add(f"B{row}")
        elif kind == "bool":
            _put(ws, f"C{row}", "TRUE | FALSE", "note", align=INDENT)
            validation = DataValidation(type="list", formula1='"TRUE,FALSE"', allow_blank=False)
            ws.add_data_validation(validation)
            validation.add(f"B{row}")
        _put(ws, f"D{row}", LIVE if live else PYTHON, "note")
        _put(ws, f"E{row}", note, "note")
        _put(ws, f"F{row}", key, "note")
        if defined:
            _name(wb, defined, S_INPUTS, f"B{row}")
        row += 1

    row += 1
    _put(ws, f"A{row}", "Derived (formulas)", "section", fill=FILL_SUB)
    for column in "BCDEF":
        ws[f"{column}{row}"].fill = FILL_SUB
    derived = [
        ("LP commitments", "=committed_capital*(1-gp_pct)", MONEY, "lp_commitment", "Committed capital less the GP commitment."),
        ("GP commitment", "=committed_capital*gp_pct", MONEY, None, ""),
        ("Compounding periods per year", '=IF(pref_comp="quarterly",4,1)', "0", "comp_m",
         "Used by the preferred-return formulas; ignored when compounding is simple."),
    ]  # fmt: skip
    for label, formula, fmt, defined, note in derived:
        row += 1
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", formula, "formula", fmt, align=Alignment(horizontal="right"))
        _put(ws, f"E{row}", note, "note")
        if defined:
            _name(wb, defined, S_INPUTS, f"B{row}")
    row += 2
    _put(ws, f"A{row}", "All inputs are hypothetical assumptions for a sample fund; none describes a real fund, "
                        "investor or company.", "note")  # fmt: skip
    _widths(ws, {"A": 36, "B": 20, "C": 30, "D": 22, "E": 78, "F": 40})
    ws.freeze_panes = f"A{INPUT_FIRST_ROW}"
    ws.sheet_properties.tabColor = "0000FF"


# --------------------------------------------------------------------------- #
# Deals and investors (inputs plus engine outputs)
# --------------------------------------------------------------------------- #
DEAL_INPUT_HEADERS = [
    "Deal", "Sector", "Strategy", "Exit date", "Gross MOIC (direct mode)", "Entry EBITDA", "Entry EV / EBITDA",
    "Entry leverage (x EBITDA)", "EBITDA growth (CAGR)", "Exit EV / EBITDA", "Cash conversion",
    "Investment 1 date", "Investment 1 amount", "Investment 2 date", "Investment 2 amount",
    "Investment 3 date", "Investment 3 amount",
    "Event 1 date", "Event 1 kind", "Event 1 share of proceeds", "Event 1 stake sold",
    "Event 2 date", "Event 2 kind", "Event 2 share of proceeds", "Event 2 stake sold",
]  # fmt: skip
DEAL_OUTPUT_HEADERS = ["Invested", "Proceeds", "Gain", "Gross MOIC", "Gross IRR", "Hold (years)", "Ownership",
                       "Exit EBITDA", "Status at valuation date"]  # fmt: skip
DEAL_HEADER_ROW = 6
DEAL_OUTPUT_COL = len(DEAL_INPUT_HEADERS) + 2  # one blank column between inputs and outputs


def _deals_sheet(wb: Workbook, cfg: FundConfig, result: FundResult) -> tuple[int, int]:
    ws = wb.create_sheet(S_DEALS)
    _title(ws, "Deals", "Blue = input. Give each deal either a gross MOIC (direct mode) or an operating case; leave "
                        "unused cells blank. Grey columns are results from the Python engine.")  # fmt: skip
    _put(ws, "A3", "Operating case: exit equity = EBITDA x EV/EBITDA - net debt, times the fund's ownership "
                   "(its cheque / entry equity). Interim events: kind is dividend_recap or partial_exit; share is the "
                   "share of the deal's total proceeds; stake sold applies to partial exits.", "note")  # fmt: skip
    _put(ws, "A4", f"Up to {MAX_INVESTMENTS} investments and {MAX_EVENTS} interim events per deal. "
                   "Row 7 shows the expected format.", "note")  # fmt: skip
    _header(ws, DEAL_HEADER_ROW, DEAL_INPUT_HEADERS, height=48)
    _header(ws, DEAL_HEADER_ROW, DEAL_OUTPUT_HEADERS, start_col=DEAL_OUTPUT_COL, height=48)
    ws.cell(DEAL_HEADER_ROW - 1, 1, "Inputs").font = F_SECTION
    ws.cell(DEAL_HEADER_ROW - 1, DEAL_OUTPUT_COL, "Python engine output (re-run to refresh)").font = F_SECTION

    formats = [None, None, None, DATE, MULT, MONEY, MULT, MULT, PCT, MULT, PCT,
               DATE, MONEY, DATE, MONEY, DATE, MONEY, DATE, None, PCT, PCT, DATE, None, PCT, PCT]  # fmt: skip
    kinds = DataValidation(type="list", formula1='"' + ",".join(PROCEEDS_KINDS) + '"', allow_blank=True)
    ws.add_data_validation(kinds)
    outputs = result.deals.set_index("deal")
    first = DEAL_HEADER_ROW + 1
    for i, deal in enumerate(cfg.deals):
        if len(deal.investments) > MAX_INVESTMENTS or len(deal.proceeds_events) > MAX_EVENTS:
            raise ConfigError(
                f"deal '{deal.name}': the Excel template holds up to {MAX_INVESTMENTS} investments and "
                f"{MAX_EVENTS} interim events per deal"
            )
        row = first + i
        op = deal.operating
        values: list[Any] = [
            deal.name, deal.sector, deal.strategy, deal.exit_date, deal.gross_moic,
            op.entry_ebitda if op else None, op.entry_ev_multiple if op else None, op.entry_leverage if op else None,
            op.ebitda_growth if op else None, op.exit_ev_multiple if op else None, op.cash_conversion if op else None,
        ]  # fmt: skip
        for k in range(MAX_INVESTMENTS):
            inv = deal.investments[k] if k < len(deal.investments) else None
            values += [inv.date if inv else None, inv.amount if inv else None]
        for k in range(MAX_EVENTS):
            event = deal.proceeds_events[k] if k < len(deal.proceeds_events) else None
            values += [event.date if event else None, event.kind if event else None,
                       event.share if event else None, event.stake_sold if event else None]  # fmt: skip
        for column, (value, fmt) in enumerate(zip(values, formats, strict=True), start=1):
            _put(ws, f"{get_column_letter(column)}{row}", value, "input", fmt)
        kinds.add(f"S{row}")
        kinds.add(f"W{row}")

        out = outputs.loc[deal.name]
        out_values = [
            (out["invested"], MONEY), (out["proceeds"], MONEY), (out["gain"], MONEY), (out["gross_moic"], MULT),
            (out["gross_irr"], PCT), (out["hold_years"], YEARS), (out["ownership"], PCT), (out["exit_ebitda"], MONEY),
            (out["status_at_valuation"], None),
        ]  # fmt: skip
        for offset, (value, fmt) in enumerate(out_values):
            _put(ws, f"{get_column_letter(DEAL_OUTPUT_COL + offset)}{row}", value, "python", fmt)
    last = first + len(cfg.deals) - 1
    total = last + 2
    c_inv, c_proc, c_gain, c_moic = (get_column_letter(DEAL_OUTPUT_COL + k) for k in range(4))
    _put(ws, f"A{total}", "Total", "bold", fill=FILL_TOTAL)
    for column in (c_inv, c_proc, c_gain):
        _put(ws, f"{column}{total}", f"=SUM({column}{first}:{column}{last})", "formula", MONEY, FILL_TOTAL, bold=True)
    _put(ws, f"{c_moic}{total}", f"=IF({c_inv}{total}>0,{c_proc}{total}/{c_inv}{total},0)", "formula", MULT,
         FILL_TOTAL, bold=True)  # fmt: skip
    widths = {get_column_letter(c): 13.5 for c in range(1, DEAL_OUTPUT_COL + len(DEAL_OUTPUT_HEADERS))}
    widths.update({"A": 20, "B": 22, "C": 11, get_column_letter(DEAL_OUTPUT_COL - 1): 3,
                   get_column_letter(DEAL_OUTPUT_COL + 8): 22})  # fmt: skip
    _widths(ws, widths)
    ws.freeze_panes = f"B{first}"
    ws.sheet_properties.tabColor = "0000FF"
    return first, last


def _investors_sheet(wb: Workbook, cfg: FundConfig) -> tuple[int, int]:
    ws = wb.create_sheet(S_INVESTORS)
    _title(ws, "Investors", "LP register. Blue = input. Commitments must add up to LP commitments on the Inputs sheet.")
    _header(ws, 5, ["Limited partner", "Commitment", "Share of LP class"], height=24)
    first = 6
    last = first + max(len(cfg.investors), 1) - 1
    for i, investor in enumerate(cfg.investors):
        row = first + i
        _put(ws, f"A{row}", investor.name, "input")
        _put(ws, f"B{row}", investor.commitment, "input", MONEY)
        _put(ws, f"C{row}", f"=IF(SUM($B${first}:$B${last})>0,B{row}/SUM($B${first}:$B${last}),0)", "formula", PCT)
    total = last + 2
    _put(ws, f"A{total}", "Total", "bold", fill=FILL_TOTAL)
    _put(ws, f"B{total}", f"=SUM(B{first}:B{last})", "formula", MONEY, FILL_TOTAL, bold=True)
    _put(ws, f"C{total}", f"=SUM(C{first}:C{last})", "formula", PCT, FILL_TOTAL, bold=True)
    _put(ws, f"A{total + 1}", "Difference to LP commitments (should be zero)")
    _put(ws, f"B{total + 1}", f"=B{total}-lp_commitment", "formula", MONEY2)
    _widths(ws, {"A": 44, "B": 16, "C": 18})
    ws.sheet_properties.tabColor = "0000FF"
    return first, last


# --------------------------------------------------------------------------- #
# Quarterly cash flows from the engine
# --------------------------------------------------------------------------- #
def _cashflows_sheet(wb: Workbook, result: FundResult) -> int:
    ws = wb.create_sheet(S_CASH)
    fund = result.config.fund
    unit = f"{fund.currency} {fund.unit}"
    _title(ws, "Quarterly fund cash flows", f"Values written by the Python engine ({unit}); re-run `pefund excel` to "
                                            "refresh. These feed the live waterfall sheets.")  # fmt: skip
    _put(
        ws,
        "A3",
        "Fee check recomputes the gross fee as basis x rate / 4. Net fee = gross fee - offset (formula).",
        "note",
    )
    groups = {"D": "Portfolio", "I": "Management fee", "O": "Other costs", "Q": "Funding", "T": "Distributable cash"}
    for column, label in groups.items():
        _put(ws, f"{column}{ROW0 - 2}", label, "section")
    _header(ws, ROW0 - 1, [
        "Quarter", "Date", "Year", "Investments (cost)", "Gross proceeds", "Cost relieved", "Cost outstanding",
        "Portfolio fair value", "Fee basis", "Fee rate (p.a.)", "Gross fee", "Fee check (formula)", "Fee offset",
        "Net fee", "Partnership expenses", "Line interest", "LP capital called", "GP capital called",
        "Fees / expenses paid from proceeds", "LP distributable cash", "GP commitment distributions",
        "LP unfunded commitment",
    ])  # fmt: skip
    a = result.arrays
    python_columns = [
        ("inv", "investments"), ("proc", "gross_proceeds"), ("relief", "cost_relieved"),
        ("cost_out", "cost_outstanding"), ("fv", "portfolio_fv"), ("fee_basis", "mgmt_fee_basis"),
        ("fee_gross", "mgmt_fee_gross"), ("fee_offset", "fee_offset"), ("expenses", "expenses"),
        ("line_int", "line_interest"), ("lp_called", "lp_contributions"), ("gp_called", "gp_contributions"),
        ("netted", "netted_from_proceeds"), ("lp_cash", "lp_distributable"),
        ("gp_dist", "gp_commit_distributions"), ("lp_unfunded", "lp_unfunded"),
    ]  # fmt: skip
    n = result.n_quarters
    for q in range(n):
        row = ROW0 + q
        _put(ws, f"{CF['q']}{row}", q, "python", INDEX)
        _put(ws, f"{CF['date']}{row}", result.dates[q], "python", DATE)
        _put(ws, f"{CF['year']}{row}", f"=YEAR({CF['date']}{row})", "formula", INDEX)
        for column, array in python_columns:
            _put(ws, f"{CF[column]}{row}", float(a[array][q]), "python", MONEY)
        _put(ws, f"{CF['fee_rate']}{row}", float(a["mgmt_fee_rate"][q]), "python", PCT2)
        _put(ws, f"{CF['fee_check']}{row}", f"={CF['fee_basis']}{row}*{CF['fee_rate']}{row}/4", "formula", MONEY)
        _put(ws, f"{CF['fee_net']}{row}", f"={CF['fee_gross']}{row}-{CF['fee_offset']}{row}", "formula", MONEY)
    last = ROW0 + n - 1
    flow_columns = ["inv", "proc", "relief", "fee_gross", "fee_check", "fee_offset", "fee_net", "expenses",
                    "line_int", "lp_called", "gp_called", "netted", "lp_cash", "gp_dist"]  # fmt: skip
    _total_row(ws, last + 1, ROW0, last, {CF[c]: MONEY for c in flow_columns})
    _widths(ws, {get_column_letter(c): 13.5 for c in range(1, len(CF) + 1)} | {"A": 9, "B": 11, "C": 7})
    ws.freeze_panes = f"C{ROW0}"
    return last


def _dbd_inputs_sheet(wb: Workbook, american: FundResult) -> tuple[str, str, str]:
    """Capital called by purpose and the deal-by-deal eligibility weights (engine values)."""
    ws = wb.create_sheet(S_DBD_IN)
    _title(ws, "Deal-by-deal inputs", "Values written by the Python engine. Left: LP capital called, by purpose. "
                                      "Right: share of that capital eligible for return in each quarter.")  # fmt: skip
    _put(ws, "A3", "A deal's capital becomes eligible as it is realised (or written down, if that rule is on); fees "
                   "and expenses become eligible in proportion to realised cost.", "note")  # fmt: skip
    kinds = american.kind_names
    k = len(kinds)
    c0, w0 = 3, 3 + k + 1
    _put(ws, f"{get_column_letter(c0)}{ROW0 - 2}", "LP capital called by purpose", "section")
    _put(
        ws,
        f"{get_column_letter(w0)}{ROW0 - 2}",
        "Eligibility weight (0 = not yet eligible, 1 = fully eligible)",
        "section",
    )
    _header(ws, ROW0 - 1, ["Quarter", "Date", *kinds], height=54)
    _header(ws, ROW0 - 1, kinds, start_col=w0, height=54)
    capital, weights = american.lp_capital_by_kind, american.eligibility
    assert capital is not None and weights is not None
    n = american.n_quarters
    for q in range(n):
        row = ROW0 + q
        _put(ws, f"A{row}", q, "python", INDEX)
        _put(ws, f"B{row}", american.dates[q], "python", DATE)
        for j in range(k):
            _put(ws, f"{get_column_letter(c0 + j)}{row}", float(capital[j, q]), "python", MONEY)
            _put(ws, f"{get_column_letter(w0 + j)}{row}", float(weights[j, q]), "python", '0.000;(0.000);"-"')
    last = ROW0 + n - 1
    _total_row(ws, last + 1, ROW0, last, {get_column_letter(c0 + j): MONEY for j in range(k)})
    _widths(ws, {get_column_letter(c): 12.5 for c in range(1, w0 + k)} | {"A": 9, "B": 11})
    ws.freeze_panes = f"C{ROW0}"
    block = f"{S_DBD_IN}!${get_column_letter(c0)}${ROW0}:${get_column_letter(c0 + k - 1)}${last}"
    dates = f"{S_DBD_IN}!$B${ROW0}:$B${last}"
    weight_row = f"{S_DBD_IN}!${get_column_letter(w0)}{{row}}:${get_column_letter(w0 + k - 1)}{{row}}"
    return block, dates, weight_row


# --------------------------------------------------------------------------- #
# Waterfall sheets (live formulas)
# --------------------------------------------------------------------------- #
GROWTH = "(1+pref_rate/comp_m)^(comp_m*{years})"  # compounding growth factor over {years}


def _waterfall_sheet(wb: Workbook, sheet: str, n: int, *, american: bool, unit: str,
                     dbd_refs: tuple[str, str, str] | None = None) -> None:  # fmt: skip
    ws = wb.create_sheet(sheet)
    if american:
        _title(ws, "Deal-by-deal (American) waterfall - live formulas",
               f"Carry is paid deal by deal, with realised losses and allocated fees carried forward ({unit}). "
               "Recalculates from the yellow levers on Inputs.")  # fmt: skip
        method = ["Eligible capital (realised deals + allocated fees)", "Eligible capital grown at the pref",
                  "Earlier tier 1-2 payments grown at the pref", "Tier 1 paid before this quarter"]  # fmt: skip
        _put(ws, "A3", "Method: hurdle = eligible capital compounded at the preferred return, less earlier return-of-"
                       "capital and pref payments compounded the same way. Eligibility comes from DbD_Inputs.", "note")  # fmt: skip
    else:
        _title(ws, "Whole-of-fund (European) waterfall - live formulas",
               f"All contributed capital and the preferred return on it are returned before any carry ({unit}). "
               "Recalculates from the yellow levers on Inputs.")  # fmt: skip
        method = ["Opening unreturned capital", "Opening unpaid pref", "Pref rate for the period",
                  "Pref accrued in the period"]  # fmt: skip
        _put(ws, "A3", "Method: running ledger. Each quarter: accrue pref, add capital called, then pay tier 1 "
                       "(capital), tier 2 (pref), tier 3 (GP catch-up) and tier 4 (carry split).", "note")  # fmt: skip
    _put(ws, "A4", "Terms in use:", "bold")
    for column, (label, name, fmt) in zip(
        "CEGIK",
        [("Pref", "pref_rate", PCT2), ("Carry", "carry_rate", PCT2), ("Catch-up", "catch_up", PCT2),
         ("Escrow", "escrow_pct", PCT2), ("Compounding", "pref_comp", None)],
        strict=True,
    ):  # fmt: skip
        _put(ws, f"{column}4", f"{label}:", "note", align=Alignment(horizontal="right"))
        _put(ws, f"{get_column_letter(ws[f'{column}4'].column + 1)}4", f"={name}", "link", fmt,
             align=Alignment(horizontal="left"))  # fmt: skip

    groups = {"D": "From the engine", "F": "Hurdle", "L": "Tier 1-2: LPs", "O": "Tier 3: GP catch-up",
              "S": "Tier 4: split", "V": "Totals", "Y": "Running balances", "AC": "Escrow",
              "AE": "Whole-fund test ledger", "AH": "After clawback"}  # fmt: skip
    for column, label in groups.items():
        _put(ws, f"{column}{ROW0 - 2}", label, "section")
    _header(ws, ROW0 - 1, [
        "Quarter", "Date", "Days in period", "LP capital called", "LP distributable cash", *method,
        "Capital to return", "Pref to pay", "Tier 1 Return of capital", "Tier 2 Preferred return",
        "Cash after tier 2", "Catch-up still needed", "Tier 3 Catch-up", "Tier 3 to GP", "Tier 3 to LPs",
        "Tier 4 Residual", "Tier 4 to GP", "Tier 4 to LPs", "LP distribution", "GP carried interest",
        "Check (zero)", "Cumulative profit distributed", "Cumulative carry", "Capital still to return",
        "Pref still to pay", "Carry escrowed", "Escrow balance", "Pref accrual", "Unreturned capital",
        "Unpaid pref", "Clawback received", "LP distribution incl. clawback", "GP carry cash",
    ], height=56)  # fmt: skip

    last = ROW0 + n - 1
    s0 = last + 3  # summary block anchor
    summary = {key: f"$E${s0 + offset}" for key, offset in SUMMARY.items()}
    final = f"${WF['q']}{{r}}=MAX(${WF['q']}${ROW0}:${WF['q']}${last})"

    for q in range(n):
        r, p = ROW0 + q, ROW0 + q - 1
        first = q == 0

        def prev(column: str, r: int = r, p: int = p, first: bool = first) -> str:
            """Reference to the previous quarter's cell, or zero for the first quarter."""
            return "0" if first else f"{WF[column]}{p}"

        def to(column: str, p: int = p) -> str:
            """Range from the first quarter to the previous one."""
            return f"{WF[column]}${ROW0}:{WF[column]}{p}"

        f: dict[str, str] = {}
        f["date"] = f"={S_CASH}!{CF['date']}{r}"
        f["days"] = "0" if first else f"={WF['date']}{r}-{WF['date']}{p}"
        f["called"] = f"={S_CASH}!{CF['lp_called']}{r}"
        f["cash"] = f"={S_CASH}!{CF['lp_cash']}{r}"
        years = f"{WF['days']}{r}/365"
        if american:
            assert dbd_refs is not None
            block, dates, weight_row = dbd_refs
            weights = weight_row.format(row=r)
            mask = f"({dates}<=${WF['date']}{r})"
            age = f"((${WF['date']}{r}-{dates})/365)"
            f["m1"] = f"=SUMPRODUCT({weights}*{block}*{mask})"
            f["m2"] = (
                f'=IF(pref_comp="simple",SUMPRODUCT({weights}*{block}*{mask}*{age}),'
                f"SUMPRODUCT({weights}*{block}*{mask}*{GROWTH.format(years=age)}))"
            )
            if first:
                f["m3"], f["m4"] = "0", "0"
                pref_paid = "0"
            else:
                paid_age = f"(({WF['date']}{r}-{to('date')})/365)"
                f["m3"] = (
                    f'=IF(pref_comp="simple",SUMPRODUCT({to("t1")}*{paid_age}),'
                    f"SUMPRODUCT(({to('t1')}+{to('t2')})*{GROWTH.format(years=paid_age)}))"
                )
                f["m4"] = f"=SUM({to('t1')})"
                pref_paid = f"SUM({to('t2')})"
            f["cap_out"] = f"=MAX(0,{WF['m1']}{r}-{WF['m4']}{r})"
            f["pref_out"] = (
                f'=IF(pref_comp="simple",MAX(0,pref_rate*({WF["m2"]}{r}-{WF["m3"]}{r})-{pref_paid}),'
                f"MAX(0,{WF['m2']}{r}-{WF['m3']}{r}-{WF['cap_out']}{r}))"
            )
        else:
            f["m1"] = "0" if first else f"={WF['cap_after']}{p}"
            f["m2"] = "0" if first else f"={WF['pref_after']}{p}"
            f["m3"] = f'=IF(pref_comp="simple",pref_rate*{years},{GROWTH.format(years=years)}-1)'
            f["m4"] = f'=IF(pref_comp="simple",{WF["m1"]}{r},{WF["m1"]}{r}+{WF["m2"]}{r})*{WF["m3"]}{r}'
            f["cap_out"] = f"={WF['m1']}{r}+{WF['called']}{r}"
            f["pref_out"] = f"={WF['m2']}{r}+{WF['m4']}{r}"

        f["t1"] = f"=MIN({WF['cash']}{r},{WF['cap_out']}{r})"
        f["t2"] = f"=MIN({WF['cash']}{r}-{WF['t1']}{r},{WF['pref_out']}{r})"
        f["rem"] = f"={WF['cash']}{r}-{WF['t1']}{r}-{WF['t2']}{r}"
        f["cu_need"] = (
            f"=IF(catch_up>carry_rate,MAX(0,(carry_rate*({prev('cum_profit')}+{WF['t2']}{r})-{prev('cum_carry')})"
            "/(catch_up-carry_rate)),0)"
        )
        f["t3"] = f"=MIN({WF['rem']}{r},{WF['cu_need']}{r})"
        f["t3_gp"] = f"=catch_up*{WF['t3']}{r}"
        f["t3_lp"] = f"={WF['t3']}{r}-{WF['t3_gp']}{r}"
        f["t4"] = f"={WF['rem']}{r}-{WF['t3']}{r}"
        f["t4_gp"] = f"=carry_rate*{WF['t4']}{r}"
        f["t4_lp"] = f"={WF['t4']}{r}-{WF['t4_gp']}{r}"
        f["lp"] = f"={WF['t1']}{r}+{WF['t2']}{r}+{WF['t3_lp']}{r}+{WF['t4_lp']}{r}"
        f["gp"] = f"={WF['t3_gp']}{r}+{WF['t4_gp']}{r}"
        f["check"] = f"={WF['cash']}{r}-{WF['lp']}{r}-{WF['gp']}{r}"
        f["cum_profit"] = f"={prev('cum_profit')}+{WF['t2']}{r}+{WF['t3']}{r}+{WF['t4']}{r}"
        f["cum_carry"] = f"={prev('cum_carry')}+{WF['gp']}{r}"
        f["cap_after"] = f"={WF['cap_out']}{r}-{WF['t1']}{r}"
        f["pref_after"] = f"={WF['pref_out']}{r}-{WF['t2']}{r}"
        is_final = final.format(r=r)
        f["escrowed"] = f"=IF({is_final},0,escrow_pct*{WF['gp']}{r})"
        f["escrow_bal"] = f"=IF({is_final},0,{prev('escrow_bal')}+{WF['escrowed']}{r})"
        f["sh_accr"] = (
            "0" if first else
            f'=IF(pref_comp="simple",{WF["sh_cap"]}{p}*pref_rate*{years},'
            f"({WF['sh_cap']}{p}+{WF['sh_pref']}{p})*({GROWTH.format(years=years)}-1))"
        )  # fmt: skip
        capital_before = f"({prev('sh_cap')}+{WF['called']}{r})"
        to_capital = f"MIN({WF['lp']}{r},{capital_before})"
        f["sh_cap"] = f"={capital_before}-{to_capital}"
        f["sh_pref"] = (
            f"={prev('sh_pref')}+{WF['sh_accr']}{r}"
            f"-MIN({WF['lp']}{r}-{to_capital},{prev('sh_pref')}+{WF['sh_accr']}{r})"
        )
        f["cb_recv"] = f"=IF({is_final},{summary['payable']},0)"
        f["lp_incl"] = f"={WF['lp']}{r}+{WF['cb_recv']}{r}"
        f["gp_cash"] = f"={WF['gp']}{r}-{WF['escrowed']}{r}+IF({is_final},{summary['released']}-{summary['from_gp']},0)"

        _put(ws, f"{WF['q']}{r}", q, "python", INDEX)
        for name, formula in f.items():
            value: Any = 0 if formula == "0" else formula
            if name == "date":
                _put(ws, f"{WF[name]}{r}", value, "link", DATE)
            elif name in ("called", "cash"):
                _put(ws, f"{WF[name]}{r}", value, "link", MONEY)
            elif name == "days":
                _put(ws, f"{WF[name]}{r}", value, "formula", INDEX)
            elif name == "m3" and not american:
                _put(ws, f"{WF[name]}{r}", value, "formula", PCT2)
            elif name == "check":
                _put(ws, f"{WF[name]}{r}", value, "formula", MONEY2)
            else:
                _put(ws, f"{WF[name]}{r}", value, "formula", MONEY)

    totals = ["called", "cash", "t1", "t2", "t3", "t3_gp", "t3_lp", "t4", "t4_gp", "t4_lp", "lp", "gp", "check",
              "escrowed", "sh_accr", "cb_recv", "lp_incl", "gp_cash"]  # fmt: skip
    if not american:
        totals.append("m4")
    _total_row(ws, last + 1, ROW0, last, {WF[c]: MONEY for c in totals})

    def total(column: str) -> str:
        return f"SUM({WF[column]}{ROW0}:{WF[column]}{last})"

    e = {key: f"E{s0 + offset}" for key, offset in SUMMARY.items()}
    block_rows = [
        ("called", "Total LP capital called", f"={total('called')}", "The capital the clawback test protects."),
        ("lp_before", "Total LP distributions, before clawback", f"={total('lp')}", ""),
        ("carry", "Total carried interest distributed", f"={total('gp')}", "Including amounts held in escrow."),
        ("profit", "LP-class net profit", f"={e['lp_before']}+{e['carry']}-{e['called']}", "LP distributions + carry - capital called."),
        ("test_a", "Test A: carry above the GP's share of profit",
         f"=MAX(0,{e['carry']}-carry_rate*MAX({e['profit']},0))", "Carry received less carry rate x net profit."),
        ("pref_accrued", "Whole-fund preferred return accrued", f"={total('sh_accr')}",
         "Pref on all LP capital, with every LP distribution applied to capital first."),
        ("test_b", "Test B: LP shortfall against capital plus pref",
         f"=MAX(0,{e['called']}+{e['pref_accrued']}-{e['lp_before']})", "What LPs are short of capital + pref."),
        ("uncapped", "Clawback before the after-tax cap",
         f"=IF({e['carry']}<=0,0,MIN({e['carry']},MAX({e['test_a']},{e['test_b']})))",
         "Greater of the two tests, never more than carry received."),
        ("cap", "After-tax cap", f"=IF(cb_after_tax,{e['carry']}*(1-cb_tax_rate),{e['carry']})",
         "Carry received net of assumed tax, if the limit applies."),
        ("payable", "Clawback payable by the GP", f"=IF(cb_enabled,MIN({e['uncapped']},{e['cap']}),0)", ""),
        ("escrow", "Carry held in escrow at liquidation", f"={total('escrowed')}", ""),
        ("from_escrow", "Clawback met from escrow", f"=MIN({e['payable']},{e['escrow']})", ""),
        ("from_gp", "Clawback paid by the GP directly", f"={e['payable']}-{e['from_escrow']}", ""),
        ("released", "Escrow released to the GP", f"={e['escrow']}-{e['from_escrow']}", ""),
        ("carry_net", "Carried interest, net of clawback", f"={e['carry']}-{e['payable']}", ""),
        ("lp_incl", "LP distributions, including clawback", f"={e['lp_before']}+{e['payable']}", ""),
        ("shortfall", "LP shortfall caused by the after-tax cap", f"={e['uncapped']}-{e['payable']}",
         "Clawback the LPs are owed but do not receive."),
    ]  # fmt: skip
    _put(ws, f"A{s0}", "Liquidation test and GP clawback", "section", fill=FILL_SUB)
    for column in "BCDEFGHI":
        ws[f"{column}{s0}"].fill = FILL_SUB
    for key, label, formula, note in block_rows:
        row = s0 + SUMMARY[key]
        _put(ws, f"A{row}", label, "bold" if key in ("payable", "carry_net", "lp_incl") else "label")
        _put(ws, f"E{row}", formula, "formula", MONEY, bold=key in ("payable", "carry_net", "lp_incl"))
        _put(ws, f"F{row}", note, "note")

    widths = {get_column_letter(c): 13.5 for c in range(1, len(WF) + 1)}
    widths.update({"A": 9, "B": 11, "C": 8})
    _widths(ws, widths)
    ws.freeze_panes = f"C{ROW0}"
    ws.sheet_properties.tabColor = "ED7D31" if american else "2E75B6"
    ws.conditional_formatting.add(
        f"{WF['check']}{ROW0}:{WF['check']}{last}",
        CellIsRule(
            operator="notBetween", formula=["-0.000001", "0.000001"], font=Font(name=FONT, color="FF0000", bold=True)
        ),
    )


# --------------------------------------------------------------------------- #
# LP / GP flows, returns, annual roll-up
# --------------------------------------------------------------------------- #
def _pick(column: str, row: int | str) -> str:
    """Cell from whichever waterfall sheet the Inputs selector points at."""
    return f'IF(wf_type="european",{S_EU}!{column}{row},{S_DBD}!{column}{row})'


def _flows_sheet(wb: Workbook, n: int, unit: str) -> int:
    ws = wb.create_sheet(S_FLOWS)
    _title(
        ws, "LP and GP cash flows - live formulas", f"Quarterly flows under the waterfall selected on Inputs ({unit})."
    )
    groups = {"D": "Limited partners", "K": "Waterfall tiers (selected waterfall)", "Q": "General partner",
              "V": "Portfolio"}  # fmt: skip
    for column, label in groups.items():
        _put(ws, f"{column}{ROW0 - 2}", label, "section")
    _header(ws, ROW0 - 1, [
        "Quarter", "Date", "Year", "LP capital called", "LP distributions", "LP net cash flow",
        "Cumulative LP net cash flow", "Cumulative paid-in", "Cumulative distributed", "DPI to date",
        "Tier 1 Return of capital", "Tier 2 Preferred return", "Tier 3 Catch-up to GP", "Tier 3 to LPs",
        "Tier 4 to LPs", "Tier 4 to GP", "GP carry distributed", "GP carry cash (after escrow, clawback)",
        "GP commitment called", "GP commitment distributions", "Management fee (net)", "Gross deal cash flow",
    ], height=56)  # fmt: skip
    last = ROW0 + n - 1
    for q in range(n):
        r, p = ROW0 + q, ROW0 + q - 1
        first = q == 0

        def cumulative(target: str, source: str, r: int = r, p: int = p, first: bool = first) -> str:
            return f"={FL[source]}{r}" if first else f"={FL[target]}{p}+{FL[source]}{r}"

        _put(ws, f"{FL['q']}{r}", q, "python", INDEX)
        _put(ws, f"{FL['date']}{r}", f"={S_CASH}!{CF['date']}{r}", "link", DATE)
        _put(ws, f"{FL['year']}{r}", f"=YEAR({FL['date']}{r})", "formula", INDEX)
        _put(ws, f"{FL['called']}{r}", f"={S_CASH}!{CF['lp_called']}{r}", "link", MONEY)
        _put(ws, f"{FL['dist']}{r}", "=" + _pick(WF["lp_incl"], r), "formula", MONEY)
        _put(ws, f"{FL['net']}{r}", f"={FL['dist']}{r}-{FL['called']}{r}", "formula", MONEY)
        _put(ws, f"{FL['cum_net']}{r}", cumulative("cum_net", "net"), "formula", MONEY)
        _put(ws, f"{FL['cum_called']}{r}", cumulative("cum_called", "called"), "formula", MONEY)
        _put(ws, f"{FL['cum_dist']}{r}", cumulative("cum_dist", "dist"), "formula", MONEY)
        _put(ws, f"{FL['dpi']}{r}", f"=IF({FL['cum_called']}{r}>0,{FL['cum_dist']}{r}/{FL['cum_called']}{r},0)",
             "formula", MULT)  # fmt: skip
        for target, source in (("t1", "t1"), ("t2", "t2"), ("t3_gp", "t3_gp"), ("t3_lp", "t3_lp"),
                               ("t4_lp", "t4_lp"), ("t4_gp", "t4_gp"), ("carry", "gp"), ("carry_cash", "gp_cash")):  # fmt: skip
            _put(ws, f"{FL[target]}{r}", "=" + _pick(WF[source], r), "formula", MONEY)
        _put(ws, f"{FL['gp_called']}{r}", f"={S_CASH}!{CF['gp_called']}{r}", "link", MONEY)
        _put(ws, f"{FL['gp_dist']}{r}", f"={S_CASH}!{CF['gp_dist']}{r}", "link", MONEY)
        _put(ws, f"{FL['fee']}{r}", f"={S_CASH}!{CF['fee_net']}{r}", "link", MONEY)
        _put(ws, f"{FL['gross']}{r}", f"={S_CASH}!{CF['proc']}{r}-{S_CASH}!{CF['inv']}{r}", "formula", MONEY)
    flow_columns = ["called", "dist", "net", "t1", "t2", "t3_gp", "t3_lp", "t4_lp", "t4_gp", "carry", "carry_cash",
                    "gp_called", "gp_dist", "fee", "gross"]  # fmt: skip
    _total_row(ws, last + 1, ROW0, last, {FL[c]: MONEY for c in flow_columns})
    _widths(ws, {get_column_letter(c): 13.5 for c in range(1, len(FL) + 1)} | {"A": 9, "B": 11, "C": 7})
    ws.freeze_panes = f"C{ROW0}"
    return last


def _first_nonzero_row(values: np.ndarray) -> int:
    nonzero = np.nonzero(np.abs(values) > 1e-9)[0]
    return ROW0 + (int(nonzero[0]) if len(nonzero) else 0)


def _returns_sheet(wb: Workbook, bundle: dict[str, Any], last: int) -> dict[str, str]:
    ws = wb.create_sheet(S_RETURNS)
    result: FundResult = bundle["result"]
    fund = result.config.fund
    unit = f"{fund.currency} {fund.unit}"
    _title(ws, "Returns and LP / GP economics - live formulas",
           f"Liquidation basis, under the waterfall selected on Inputs ({unit}).")  # fmt: skip
    a = result.arrays

    def col(name: str) -> str:
        return f"{S_FLOWS}!${FL[name]}${ROW0}:${FL[name]}${last}"

    def total(name: str) -> str:
        return f"SUM({col(name)})"

    # Excel's XIRR misbehaves when the first cash flow is zero, so each range starts at the first non-zero flow.
    net_first = _first_nonzero_row(a["lp_contributions"] - a["lp_distributions"])
    gross_first = _first_nonzero_row(a["gross_proceeds"] - a["investments"])
    net_irr = (
        f"=IFERROR(XIRR({S_FLOWS}!${FL['net']}${net_first}:${FL['net']}${last},"
        f"{S_FLOWS}!${FL['date']}${net_first}:${FL['date']}${last}),0)"
    )
    gross_irr = (
        f"=IFERROR(XIRR({S_FLOWS}!${FL['gross']}${gross_first}:${FL['gross']}${last},"
        f"{S_FLOWS}!${FL['date']}${gross_first}:${FL['date']}${last}),0)"
    )
    s0 = last + 3

    def summary(key: str) -> str:
        return _pick("$E$", s0 + SUMMARY[key])

    refs: dict[str, str] = {}
    row = 4

    def section(label: str) -> None:
        nonlocal row
        row += 1
        _put(ws, f"A{row}", label, "section", fill=FILL_SUB)
        for column in "BC":
            ws[f"{column}{row}"].fill = FILL_SUB
        row += 1

    def line(key: str, label: str, formula: str, fmt: str, note: str = "", bold: bool = False) -> None:
        nonlocal row
        _put(ws, f"A{row}", label, "bold" if bold else "label")
        _put(ws, f"B{row}", formula, "formula", fmt, bold=bold, align=Alignment(horizontal="right"))
        _put(ws, f"C{row}", note, "note")
        refs[key] = f"{S_RETURNS}!$B${row}"
        row += 1

    def at(key: str) -> str:
        return refs[key].split("!")[1]

    section("Net performance (limited partners)")
    line("waterfall", "Waterfall in use", '=IF(wf_type="european","Whole-of-fund (European)","Deal-by-deal (American)")',
         "@", "Selected on the Inputs sheet.")  # fmt: skip
    line("paid_in", "Paid-in capital", f"={total('called')}", MONEY, "Capital called from LPs.")
    line("distributed", "Distributions", f"={total('dist')}", MONEY, "Including any clawback received.")
    line("net_profit", "Net profit", f"={at('distributed')}-{at('paid_in')}", MONEY)
    line("dpi", "DPI", f"=IF({at('paid_in')}>0,{at('distributed')}/{at('paid_in')},0)", MULT,
         "Distributions / paid-in.")  # fmt: skip
    line("tvpi", "Net TVPI", f"={at('dpi')}", MULT, "Equal to DPI at liquidation: nothing is left unrealised.", True)
    line("net_irr", "Net IRR", net_irr, PCT, "XIRR of LP capital calls and distributions.", True)

    section("Gross performance (portfolio)")
    line("invested", "Invested capital", f"=SUM({S_CASH}!${CF['inv']}${ROW0}:${CF['inv']}${last})", MONEY)
    line("proceeds", "Gross proceeds", f"=SUM({S_CASH}!${CF['proc']}${ROW0}:${CF['proc']}${last})", MONEY)
    line("gross_profit", "Gross profit", f"={at('proceeds')}-{at('invested')}", MONEY)
    line("gross_moic", "Gross MOIC", f"=IF({at('invested')}>0,{at('proceeds')}/{at('invested')},0)", MULT, "", True)
    line("gross_irr", "Gross IRR", gross_irr, PCT, "XIRR of deal-level cash flows, before any fund cost.", True)
    line("spread", "Gross-to-net IRR spread (bps)", f"=({at('gross_irr')}-{at('net_irr')})*10000", BPS)

    section("Fund costs and carried interest")
    line("fees", "Management fees, net of offsets", f"={total('fee')}", MONEY)
    line("expenses", "Partnership expenses and line interest",
         f"=SUM({S_CASH}!${CF['expenses']}${ROW0}:${CF['expenses']}${last})"
         f"+SUM({S_CASH}!${CF['line_int']}${ROW0}:${CF['line_int']}${last})", MONEY)  # fmt: skip
    line("carry_gross", "Carried interest distributed", "=" + summary("carry"), MONEY)
    line("clawback", "Clawback paid by the GP", "=" + summary("payable"), MONEY)
    line("carry_net", "Carried interest, net of clawback", "=" + summary("carry_net"), MONEY, "", True)
    line("shortfall", "LP shortfall from the after-tax cap", "=" + summary("shortfall"), MONEY)
    line("carry_share", "Carry as a share of LP-class profit",
         f"=IF({at('net_profit')}+{at('carry_net')}>0,{at('carry_net')}/({at('net_profit')}+{at('carry_net')}),0)", PCT,
         "Equals the carry rate once the GP is fully caught up.")  # fmt: skip

    section("Who receives the gross profit")
    line("gp_commit_profit", "GP commitment profit", f"={total('gp_dist')}-{total('gp_called')}", MONEY)
    line("gp_total", "GP and manager total economics", f"={at('fees')}+{at('carry_net')}+{at('gp_commit_profit')}",
         MONEY, "Fees + carry + profit on the GP commitment.")  # fmt: skip
    line(
        "lp_share",
        "LP share of gross profit",
        f"=IF({at('gross_profit')}<>0,{at('net_profit')}/{at('gross_profit')},0)",
        PCT,
    )
    line("gp_share", "GP and manager share of gross profit",
         f"=IF({at('gross_profit')}<>0,{at('gp_total')}/{at('gross_profit')},0)", PCT)  # fmt: skip
    line("cost_share", "Expenses share of gross profit",
         f"=IF({at('gross_profit')}<>0,{at('expenses')}/{at('gross_profit')},0)", PCT)  # fmt: skip
    line("split_check", "Split check (should be zero)",
         f"={at('gross_profit')}-{at('net_profit')}-{at('gp_total')}-{at('expenses')}", MONEY2,
         "Gross profit less every recipient's share.")  # fmt: skip

    valuation = bundle["valuation"]
    section(f"Interim snapshot at {valuation['as_of']:%d %b %Y} (Python engine, configured terms)")
    snapshot = [
        ("Paid-in capital", valuation["paid_in"], MONEY), ("Distributions", valuation["distributed"], MONEY),
        ("Net asset value (after accrued carry)", valuation["nav"], MONEY), ("DPI", valuation["dpi"], MULT),
        ("RVPI", valuation["rvpi"], MULT), ("Net TVPI", valuation["tvpi"], MULT),
        ("Net IRR", valuation["net_irr"], PCT), ("Gross MOIC", valuation["gross_moic"], MULT),
        ("Gross IRR", valuation["gross_irr"], PCT), ("Accrued carried interest", valuation["carry_accrued"], MONEY),
        ("Paid-in as a share of LP commitments", valuation["paid_in_pct_of_commitment"], PCT),
    ]  # fmt: skip
    for label, value, fmt in snapshot:
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", value, "python", fmt, align=Alignment(horizontal="right"))
        row += 1
    _put(ws, f"C{row - len(snapshot)}", "Values from the Python run; they do not move with the yellow levers.", "note")
    _widths(ws, {"A": 46, "B": 26, "C": 70})
    ws.sheet_properties.tabColor = "70AD47"
    return refs


def _annual_sheet(wb: Workbook, result: FundResult, last: int) -> tuple[int, int]:
    ws = wb.create_sheet(S_ANNUAL)
    unit = f"{result.config.fund.currency} {result.config.fund.unit}"
    _title(ws, "Annual summary - live formulas", f"Calendar-year totals of the quarterly flows ({unit}).")
    _header(ws, 5, ["Year", "LP capital called", "LP distributions", "LP net cash flow", "Return of capital",
                    "Preferred return", "LP share of profit split", "GP carried interest", "Management fee (net)",
                    "Gross deal cash flow"], height=42)  # fmt: skip
    years = sorted({d.year for d in result.dates})
    year_range = f"{S_FLOWS}!${FL['year']}${ROW0}:${FL['year']}${last}"

    def sumifs(name: str, row: int) -> str:
        return f"SUMIFS({S_FLOWS}!${FL[name]}${ROW0}:${FL[name]}${last},{year_range},$A{row})"

    first = 6
    for i, year in enumerate(years):
        row = first + i
        _put(ws, f"A{row}", year, "label", INDEX)
        _put(ws, f"B{row}", "=" + sumifs("called", row), "formula", MONEY)
        _put(ws, f"C{row}", "=" + sumifs("dist", row), "formula", MONEY)
        _put(ws, f"D{row}", f"=C{row}-B{row}", "formula", MONEY)
        _put(ws, f"E{row}", "=" + sumifs("t1", row), "formula", MONEY)
        _put(ws, f"F{row}", "=" + sumifs("t2", row), "formula", MONEY)
        _put(ws, f"G{row}", "=" + sumifs("t3_lp", row) + "+" + sumifs("t4_lp", row), "formula", MONEY)
        _put(ws, f"H{row}", "=" + sumifs("carry", row), "formula", MONEY)
        _put(ws, f"I{row}", "=" + sumifs("fee", row), "formula", MONEY)
        _put(ws, f"J{row}", "=" + sumifs("gross", row), "formula", MONEY)
    end = first + len(years) - 1
    _total_row(ws, end + 1, first, end, dict.fromkeys("BCDEFGHIJ", MONEY))
    _put(
        ws,
        f"A{end + 3}",
        "Tiers exclude any clawback, which is paid by the GP at liquidation and shown on Returns.",
        "note",
    )
    _widths(ws, {"A": 9} | dict.fromkeys("BCDEFGHIJ", 15))
    ws.freeze_panes = "B6"
    return first, end


def _lp_accounts_sheet(wb: Workbook, bundle: dict[str, Any], investors: tuple[int, int], refs: dict[str, str]) -> None:
    ws = wb.create_sheet(S_LPS)
    result: FundResult = bundle["result"]
    unit = f"{result.config.fund.currency} {result.config.fund.unit}"
    _title(ws, "LP capital accounts", f"Each LP's pro-rata share of the LP class ({unit}). Liquidation basis is live; "
                                      "the interim columns are Python values at the valuation date.")  # fmt: skip
    _header(ws, 5, ["Limited partner", "Commitment", "Share of LP class", "Paid-in", "Distributions", "Net profit",
                    "DPI", "Net IRR", "Interim paid-in", "Interim distributions", "Interim NAV", "Interim TVPI"],
            height=42)  # fmt: skip
    _put(ws, "D4", "At liquidation (live)", "section")
    _put(ws, "I4", f"At {bundle['valuation']['as_of']:%d %b %Y} (Python engine)", "section")
    first_inv, last_inv = investors
    interim = bundle["investors"]
    if interim is None or interim.empty:
        _put(ws, "A6", "No investor register was supplied.", "note")
        return
    for i, (_, record) in enumerate(interim.iterrows()):
        row, source = 6 + i, first_inv + i
        _put(ws, f"A{row}", f"={S_INVESTORS}!A{source}", "link")
        _put(ws, f"B{row}", f"={S_INVESTORS}!B{source}", "link", MONEY)
        _put(ws, f"C{row}", f"={S_INVESTORS}!C{source}", "link", PCT)
        _put(ws, f"D{row}", f"=C{row}*{refs['paid_in']}", "formula", MONEY)
        _put(ws, f"E{row}", f"=C{row}*{refs['distributed']}", "formula", MONEY)
        _put(ws, f"F{row}", f"=E{row}-D{row}", "formula", MONEY)
        _put(ws, f"G{row}", f"=IF(D{row}>0,E{row}/D{row},0)", "formula", MULT)
        _put(ws, f"H{row}", f"={refs['net_irr']}", "link", PCT)
        _put(ws, f"I{row}", record["paid_in"], "python", MONEY)
        _put(ws, f"J{row}", record["distributions"], "python", MONEY)
        _put(ws, f"K{row}", record["nav"], "python", MONEY)
        _put(ws, f"L{row}", record["tvpi"], "python", MULT)
    end = 6 + len(interim) - 1
    _total_row(ws, end + 1, 6, end, dict.fromkeys("BDEFIJK", MONEY))
    _put(ws, f"C{end + 1}", f"=SUM(C6:C{end})", "formula", PCT, FILL_TOTAL, bold=True)
    _put(ws, f"A{end + 3}", "Every LP invests on the same terms, so each takes its commitment share of every flow; "
                            "all LPs therefore share the same DPI and IRR.", "note")  # fmt: skip
    _widths(ws, {"A": 40} | dict.fromkeys("BCDEFGHIJKL", 14))
    del last_inv


# --------------------------------------------------------------------------- #
# Engine-output sheets
# --------------------------------------------------------------------------- #
def _write_table(ws: Worksheet, row: int, frame: pd.DataFrame, headers: list[str], formats: list[str | None],
                 kind: str = "python") -> int:  # fmt: skip
    _header(ws, row, headers, height=42)
    for i, (_, record) in enumerate(frame.iterrows()):
        for j, (value, fmt) in enumerate(zip(record.tolist(), formats, strict=True)):
            cell_kind = "label" if j == 0 else kind
            _put(ws, f"{get_column_letter(j + 1)}{row + 1 + i}", value, cell_kind, fmt,
                 align=WRAP if isinstance(value, str) and len(value) > 40 else None)  # fmt: skip
    return row + len(frame)


def _bridge_sheet(wb: Workbook, bundle: dict[str, Any]) -> None:
    ws = wb.create_sheet(S_BRIDGE)
    result: FundResult = bundle["result"]
    unit = f"{result.config.fund.currency} {result.config.fund.unit}"
    _title(
        ws, "Gross-to-net bridge and value creation", f"Values from the Python engine at the configured terms ({unit})."
    )
    _put(
        ws, "A4", "Gross-to-net bridge: each step re-measures LP returns with one more fund cost switched on", "section"
    )
    bridge = bundle["bridge"][["step", "irr_after", "irr_impact_bps", "multiple_after", "multiple_impact"]]
    end = _write_table(ws, 5, bridge, ["Step", "IRR after step", "IRR impact (bps)", "Multiple after step",
                                       "Multiple impact"], [None, PCT, BPS, MULT, MULT])  # fmt: skip
    row = end + 3
    _put(ws, f"A{row}", "Split of gross profit", "section")
    split = bundle["value_split"][["component", "recipient", "amount", "share_of_gross_profit"]]
    end = _write_table(ws, row + 1, split, ["Component", "Recipient", "Amount", "Share of gross profit"],
                       [None, None, MONEY, PCT])  # fmt: skip
    _put(ws, f"A{end + 1}", "Total", "bold", fill=FILL_TOTAL)
    _put(ws, f"C{end + 1}", f"=SUM(C{row + 2}:C{end})", "formula", MONEY, FILL_TOTAL, bold=True)
    _put(ws, f"D{end + 1}", f"=SUM(D{row + 2}:D{end})", "formula", PCT, FILL_TOTAL, bold=True)
    row = end + 4
    _put(ws, f"A{row}", "Value creation: drivers of the portfolio's gross gain", "section")
    drivers = bundle["value_creation"][["driver", "amount", "share_of_gain"]]
    end = _write_table(ws, row + 1, drivers, ["Driver", "Amount", "Share of gain"], [None, MONEY, PCT])
    _put(ws, f"A{end + 1}", "Total", "bold", fill=FILL_TOTAL)
    _put(ws, f"B{end + 1}", f"=SUM(B{row + 2}:B{end})", "formula", MONEY, FILL_TOTAL, bold=True)
    _put(ws, f"C{end + 1}", f"=SUM(C{row + 2}:C{end})", "formula", PCT, FILL_TOTAL, bold=True)
    row = end + 4
    _put(ws, f"A{row}", "LP and GP economics over the life of the fund", "section")
    economics = bundle["economics"][["party", "contributions", "distributions", "profit", "multiple", "irr",
                                     "share_of_gross_profit"]]  # fmt: skip
    end = _write_table(ws, row + 1, economics, ["Party", "Contributions", "Distributions", "Profit", "Multiple", "IRR",
                                                "Share of gross profit"],
                       [None, MONEY, MONEY, MONEY, MULT, PCT, PCT])  # fmt: skip
    row = end + 3
    _put(ws, f"A{row}", "Whole-of-fund versus deal-by-deal", "section")
    comparison = bundle["comparison"]["table"][[
        "waterfall", "net_irr", "tvpi", "carry_gross", "clawback_paid", "carry_net", "first_carry_date",
        "average_carry_timing_years", "carry_present_value",
    ]]  # fmt: skip
    _write_table(ws, row + 1, comparison, ["Waterfall", "Net IRR", "Net TVPI", "Carry distributed", "Clawback paid",
                                           "Carry, net", "First carry date", "Average carry timing (years)",
                                           "Carry present value at 10%"],
                 [None, PCT, MULT, MONEY, MONEY, MONEY, DATE, YEARS, MONEY])  # fmt: skip
    _widths(ws, {"A": 44, "B": 16, "C": 16, "D": 18, "E": 16, "F": 14, "G": 18, "H": 18, "I": 18})


def _scenarios_sheet(wb: Workbook, bundle: dict[str, Any]) -> tuple[int, int] | None:
    table = bundle.get("scenario_table")
    if table is None:
        return None
    ws = wb.create_sheet(S_SCEN)
    _title(ws, "Scenarios", "Each row is a full re-run of the Python fund model with the overrides described. "
                            "Liquidation basis.")  # fmt: skip
    columns = ["label", "description", "waterfall", "gross_moic", "gross_irr", "net_irr", "tvpi", "carry_net",
               "clawback_paid", "total_management_fees", "total_lp_profit", "first_carry_year", "fund_life_years"]  # fmt: skip
    headers = ["Scenario", "What changes", "Waterfall", "Gross MOIC", "Gross IRR", "Net IRR", "Net TVPI",
               "Carry, net of clawback", "Clawback paid", "Management fees", "LP net profit",
               "First carry (year of fund life)", "Fund life (years)", "Net IRR vs base (bps)", "Net TVPI vs base"]  # fmt: skip
    formats = [None, None, None, MULT, PCT, PCT, MULT, MONEY, MONEY, MONEY, MONEY, YEARS, YEARS]
    end = _write_table(ws, 5, table[columns], headers[:13], formats)
    _header(ws, 5, headers, height=48)
    for row in range(6, end + 1):
        _put(ws, f"N{row}", f"=(F{row}-$F$6)*10000", "formula", BPS)
        _put(ws, f"O{row}", f"=G{row}-$G$6", "formula", MULT)
        ws.row_dimensions[row].height = 30
    _widths(ws, {"A": 26, "B": 70, "C": 12} | dict.fromkeys("DEFGHIJKLMNO", 14))
    ws.freeze_panes = "B6"
    return 6, end


def _sensitivity_sheet(wb: Workbook, bundle: dict[str, Any], currency: str) -> None:
    grids = bundle.get("grids")
    if not grids:
        return
    ws = wb.create_sheet(S_SENS)
    _title(ws, "Sensitivity tables", "Every cell is a full re-run of the Python fund model. Darker blue = higher. "
                                     f"Money in {currency}.")  # fmt: skip
    unit_formats = {"pct": PCT, "multiple": MULT, "money": MONEY, "years": YEARS}
    axis_formats = {"pct": "0.0%;-0.0%;0.0%", "pct_points": "+0.0%;-0.0%;0.0%", "quarters": "+0;-0;0",
                    "multiple": MULT}  # fmt: skip
    row = 4
    for grid in grids.values():
        attrs = grid.attrs
        _put(ws, f"A{row}", f"{attrs['metric_label']}: {attrs['x_label']} (across) against {attrs['y_label']} (down)",
             "section")  # fmt: skip
        row += 1
        _put(ws, f"A{row}", f"{attrs['y_label']} \\ {attrs['x_label']}", "bold", fill=FILL_SUB, align=WRAP)
        for j, x_value in enumerate(grid.columns):
            _put(ws, f"{get_column_letter(j + 2)}{row}", float(x_value), "bold", axis_formats.get(attrs["x_unit"]),
                 FILL_SUB, Alignment(horizontal="center"))  # fmt: skip
        fmt = unit_formats.get(attrs["metric_unit"], "0.00")
        for i, y_value in enumerate(grid.index):
            _put(ws, f"A{row + 1 + i}", float(y_value), "bold", axis_formats.get(attrs["y_unit"]), FILL_SUB,
                 Alignment(horizontal="center"))  # fmt: skip
            for j in range(grid.shape[1]):
                _put(ws, f"{get_column_letter(j + 2)}{row + 1 + i}", float(grid.iat[i, j]), "python", fmt,
                     align=Alignment(horizontal="center"))  # fmt: skip
        first, last = row + 1, row + grid.shape[0]
        area = f"B{first}:{get_column_letter(grid.shape[1] + 1)}{last}"
        ws.conditional_formatting.add(
            area, ColorScaleRule(start_type="min", start_color="F7FBFF", end_type="max", end_color="6DA7EC")
        )
        row = last + 3
    _widths(ws, {"A": 30} | {get_column_letter(c): 12 for c in range(2, 12)})


def _monte_carlo_sheet(wb: Workbook, bundle: dict[str, Any]) -> None:
    stats = bundle.get("monte_carlo_stats")
    sims = bundle.get("monte_carlo")
    if stats is None or sims is None:
        return
    ws = wb.create_sheet(S_MC)
    settings = sims.attrs["settings"]
    _title(ws, "Monte Carlo simulation", "Values from the Python engine. Each trial redraws deal outcomes and re-runs "
                                         "the fund under both waterfalls.")  # fmt: skip
    _put(ws, "A4", "Simulation settings", "section")
    settings_rows = [
        ("Trials", settings.n_sims, "0"), ("Random seed", settings.seed, "0"),
        ("Market-wide exit multiple volatility", settings.market_vol, PCT),
        ("Deal-specific exit multiple volatility", settings.idiosyncratic_vol, PCT),
        ("Write-off probability per deal", settings.write_off_probability, PCT),
        ("Quarters of delay per one sigma of market weakness", settings.hold_shift_per_market_sigma, YEARS),
        ("Deal-specific holding period noise (quarters, +/-)", settings.hold_shift_noise_quarters, "0"),
    ]  # fmt: skip
    row = 5
    for label, value, fmt in settings_rows:
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", value, "python", fmt)
        row += 1
    row += 1
    _put(ws, f"A{row}", "Results", "section")
    row += 1
    labels = [
        ("net_irr_mean", "Net IRR, mean", PCT), ("net_irr_median", "Net IRR, median", PCT),
        ("net_irr_p5", "Net IRR, 5th percentile", PCT), ("net_irr_p25", "Net IRR, 25th percentile", PCT),
        ("net_irr_p75", "Net IRR, 75th percentile", PCT), ("net_irr_p95", "Net IRR, 95th percentile", PCT),
        ("tvpi_mean", "Net TVPI, mean", MULT), ("tvpi_p5", "Net TVPI, 5th percentile", MULT),
        ("tvpi_p95", "Net TVPI, 95th percentile", MULT), ("gross_moic_mean", "Gross MOIC, mean", MULT),
        ("prob_net_irr_below_pref", "Probability net IRR is below the hurdle", PCT),
        ("prob_capital_loss", "Probability of LP capital loss (TVPI below 1.0x)", PCT),
        ("prob_no_carry", "Probability no carry is earned", PCT),
        ("carry_mean", "Carry, mean", MONEY), ("carry_p5", "Carry, 5th percentile", MONEY),
        ("carry_p95", "Carry, 95th percentile", MONEY),
        ("prob_clawback_american", "Probability of a material clawback, deal-by-deal", PCT),
        ("clawback_mean_when_triggered", "Average clawback when triggered, deal-by-deal", MONEY),
        ("clawback_max", "Largest clawback, deal-by-deal", MONEY),
        ("clawback_shortfall_mean_when_triggered", "Average LP shortfall from the after-tax cap, when triggered", MONEY),
        ("prob_clawback_european", "Probability of a material clawback, whole-of-fund", PCT),
        ("american_irr_drag_mean", "Average LP IRR cost of deal-by-deal versus whole-of-fund", PCT2),
        ("material_clawback_threshold", "Materiality threshold for a clawback", MONEY),
    ]  # fmt: skip
    for key, label, fmt in labels:
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", stats[key], "python", fmt)
        row += 1
    _widths(ws, {"A": 62, "B": 16})


def _commentary_sheet(wb: Workbook, commentary: str | None) -> None:
    ws = wb.create_sheet(S_AI)
    _title(ws, "AI commentary", "Generated from the model outputs by the commentary layer. Regenerate with "
                                "`pefund commentary`.")  # fmt: skip
    ws.column_dimensions["A"].width = 130
    text = commentary or "No commentary was supplied when this workbook was built."
    row = 4
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            _put(ws, f"A{row}", stripped.lstrip("#").strip(), "section")
        else:
            cleaned = stripped.replace("**", "")
            _put(ws, f"A{row}", cleaned, "label", align=WRAP)
            if len(cleaned) > 125:
                ws.row_dimensions[row].height = 13.5 * (len(cleaned) // 125 + 1)
        row += 1
    ws.sheet_view.showGridLines = False


# --------------------------------------------------------------------------- #
# Checks: Excel formulas against the Python engine
# --------------------------------------------------------------------------- #
def _checks_sheet(wb: Workbook, bundle: dict[str, Any], last: int, refs: dict[str, str]) -> None:
    ws = wb.create_sheet(S_CHECKS)
    cfg: FundConfig = bundle["result"].config
    _title(ws, "Checks: Excel formulas against the Python engine",
           "Each Excel total is compared with the value the Python engine produced for the same inputs.")  # fmt: skip
    s0 = last + 3
    runs = bundle["comparison"]["runs"]

    _put(ws, "A4", "Levers at the time of the Python run", "section")
    _header(ws, 5, ["Lever", "Now (Excel)", "Python run", "Unchanged?"], height=24)
    levers = [
        ("Waterfall type", "wf_type", cfg.waterfall.type, None),
        ("Preferred return", "pref_rate", cfg.waterfall.pref_rate, PCT2),
        ("Compounding", "pref_comp", cfg.waterfall.pref_compounding, None),
        ("Catch-up rate", "catch_up", cfg.waterfall.catch_up_rate, PCT2),
        ("Carry rate", "carry_rate", cfg.waterfall.carry_rate, PCT2),
        ("Clawback enabled", "cb_enabled", cfg.clawback.enabled, None),
        ("After-tax limit", "cb_after_tax", cfg.clawback.after_tax_limit, None),
        ("Assumed tax rate", "cb_tax_rate", cfg.clawback.assumed_tax_rate, PCT2),
        ("Carry escrow", "escrow_pct", cfg.clawback.escrow_pct, PCT2),
    ]  # fmt: skip
    row = 6
    for label, name, value, fmt in levers:
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", f"={name}", "link", fmt, align=RIGHT)
        _put(ws, f"C{row}", value, "python", fmt, align=RIGHT)
        _put(ws, f"D{row}", f'=IF(B{row}=C{row},"Yes","Changed")', "formula", align=MIDDLE)
        row += 1
    lever_first, lever_last = 6, row - 1
    row += 1
    _put(ws, f"A{row}", "Levers unchanged since the Python run?", "bold")
    _put(ws, f"B{row}", f'=IF(COUNTIF(D{lever_first}:D{lever_last},"Changed")=0,"Yes","No")', "formula",
         align=Alignment(horizontal="right"), bold=True)  # fmt: skip
    unchanged = f"$B${row}"
    _put(
        ws,
        f"C{row}",
        "If No, differences below are expected: Excel has recalculated, the Python values have not.",
        "note",
        align=INDENT,
    )

    row += 2
    _put(ws, f"A{row}", "Reconciliation", "section")
    row += 1
    _header(ws, row, ["Item", "Excel (formula)", "Python (value)", "Difference", "Status"], height=24)
    first_check = row + 1
    row = first_check

    def check(label: str, formula: str, value: float, fmt: str = MONEY2, tolerance: float = 0.000001) -> None:
        nonlocal row
        _put(ws, f"A{row}", label)
        _put(ws, f"B{row}", formula, "formula", fmt)
        _put(ws, f"C{row}", value, "python", fmt)
        _put(ws, f"D{row}", f"=B{row}-C{row}", "formula", "0.000000;-0.000000;0.000000")
        _put(ws, f"E{row}", f'=IF(ABS(D{row})<={tolerance},"OK",IF({unchanged}="Yes","DIFFERENCE","Lever changed"))',
             "formula", align=MIDDLE)  # fmt: skip
        row += 1

    def heading(label: str) -> None:
        nonlocal row
        _put(ws, f"A{row}", label, "bold", fill=FILL_SUB)
        for column in "BCDE":
            ws[f"{column}{row}"].fill = FILL_SUB
        row += 1

    for sheet, kind, label in (
        (S_EU, "european", "Whole-of-fund waterfall"),
        (S_DBD, "american", "Deal-by-deal waterfall"),
    ):
        run: FundResult = runs[kind]
        totals = run.totals
        heading(label)

        def total(column: str, sheet: str = sheet) -> str:
            return f"=SUM({sheet}!{WF[column]}{ROW0}:{WF[column]}{last})"

        def cell(key: str, sheet: str = sheet) -> str:
            return f"={sheet}!$E${s0 + SUMMARY[key]}"

        check("Tier 1 Return of capital", total("t1"), totals["tier1_return_of_capital"])
        check("Tier 2 Preferred return", total("t2"), totals["tier2_preferred_return"])
        check("Tier 3 Catch-up", total("t3"), totals["tier3_catch_up"])
        check("Tier 4 to LPs", total("t4_lp"), totals["tier4_split_lp"])
        check("Tier 4 to GP", total("t4_gp"), totals["tier4_split_gp"])
        check("Carried interest distributed", cell("carry"), totals["carry_gross"])
        check("Whole-fund pref accrued (clawback test)", cell("pref_accrued"), totals["whole_fund_pref_accrued"])
        check("Clawback before cap", cell("uncapped"), totals["clawback_uncapped"])
        check("Clawback payable", cell("payable"), totals["clawback_paid"])
        check("Carried interest, net of clawback", cell("carry_net"), totals["carry_net"])
        check("LP distributions incl. clawback", cell("lp_incl"), totals["total_lp_distributions"])
        check("Sum of tier checks (should be zero)", total("check"), 0.0)

    heading(f"Returns (Python: {cfg.waterfall.type} waterfall, as configured)")
    summary = bundle["summary"]
    check("Paid-in capital", f"={refs['paid_in']}", summary["paid_in"])
    check("LP distributions", f"={refs['distributed']}", summary["distributed"])
    check("Net TVPI", f"={refs['tvpi']}", summary["tvpi"], '0.0000"x"')
    check("Net IRR", f"={refs['net_irr']}", summary["net_irr"], "0.0000%")
    check("Gross MOIC", f"={refs['gross_moic']}", summary["gross_moic"], '0.0000"x"')
    check("Gross IRR", f"={refs['gross_irr']}", summary["gross_irr"], "0.0000%")
    check("Management fees", f"={refs['fees']}", summary["total_management_fees"])
    check("GP total economics", f"={refs['gp_total']}", summary["gp_total_economics"])
    check("Split of gross profit (should be zero)", f"={refs['split_check']}", 0.0)
    heading("Engine inputs")
    check("Fee check: formula less engine gross fee",
          f"=SUM({S_CASH}!{CF['fee_check']}{ROW0}:{CF['fee_check']}{last})-SUM({S_CASH}!{CF['fee_gross']}{ROW0}:{CF['fee_gross']}{last})",
          0.0)  # fmt: skip
    check("Capital by purpose less LP capital called",
          f"=SUM({S_DBD_IN}!C{last + 1}:{get_column_letter(2 + len(runs['american'].kind_names))}{last + 1})"
          f"-SUM({S_CASH}!{CF['lp_called']}{ROW0}:{CF['lp_called']}{last})", 0.0)  # fmt: skip
    last_check = row - 1

    row += 1
    _put(ws, f"A{row}", "Overall", "bold")
    _put(ws, f"B{row}", (
        f'=IF(COUNTIF(E{first_check}:E{last_check},"DIFFERENCE")>0,"REVIEW: Excel and Python disagree",'
        f'IF(COUNTIF(E{first_check}:E{last_check},"Lever changed")>0,'
        '"Levers changed in Excel: Python values are from the original run","All checks pass"))'
    ), "formula", bold=True)  # fmt: skip
    _name(wb, "checks_status", S_CHECKS, f"B{row}")
    ws.conditional_formatting.add(
        f"E{first_check}:E{last_check}",
        CellIsRule(operator="equal", formula=['"DIFFERENCE"'], font=Font(name=FONT, color="FF0000", bold=True)),
    )
    _widths(ws, {"A": 46, "B": 22, "C": 18, "D": 16, "E": 18})
    ws.sheet_properties.tabColor = "C00000"


# --------------------------------------------------------------------------- #
# Dashboard and README
# --------------------------------------------------------------------------- #
def _style_chart(chart: Any, title: str, y_title: str | None = None, x_title: str | None = None) -> None:
    chart.title = title
    chart.title.overlay = False
    chart.roundedCorners = False
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    if chart.legend is not None:
        chart.legend.overlay = False
        chart.legend.position = "b"
    if y_title:
        chart.y_axis.title = y_title
        chart.y_axis.title.overlay = False
    if x_title:
        chart.x_axis.title = x_title
        chart.x_axis.title.overlay = False
    chart.height, chart.width = 8.4, 16.5


def _dashboard_sheet(wb: Workbook, bundle: dict[str, Any], refs: dict[str, str], last: int, annual: tuple[int, int],
                     deals: tuple[int, int], scenarios: tuple[int, int] | None) -> None:  # fmt: skip
    ws = wb.create_sheet(S_DASH)
    result: FundResult = bundle["result"]
    fund = result.config.fund
    unit = f"{fund.currency} {fund.unit}"
    _put(ws, "A1", fund.name, "title")
    _put(
        ws,
        "A2",
        f"Fund economics dashboard - liquidation basis, {unit}. KPIs and the first two charts are live.",
        "note",
    )
    ws.row_dimensions[1].height = 24
    tiles = [
        ("Net IRR", "net_irr", PCT), ("Net TVPI", "tvpi", MULT), ("Gross IRR", "gross_irr", PCT),
        ("Gross MOIC", "gross_moic", MULT), ("Carry, net of clawback", "carry_net", MONEY),
        ("Management fees", "fees", MONEY), ("GP share of gross profit", "gp_share", PCT),
    ]  # fmt: skip
    for i, (label, key, fmt) in enumerate(tiles):
        column = get_column_letter(1 + 2 * i)
        _put(ws, f"{column}4", label, "note")
        cell = ws[f"{column}5"]
        cell.value, cell.font, cell.number_format = f"={refs[key]}", F_KPI, fmt
        cell.alignment = Alignment(horizontal="left")
    _put(ws, "A7", "Waterfall in use:", "note")
    _put(ws, "C7", f"={refs['waterfall']}", "link")
    _put(ws, "G7", "Model integrity:", "note")
    _put(ws, "I7", "=checks_status", "link")
    ws.row_dimensions[5].height = 26

    palette = {"blue": "2A78D6", "orange": "EB6834", "aqua": "1BAF7A", "neutral": "C3C2B7"}

    # 1. J-curve (live)
    line = LineChart()
    line.add_data(Reference(wb[S_FLOWS], min_col=7, min_row=ROW0 - 1, max_row=last), titles_from_data=True)
    line.set_categories(Reference(wb[S_FLOWS], min_col=2, min_row=ROW0, max_row=last))
    _style_chart(line, "J-curve: cumulative LP net cash flow", unit)
    line.series[0].graphicalProperties.line.solidFill = palette["aqua"]
    line.series[0].graphicalProperties.line.width = 22000
    line.series[0].smooth = False
    line.x_axis.number_format = "mmm-yy"
    line.x_axis.tickLblSkip = 4
    line.x_axis.tickLblPos = "low"  # keep date labels at the bottom, not on the zero line
    line.y_axis.number_format = "#,##0"
    line.legend = None
    ws.add_chart(line, "A9")

    # 2. Distributions by tier and year (live)
    first_year, last_year = annual
    tiers = BarChart()
    tiers.type, tiers.grouping, tiers.overlap = "col", "stacked", 100
    tiers.add_data(Reference(wb[S_ANNUAL], min_col=5, max_col=8, min_row=5, max_row=last_year), titles_from_data=True)
    tiers.set_categories(Reference(wb[S_ANNUAL], min_col=1, min_row=first_year, max_row=last_year))
    _style_chart(tiers, "Distributions by waterfall tier", unit)
    tiers.gapWidth = 70
    tiers.y_axis.number_format = "#,##0"
    for series, colour in zip(tiers.series, ("neutral", "blue", "aqua", "orange"), strict=True):
        series.graphicalProperties.solidFill = palette[colour]
        series.graphicalProperties.line.solidFill = "FFFFFF"
    ws.add_chart(tiers, "J9")

    # 3. Deal MOIC (engine values)
    first_deal, last_deal = deals
    moic = BarChart()
    moic.type = "bar"
    moic.add_data(Reference(wb[S_DEALS], min_col=DEAL_OUTPUT_COL + 3, min_row=DEAL_HEADER_ROW, max_row=last_deal),
                  titles_from_data=True)  # fmt: skip
    moic.set_categories(Reference(wb[S_DEALS], min_col=1, min_row=first_deal, max_row=last_deal))
    _style_chart(moic, "Gross MOIC by deal", None, None)
    moic.series[0].graphicalProperties.solidFill = palette["blue"]
    moic.gapWidth = 70
    moic.x_axis.scaling.orientation = "maxMin"  # first deal at the top
    moic.y_axis.tickLblPos = "low"
    moic.legend = None
    moic.y_axis.number_format = '0.0"x"'
    ws.add_chart(moic, "A27")

    # 4. Scenario net IRR (engine values)
    if scenarios is not None:
        first_scenario, last_scenario = scenarios
        scen = BarChart()
        scen.type = "bar"
        scen.add_data(Reference(wb[S_SCEN], min_col=6, min_row=5, max_row=last_scenario), titles_from_data=True)
        scen.set_categories(Reference(wb[S_SCEN], min_col=1, min_row=first_scenario, max_row=last_scenario))
        _style_chart(scen, "Net IRR by scenario", None, None)
        scen.series[0].graphicalProperties.solidFill = palette["blue"]
        scen.gapWidth = 70
        scen.x_axis.scaling.orientation = "maxMin"  # base case at the top
        scen.y_axis.tickLblPos = "low"
        scen.legend = None
        scen.y_axis.number_format = "0%"
        ws.add_chart(scen, "J27")
    for column in range(1, 20):
        ws.column_dimensions[get_column_letter(column)].width = 11.5
    ws.sheet_view.showGridLines = False
    ws.sheet_properties.tabColor = NAVY


def _readme_sheet(wb: Workbook, bundle: dict[str, Any], source: str) -> None:
    ws = wb.active
    ws.title = S_README
    result: FundResult = bundle["result"]
    _put(ws, "A1", "Private equity fund waterfall and carry model", "title")
    _put(ws, "A2", f"{result.config.fund.name} - built {dt.date.today():%d %b %Y} from {source} by the pefund Python "
                   "engine.", "note")  # fmt: skip
    lines: list[tuple[str, str]] = [
        ("section", "What this workbook is"),
        ("label", "A fund-economics model in two halves. The Python engine projects the deals, fees and capital calls. "
                  "Excel then runs both distribution waterfalls, the escrow and the clawback test with live formulas, "
                  "and reconciles every total back to Python on the Checks sheet."),
        ("section", "How to use it"),
        ("label", "1. Change a yellow lever on Inputs (preferred return, carry, catch-up, compounding, waterfall type, "
                  "escrow, clawback terms). The waterfalls, Returns, Annual and Dashboard recalculate immediately."),
        ("label", "2. To change deals, fees, fund size or scenario shocks, edit the blue cells on Inputs, Deals or "
                  "Investors, save, and run:  pefund excel --config <this file> --out <new file>"),
        ("label", "3. Read Checks last. 'All checks pass' means Excel and Python agree to the cent. After step 1 it "
                  "reports that levers changed, which is expected until Python is re-run."),
        ("section", "Colour code"),
        ("input", "Blue text: an input you may change."),
        ("key", "Yellow fill: an input that recalculates this workbook live."),
        ("formula", "Black text: a formula."),
        ("link", "Green text: a link to another sheet."),
        ("python", "Grey text: a value written by the Python engine (refresh by re-running)."),
        ("section", "Sheets"),
        ("label", "Dashboard - headline KPIs and charts.            Inputs / Deals / Investors - every assumption."),
        ("label", "CashFlows - quarterly engine output.            WF_WholeFund - European waterfall as a running ledger."),
        ("label", "WF_DealByDeal - American waterfall with loss carryforward.      DbD_Inputs - its eligibility weights."),
        ("label", "LP_GP_Flows - cash flows for the selected waterfall.      Returns - IRR, multiples, LP / GP split."),
        ("label", "Annual - calendar-year roll-up.      LP_Accounts - each investor's capital account."),
        ("label", "Bridge - gross-to-net and value creation.      Scenarios / Sensitivity / MonteCarlo - engine analyses."),
        ("label", "AI_Commentary - generated narrative.      Checks - Excel against Python."),
        ("section", "Conventions"),
        ("label", f"Money is in {result.config.fund.currency} {result.config.fund.unit}. Zero shows as a dash and "
                  "negatives in brackets. Cash flows fall on quarter-ends; IRR is XIRR on those dates; preferred "
                  "return uses actual days / 365."),
        ("label", "Net = the limited partners' experience after fees, expenses and carried interest. "
                  "Gross = deal-level, before any fund cost."),
        ("section", "Important"),
        ("label", "The fund, investors and portfolio companies are hypothetical. The model is an analytical "
                  "illustration, not investment, legal or tax advice, and a real waterfall is governed by the "
                  "fund's limited partnership agreement."),
    ]  # fmt: skip
    row = 4
    for kind, text in lines:
        if kind == "section" and row > 4:
            row += 1  # blank line before each heading
        if kind == "key":
            _put(ws, f"A{row}", text, "input", fill=FILL_KEY)
        else:
            _put(ws, f"A{row}", text, kind, align=WRAP if kind == "label" else None)
            if kind == "label" and len(text) > 120:
                ws.row_dimensions[row].height = 14.0 * (len(text) // 120 + 1)
        row += 1
    ws.column_dimensions["A"].width = 125
    ws.sheet_view.showGridLines = False


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_workbook(bundle: dict[str, Any], path: str | Path, *, commentary: str | None = None,
                   source: str = "the configuration file") -> Path:  # fmt: skip
    """Write the Excel model for a pipeline bundle and return its path.

    The workbook contains formulas without cached values; open it in Excel (or
    run ``pefund.reporting.excel_verify.recalculate``) to compute them.
    """
    result: FundResult = bundle["result"]
    cfg: FundConfig = result.config
    american: FundResult = bundle["comparison"]["runs"]["american"]
    n = result.n_quarters
    unit = f"{cfg.fund.currency} {cfg.fund.unit}"

    wb = Workbook()
    wb._named_styles["Normal"].font = Font(name=FONT, size=10)  # workbook-wide default font
    _readme_sheet(wb, bundle, source)
    _inputs_sheet(wb, cfg)
    deals = _deals_sheet(wb, cfg, result)
    investors = _investors_sheet(wb, cfg)
    last = _cashflows_sheet(wb, result)
    _waterfall_sheet(wb, S_EU, n, american=False, unit=unit)
    dbd_refs = _dbd_inputs_sheet(wb, american)
    _waterfall_sheet(wb, S_DBD, n, american=True, unit=unit, dbd_refs=dbd_refs)
    _flows_sheet(wb, n, unit)
    refs = _returns_sheet(wb, bundle, last)
    annual = _annual_sheet(wb, result, last)
    _lp_accounts_sheet(wb, bundle, investors, refs)
    _bridge_sheet(wb, bundle)
    scenarios = _scenarios_sheet(wb, bundle)
    _sensitivity_sheet(wb, bundle, unit)
    _monte_carlo_sheet(wb, bundle)
    _commentary_sheet(wb, commentary)
    _checks_sheet(wb, bundle, last, refs)
    _dashboard_sheet(wb, bundle, refs, last, annual, deals, scenarios)

    order = [S_README, S_DASH, S_INPUTS, S_DEALS, S_INVESTORS, S_CASH, S_EU, S_DBD, S_DBD_IN, S_FLOWS, S_RETURNS,
             S_ANNUAL, S_LPS, S_BRIDGE, S_SCEN, S_SENS, S_MC, S_AI, S_CHECKS]  # fmt: skip
    wb._sheets = [wb[name] for name in order if name in wb.sheetnames]
    wb.active = 0

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path
