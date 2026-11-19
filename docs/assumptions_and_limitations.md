# Assumptions and limitations

A model is only useful if its limits are stated. This page lists what the model assumes, what it
simplifies and what it leaves out, so that nobody relies on it for something it does not do.

## The fund is hypothetical

"Sample Mid-Market Buyout Fund I", its seven investors and its twelve portfolio companies are
invented. The terms are typical of a buyout fund (2% fee stepping down, 8% preferred return, full
catch-up, 20% carry) but they are not taken from any real limited partnership agreement, and the
deal outcomes are assumptions, not forecasts. Nothing here is investment, legal or tax advice.

## A real waterfall is defined by its LPA

The engine implements the common forms of each term and makes them configurable. A real fund's
limited partnership agreement can differ in ways that change the numbers. Before using the model on
a real fund, check at least these against the document:

| Term | What the model does | What an LPA may say instead |
| --- | --- | --- |
| Preferred return day count | Actual / 365 from each quarter-end | 30 / 360, or accrual from the actual drawdown date |
| Preferred return compounding | Annual, quarterly or simple | Other frequencies; compounding only on capital for some classes |
| Deal-by-deal "realised capital" | Cost relieved, optionally plus write-downs, plus a share of fees and expenses | Different allocation of fees and expenses; permanent write-downs only; write-ups not offsetting write-downs |
| Clawback | Tested once at liquidation; greater of excess carry and LP shortfall; optional after-tax cap | Interim clawback tests; different tax assumptions; several or joint guarantees by the carry recipients |
| Escrow | A fixed share of each carry payment, released at liquidation | Release tests during the life of the fund; a minimum balance |
| Catch-up | One rate applied until the GP reaches its carry share | Tiered carry rates that step up with performance |
| Management fee | One rate and basis in the investment period, another after | Fee holidays, breakpoints by commitment size, write-off adjustments to the invested-capital basis |
| Recycling | Cost returned in the investment period, capped | Time limits on recycling; recycling of fees; different caps |

## Simplifications

- **Quarterly time step.** Every cash flow falls on a quarter-end. Calls and distributions within a
  quarter are netted in time, which moves IRR slightly compared with daily flows.
- **Just-in-time funding.** The fund holds no cash reserve and calls exactly what it needs. Fees are
  charged quarterly in advance.
- **One LP class.** All limited partners share one set of terms and are allocated pro rata. There
  are no side letters, fee discounts for large investors, excused investors, defaulting investors
  or later closings with equalisation interest.
- **Pro-rata GP commitment.** The GP's own commitment invests alongside the LPs in every deal, pays
  its share of expenses, and bears no fee or carry. A fee-waiver or cashless contribution is not
  modelled.
- **Subscription line.** A fixed number of quarters outstanding, simple interest, used only in the
  investment period. No borrowing base, covenants, undrawn fees or NAV facility.
- **Deals.** A deal is described either by a multiple of money or by five operating drivers
  (entry EBITDA, entry multiple, leverage, growth, exit multiple) with a constant cash conversion.
  There is no debt schedule, management equity, transaction cost, earn-out, currency effect or tax
  at the portfolio-company level. Equity value is floored at zero.
- **Fair value** is a smooth path: cost for a set number of quarters, then a straight line to the
  value still to be realised. It is not a mark-to-market valuation and does not produce realistic
  quarterly volatility. Interim NAV, RVPI and accrued carry inherit this.
- **Taxes.** The only tax in the model is the assumed rate used to cap the clawback. Withholding,
  tax distributions and the investors' own tax positions are outside it.
- **Single currency.**
- **Gross-to-net bridge.** The attribution is sequential (fees, then expenses, then carry). A
  different order would give slightly different step sizes; the total is unaffected.
- **Public-market equivalent.** The benchmark is a constant annual return, not an index series.

## Monte Carlo

The simulation is an illustration of dispersion, not a risk model. The market and deal-specific
volatilities, the write-off probability and the link between weak markets and delayed exits are
assumptions chosen to be plausible. They are not calibrated to data. Outcomes are lognormal around
each deal's base case, and deals are correlated only through the single market factor. The
simulation is seeded; exact figures can differ slightly between NumPy versions.

## Gen AI layer

- The commentary is a draft for an analyst to review. The grounding check removes invented and
  mislabelled figures; it does not validate reasoning or confirm that a figure is attached to the
  right deal or scenario. See [ai_layer.md](ai_layer.md#what-the-check-does-not-prove).
- The language-model path has been tested only against stand-in clients, not with a live API call.
  The committed sample commentary comes from the deterministic writer.

## Excel model

- The reconciliation between Excel formulas and Python runs in LibreOffice Calc. The workbook has
  not been opened in Microsoft Excel as part of the automated checks.
- Nine waterfall terms recalculate live in Excel. Everything else (deals, fees, fund size, shocks)
  is recomputed by the engine and requires a re-run.
- The `Deals` sheet has room for three cheques and two interim proceeds events per deal. A YAML
  configuration has no such limit.

## What the tests do and do not establish

The tests show that the engine does what this documentation says: the tiers follow the stated
formulas, two independent implementations agree with it, cash is conserved, and the Excel formulas
reproduce it. They do not show that those rules match any particular fund's agreement. That is a
matter of reading the agreement, which is the first item on this page.
