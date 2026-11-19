# The Excel model

[`excel/PE_Fund_Waterfall_Model.xlsx`](../excel/PE_Fund_Waterfall_Model.xlsx) is built by
`pefund excel` (or `pefund all`). It is a working model, not a data export: both distribution
waterfalls, the escrow, the clawback test, the returns and the dashboard are **live Excel
formulas**, about 4,100 of them in the base case.

## Why it is built this way

A fund model usually exists twice: a spreadsheet the deal team trusts and code that can run ten
thousand scenarios. The two drift apart. Here the same engine writes the workbook, the workbook
recomputes the waterfall independently in formulas, and a `Checks` sheet compares the two line by
line. If they ever disagree, the workbook says so on its front page.

| Part of the model | Where it is calculated | Why |
| --- | --- | --- |
| Deal cash flows, fees, expenses, capital calls | Python (written as values) | Path-dependent rules (offset carry-forward, subscription line, recycling) are clearer and better tested in code |
| Both waterfalls, escrow, clawback | **Excel formulas** | This is the part a reader wants to audit cell by cell |
| IRR, DPI, RVPI, TVPI, LP / GP split, annual roll-up | **Excel formulas** | They follow from the waterfall and must respond to the levers |
| Scenarios, sensitivities, Monte Carlo | Python (written as values) | Each cell is a full re-run of the fund |

## Colour code

| Style | Meaning |
| --- | --- |
| Blue text | An input you may change |
| Yellow fill | A **live lever**: an input that recalculates the workbook immediately |
| Black text | A formula |
| Green text | A link to another sheet |
| Grey text | A value written by the Python engine; refresh it by re-running `pefund excel` |

Zero is shown as a dash and negatives in brackets. Money is in the unit stated on `Inputs`.

## Sheets

| Sheet | What it holds |
| --- | --- |
| `README` | How to use the workbook |
| `Dashboard` | Headline KPIs, the waterfall in use, the status of `Checks`, and four charts |
| `Inputs` | Every fund-level assumption, with allowed values, what it does and its configuration key |
| `Deals` | Deal inputs (up to three cheques and two interim proceeds events each) beside the engine's outcome per deal |
| `Investors` | The LP register |
| `CashFlows` | Quarterly output of the engine: investments, proceeds, fees and offsets, expenses, capital called, cash available to the LP class |
| `WF_WholeFund` | European waterfall as a running ledger, one row per quarter |
| `WF_DealByDeal` | American waterfall with loss carryforward |
| `DbD_Inputs` | LP capital by purpose (each deal, fees, expenses) and the eligibility weights the deal-by-deal waterfall uses |
| `LP_GP_Flows` | Cash flows of LPs, GP carry and GP commitment for whichever waterfall is selected |
| `Returns` | Net and gross IRR, multiples, and the split of gross profit between LPs, GP and costs |
| `Annual` | Calendar-year roll-up |
| `LP_Accounts` | Each investor's pro-rata capital account: at liquidation (live formulas) and at the valuation date (engine values) |
| `Bridge` | Gross-to-net bridge and value creation by deal |
| `Scenarios`, `Sensitivity`, `MonteCarlo` | Results of the scenario engine |
| `AI_Commentary` | The generated commentary, with its grounding score |
| `Checks` | Excel formulas against Python, line by line |

## Live levers

Nine inputs recalculate the workbook without Python. They are the yellow cells on `Inputs`, and each
is a named range used by the formulas:

| Lever | Named range | Effect |
| --- | --- | --- |
| Waterfall type | `wf_type` | Selects which waterfall sheet feeds `LP_GP_Flows`, `Returns`, `Annual` and the dashboard |
| Preferred return | `pref_rate` | Hurdle rate in both waterfalls |
| Compounding | `pref_comp` | `annual`, `quarterly` or `simple` |
| GP catch-up rate | `catch_up` | 100% full, 0% none |
| Carried interest rate | `carry_rate` | GP share of profit |
| Clawback enabled | `cb_enabled` | Switches the liquidation test on or off |
| After-tax limit | `cb_after_tax` | Caps the clawback at carry net of assumed tax |
| Assumed tax rate | `cb_tax_rate` | Used by that cap |
| Carry escrow | `escrow_pct` | Share of each carry payment held back |

After a lever is changed, `Checks` reports **"Levers changed in Excel: Python values are from the
original run"**. That is expected: Excel has recalculated and the grey Python values have not.
Re-running `pefund excel` with the workbook as its configuration brings the two back together.

Everything that is not a lever (deals, fund size, fees, shocks, the subscription line) changes the
cash flows themselves, so it goes through the engine. See *Round trip* below.

## How the waterfall formulas work

**`WF_WholeFund` is the textbook running ledger.** Each row carries the unreturned capital and the
unpaid preferred return forward, accrues one period of preferred return, adds the capital called,
and pays the four tiers:

```
Pref rate for the period = (1 + pref_rate / comp_m) ^ (comp_m × days / 365) − 1      (compounding)
                         = pref_rate × days / 365                                    (simple)
Tier 1 = MIN(cash, capital to return)
Tier 2 = MIN(cash − tier 1, pref to pay)
Catch-up still needed = MAX(0, (carry_rate × profit distributed to date − carry to date)
                               / (catch_up − carry_rate))        when catch_up > carry_rate, else 0
Tier 3 = MIN(cash − tier 1 − tier 2, catch-up still needed)      GP share = catch_up × tier 3
Tier 4 = what is left                                            GP share = carry_rate × tier 4
```

**`WF_DealByDeal` cannot use a running ledger**, because the capital that counts changes as deals
are realised. It rebuilds the hurdle from scratch each quarter with `SUMPRODUCT`: eligibility
weight × capital contributed for that purpose × a mask for contributions made so far × the growth
factor from the contribution date. Earlier tier-1 and tier-2 payments are grown forward and
subtracted in the same way. This is the formula in
[methodology.md](methodology.md#6-the-distribution-waterfall), cell for cell. The weights on
`DbD_Inputs` are engine values and reflect the `expense_allocation` and `writedowns_as_realized`
settings of the run.

**Clawback block.** Under each waterfall, a small block repeats the two-pronged test at
liquidation: fund profit, excess carry, LP shortfall against capital plus whole-fund preferred
return, the after-tax cap, the amount met from escrow, and the GP's carry net of clawback. The
whole-fund preferred return comes from three "test ledger" columns on the same sheet.

**Returns.** IRR uses `XIRR` on the quarter-end dates. Each range starts at the first non-zero cash
flow, because Excel's `XIRR` returns an error when the first value is zero.

## The Checks sheet

`Checks` compares 35 totals: every tier under both waterfalls, carry, clawback before and after the
cap, LP distributions, paid-in capital, TVPI, net and gross IRR, gross MOIC, fees, GP economics,
the fee recomputed by formula, and capital by purpose. Each line shows the Excel formula, the Python
value, the difference and a status, with a tolerance of 0.000001. The overall result is a named range
(`checks_status`) shown on the dashboard.

To run the same comparison without opening Excel:

```bash
pefund verify-excel excel/PE_Fund_Waterfall_Model.xlsx
```

This recalculates a copy of the workbook with a headless LibreOffice and reads `Checks`. The
original file is never modified. The test suite does this for the base case and then for six
combinations of lever settings that between them change all nine levers, checking both waterfall
sheets each time. Those runs start from a loss-making scenario, so the clawback, the after-tax cap
and the escrow are exercised as well ([`tests/test_excel.py`](../tests/test_excel.py)).

## Round trip: the workbook as an input file

The workbook is also a configuration file. Edit the blue cells on `Inputs`, `Deals` or `Investors`,
save, and run:

```bash
pefund excel --config excel/PE_Fund_Waterfall_Model.xlsx --out excel/my_case.xlsx
pefund all   --config excel/my_case.xlsx --out outputs/my_case
```

Inputs are validated exactly as a YAML configuration is. For example, changing the fund size
without changing the investor register is rejected, because commitments would no longer add up.
A YAML file can hold any number of cheques and interim events per deal; the `Deals` sheet has room
for three and two.

## Things to know

- **Cached values.** `openpyxl` writes formulas without results. Excel calculates them on opening.
  The copy in this repository has been recalculated with LibreOffice so that viewers that do not
  calculate (file previews, pandas) show numbers.
- **Tested in LibreOffice, not in Excel.** The reconciliation runs in LibreOffice Calc. The workbook
  uses eleven long-standing functions (`IF`, `MIN`, `MAX`, `SUM`, `SUMPRODUCT`, `SUMIFS`, `COUNTIF`,
  `XIRR`, `IFERROR`, `ABS`, `YEAR`), with no dynamic arrays, macros or external links, so Excel
  should calculate it identically, but it has not been opened in Excel as part of the automated
  checks.
- **Charts** are native Excel charts built from the formula cells, so the first two respond to the
  levers.
