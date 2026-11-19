"""Read a fund configuration back out of the Excel model.

The ``Inputs``, ``Deals`` and ``Investors`` sheets written by
:mod:`pefund.reporting.excel_model` are a complete input file.  This module
turns them back into the nested dictionary that :class:`~pefund.config.FundConfig`
validates, so an analyst can edit assumptions in Excel and re-run the engine::

    pefund run --config excel/PE_Fund_Waterfall_Model.xlsx
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..config import ConfigError

INPUTS_SHEET, DEALS_SHEET, INVESTORS_SHEET = "Inputs", "Deals", "Investors"
_INTEGER_KEYS = {
    "subscription_line.quarters_outstanding",
    "valuation.hold_at_cost_quarters",
    "shocks.hold_shift_quarters",
}


def _clean(value: Any) -> Any:
    """Normalise a cell value: blanks to ``None``, datetimes to dates, whole floats kept as floats."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None
        if text.upper() in ("TRUE", "FALSE"):
            return text.upper() == "TRUE"
        return text
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def read_config_workbook(path: str | Path) -> dict[str, Any]:
    """Return the configuration dictionary held in an Excel model."""
    wb = load_workbook(Path(path), data_only=True)
    for sheet in (INPUTS_SHEET, DEALS_SHEET):
        if sheet not in wb.sheetnames:
            raise ConfigError(f"Workbook {path} has no '{sheet}' sheet; it was not built by `pefund excel`")

    config: dict[str, Any] = {}
    # --- Inputs: column B holds the value, column F the dotted configuration key.
    for row in wb[INPUTS_SHEET].iter_rows(min_row=1, values_only=True):
        key = row[5] if len(row) > 5 else None
        if not isinstance(key, str) or "." not in key or " " in key.strip():
            continue
        value = _clean(row[1])
        key = key.strip()
        if key in _INTEGER_KEYS and value is not None:
            value = int(round(float(value)))
        section, field = key.split(".", 1)
        if value is None and key != "fund.valuation_date":
            raise ConfigError(f"Inputs sheet: '{key}' is blank")
        config.setdefault(section, {})[field] = value

    # --- Deals: one row per deal, located by the header row that starts with "Deal".
    rows = list(wb[DEALS_SHEET].iter_rows(values_only=True))
    header_index = next((i for i, row in enumerate(rows) if row and row[0] == "Deal"), None)
    if header_index is None:
        raise ConfigError("Deals sheet: could not find the header row starting with 'Deal'")
    headers = [str(h).strip() if h is not None else "" for h in rows[header_index]]

    def column(name: str) -> int:
        try:
            return headers.index(name)
        except ValueError:
            raise ConfigError(f"Deals sheet: missing column '{name}'") from None

    deals = []
    for row in rows[header_index + 1 :]:
        name = _clean(row[0]) if row else None
        if name is None or name == "Total":
            if deals:
                break  # the first blank row after the table ends it
            continue

        def cell(label: str, row: tuple = row) -> Any:
            return _clean(row[column(label)])

        deal: dict[str, Any] = {
            "name": name,
            "sector": cell("Sector") or "Unclassified",
            "strategy": cell("Strategy") or "buyout",
            "exit_date": cell("Exit date"),
        }
        if cell("Gross MOIC (direct mode)") is not None:
            deal["gross_moic"] = float(cell("Gross MOIC (direct mode)"))
        operating = {
            "entry_ebitda": cell("Entry EBITDA"),
            "entry_ev_multiple": cell("Entry EV / EBITDA"),
            "entry_leverage": cell("Entry leverage (x EBITDA)"),
            "ebitda_growth": cell("EBITDA growth (CAGR)"),
            "exit_ev_multiple": cell("Exit EV / EBITDA"),
            "cash_conversion": cell("Cash conversion"),
        }
        if any(v is not None for v in operating.values()):
            missing = [k for k, v in operating.items() if v is None]
            if missing:
                raise ConfigError(f"Deals sheet: deal '{name}' has an incomplete operating case; blank: {missing}")
            deal["operating"] = operating
        deal["investments"] = [
            {"date": cell(f"Investment {k} date"), "amount": float(cell(f"Investment {k} amount"))}
            for k in (1, 2, 3)
            if cell(f"Investment {k} date") is not None and cell(f"Investment {k} amount") is not None
        ]
        events = []
        for k in (1, 2):
            if cell(f"Event {k} date") is None:
                continue
            event = {
                "date": cell(f"Event {k} date"),
                "kind": cell(f"Event {k} kind"),
                "share": cell(f"Event {k} share of proceeds"),
            }
            if cell(f"Event {k} stake sold") is not None:
                event["stake_sold"] = cell(f"Event {k} stake sold")
            events.append(event)
        if events:
            deal["proceeds_events"] = events
        deals.append(deal)
    config["deals"] = deals

    # --- Investors: optional register.
    if INVESTORS_SHEET in wb.sheetnames:
        investors = []
        started = False
        for row in wb[INVESTORS_SHEET].iter_rows(values_only=True):
            if row and row[0] == "Limited partner":
                started = True
                continue
            if not started:
                continue
            name, commitment = _clean(row[0]), _clean(row[1])
            if name is None or name == "Total":
                break
            investors.append({"name": name, "commitment": float(commitment)})
        if investors:
            config["investors"] = investors
    return config
