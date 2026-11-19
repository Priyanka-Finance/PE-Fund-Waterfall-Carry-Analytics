# Methodology

How the model works, in the order the cash moves. Every rule below is implemented in
`src/pefund/` and exercised by the tests; file names are given so each statement can be checked
against the code.

- [1. Conventions](#1-conventions)
- [2. What happens in one quarter](#2-what-happens-in-one-quarter)
- [3. Commitments and capital calls](#3-commitments-and-capital-calls)
- [4. Management fee and expenses](#4-management-fee-and-expenses)
- [5. Portfolio deals](#5-portfolio-deals)
- [6. The distribution waterfall](#6-the-distribution-waterfall)
- [7. Whole-of-fund and deal-by-deal](#7-whole-of-fund-and-deal-by-deal)
- [8. Escrow and clawback](#8-escrow-and-clawback)
- [9. Interim valuation and accrued carry](#9-interim-valuation-and-accrued-carry)
- [10. Performance measures](#10-performance-measures)
- [11. Gross-to-net bridge and LP / GP economics](#11-gross-to-net-bridge-and-lp--gp-economics)
- [12. Scenario and sensitivity engine](#12-scenario-and-sensitivity-engine)
- [13. Monte Carlo simulation](#13-monte-carlo-simulation)
- [14. How the model is verified](#14-how-the-model-is-verified)

## 1. Conventions

| Item | Convention |
| --- | --- |
| Time step | Calendar quarters. Every date snaps to the quarter-end on or after it; quarter 0 is the first close. |
| Day count | Actual / 365 from the first close, for the preferred return and for IRR. A four-year hold that crosses a leap day is 1,461 / 365 = 4.0027 years. |
| Money | One currency and one unit (the base case uses USD millions). Rates are decimals: 0.08 is 8%. |
| Parties | **LPs** (the fee-paying limited partners), the **GP commitment** (the manager's own money, invested alongside, fee-free and carry-free), the **GP carry** (carried interest) and the **manager** (management fees). |
| Net | The LPs' experience: capital called from them against cash distributed to them, after fees, expenses and carry. |
| Gross | Deal level: equity invested against proceeds, before any fund-level cost. |
| Liquidation | The last quarter of the model, when the final deal exits. The clawback is tested and the escrow released there. |

## 2. What happens in one quarter

`run_fund` in [`fund.py`](../src/pefund/fund.py) walks the fund quarter by quarter. Within a quarter
the order is fixed, because it matters:

1. **Recycling** – cost returned during the investment period is added back to unfunded commitments,
   up to the cap.
2. **Management fee** for the coming quarter, net of any portfolio-fee offset.
3. **Partnership expenses.**
4. **Funding** – investments, expenses and fees are funded by a capital call, by the subscription
   line, or (once commitments are exhausted) out of proceeds.
5. **Distribution waterfall** on the LPs' share of the quarter's proceeds.
6. **Escrow** – the agreed share of any carry is held back.
7. **Whole-of-fund test ledger** is updated (used later by the clawback).
8. **Liquidation only** – clawback test, escrow release.
9. **Interim valuation** – fair value, accrued carry, clawback exposure and LP net asset value.

## 3. Commitments and capital calls

- **Commitments.** `committed_capital` is split between the GP commitment (`gp_commitment_pct`) and
  the LPs. Every deal and every expense is shared pro rata between the two. Only LPs pay the
  management fee and only LP profits bear carry.
- **Capital calls are just-in-time.** Each quarter the fund calls exactly what it needs, in this
  order: investments, then expenses, then fees. `lp_unfunded` tracks what is left.
- **Over-commitment is an error, not a silent fix.** If deals need more LP capital than is unfunded,
  the engine stops with a `ConfigError` naming the quarter. A model that quietly invents capital would
  overstate returns.
- **Fees after commitments run out.** If the unfunded commitment cannot cover fees or expenses, they
  are netted from that quarter's proceeds before the waterfall (`netted_from_proceeds`). If there are
  no proceeds either, they are carried as a payable to the next quarter. Either case is reported in
  `result.flags`.
- **Recycling** (`recycling.enabled`). Cost relieved by realisations during the investment period
  becomes callable again, capped at `cap_pct_of_commitments`. Paid-in capital can then exceed
  commitments, which is why DPI and TVPI are measured on paid-in capital, never on commitments.
- **Subscription line** (`subscription_line.enabled`). During the investment period a quarter's
  needs are drawn on the line and called from investors `quarters_outstanding` quarters later,
  together with simple interest at `interest_rate`. The commitment is reserved at the draw, so the
  line can never be used to over-commit. The line is ignored where the repayment would fall after
  liquidation. Interest is a partnership expense. The effect is the well-known one: IRR rises because
  capital is called later, while TVPI falls by the interest cost.

## 4. Management fee and expenses

- **Rate and basis by period.** During the investment period the fee is
  `rate_investment_period × basis_investment_period`; afterwards
  `rate_post_investment_period × basis_post_investment_period`. This covers the usual step-down in
  both rate and basis. Three bases are supported:

  | Basis | Amount |
  | --- | --- |
  | `committed` | LP commitments |
  | `net_invested` | LP share of the cost of unrealised investments |
  | `nav` | LP share of fair value |

- **Timing.** One quarter of fee is charged at each quarter-end for the coming quarter, so no fee is
  charged in the liquidation quarter.
- **Fee offsets.** The manager may charge portfolio companies a transaction fee (a percentage of
  equity invested) and a monitoring fee (a percentage per annum of cost still invested).
  `offset_pct` of those fees is credited against the management fee. A credit larger than the
  quarter's fee is carried forward, never paid out.
- **Partnership expenses.** `organizational` is charged once at first close; `annual_operating` is
  spread evenly over the quarters. The GP commitment bears its pro-rata share of expenses.

## 5. Portfolio deals

[`deals.py`](../src/pefund/deals.py) turns each deal into four arrays (deals × quarters): equity
invested, gross proceeds, cost relieved and fair value.

**Two ways to state an outcome**

- *Direct*: total proceeds = `gross_moic` × cost.
- *Operating* (an LBO-style driver model):

  ```
  exit EBITDA      = entry EBITDA × (1 + growth) ^ hold years
  exit net debt    = entry EBITDA × entry leverage − cash conversion × cumulative EBITDA
  exit equity      = max(exit EBITDA × exit EV/EBITDA multiple − exit net debt, 0)
  fund's proceeds  = ownership × exit equity,   ownership = equity cheque ÷ entry equity
  ```

  The operating mode is what makes the sensitivities meaningful: a later exit compounds EBITDA and
  pays down more debt, so a longer hold raises the multiple of money while it lowers the IRR. It also
  yields a **value-creation bridge** per deal: EBITDA growth, multiple expansion, the combined
  effect, and deleveraging, which together equal proceeds minus cost.

**Cash events during the hold**

| Event | Proceeds | Cost relieved |
| --- | --- | --- |
| Follow-on investment | – | adds to cost |
| `dividend_recap` | `share` × total proceeds | cost-recovery: the lower of the amount and the remaining cost |
| `partial_exit` | `share` × total proceeds | `stake_sold` × remaining cost |
| Final exit | whatever is left | all remaining cost |

**Fair value.** A holding is carried at cost for `hold_at_cost_quarters`, then moves in a straight
line to the value still to be realised. A deal heading for a loss is therefore written down
gradually before it exits, which matters for the deal-by-deal waterfall (section 7).

**Shocks.** `shocks.exit_multiple_pct` scales every exit valuation, `ebitda_growth_shift` adds to
every operating deal's growth, and `hold_shift_quarters` moves every exit (interim events move with
it; an exit can never fall before the last cheque or after the extended fund term).
`shocks.deal_overrides` applies the same shocks, a fixed `gross_moic` or a `write_off` to one deal.

## 6. The distribution waterfall

Cash available to the LP class runs through four tiers (`split_tiers` in
[`fund.py`](../src/pefund/fund.py)):

| Tier | Who receives | Until |
| --- | --- | --- |
| 1. Return of capital | 100% LPs | eligible capital is returned |
| 2. Preferred return | 100% LPs | the hurdle on that capital is paid |
| 3. GP catch-up | `catch_up_rate` to the GP, the rest to LPs | GP carry = `carry_rate` × all profit distributed |
| 4. Residual split | `carry_rate` to the GP, the rest to LPs | – |

**How much is owed in tiers 1 and 2.** For eligible contributions `c_j` paid at time `t_j`, and
tier-1 and tier-2 payments `p_k` already made at `t_k`, the amount that clears both tiers at time
`t` is

```
hurdle(t) = Σ c_j × (1 + r)^(t − t_j)  −  Σ p_k × (1 + r)^(t − t_k)
```

Capital outstanding is contributions less tier-1 payments; the preferred return outstanding is the
hurdle less capital outstanding. This is the same arithmetic as the familiar running ledger
(opening balance, plus accrual, less payments). It is written in this form because it still works
when the set of eligible contributions changes over time, which is exactly what deal-by-deal needs.

**Preferred return compounding**

| `pref_compounding` | Growth of 1 over τ years |
| --- | --- |
| `annual` | `(1 + r)^τ` |
| `quarterly` | `(1 + r/4)^(4τ)` |
| `simple` | `1 + r × τ` on unreturned capital only; unpaid pref does not itself earn pref |

**Catch-up.** With profit distributed so far `P` (tiers 2 to 4, to LPs and GP together) and carry
paid so far `C`, the catch-up tier still needs

```
needed = (carry_rate × P − C) ÷ (catch_up_rate − carry_rate)
```

- `catch_up_rate = 1.0` is a full catch-up: the GP takes every dollar until it holds `carry_rate`
  of all profit.
- A rate between the carry rate and 1 (say 0.5) is a partial catch-up; the same formula applies.
- `catch_up_rate = 0` is a hard hurdle: no tier 3, and the GP earns carry only on profit above the
  hurdle.
- A rate between 0 and the carry rate is rejected at validation, because the GP could never catch up.

The test is cumulative, so the catch-up **reopens** when it should. If new capital is called after
carry has been paid, more preferred return accrues and the next distribution passes through tiers 2
and 3 again before reaching the split.

## 7. Whole-of-fund and deal-by-deal

Both waterfalls use the algorithm in section 6. The only difference is **which capital is eligible**
at a distribution date, expressed as a weight between 0 and 1 on each stream of contributions:

| Stream of LP capital | Whole-of-fund (`european`) | Deal-by-deal (`american`) |
| --- | --- | --- |
| Capital in deal *d* | 1 | cost relieved to date ÷ cost invested to date |
| … plus, if `writedowns_as_realized` | – | unrealised write-down of *d* ÷ cost invested |
| Fees and expenses, `pro_rata` | 1 | realised cost of all deals ÷ cost invested in all deals |
| Fees and expenses, `all_to_date` | 1 | 1 |

What follows from that:

- **Whole-of-fund** returns *all* contributed capital and the preferred return on it before any
  carry. Carry arrives late and is rarely overpaid.
- **Deal-by-deal** needs back only the capital in realised deals, with their share of fees and
  expenses, so carry starts at the first profitable exit.
- **Loss carryforward.** A deal realised at a loss keeps a weight of 1 while its proceeds fell short
  of its cost. The shortfall stays in the hurdle and must be made up from later exits before more
  carry is paid.
- **Write-downs as realised losses.** With `writedowns_as_realized`, the written-down part of a
  holding counts as realised, so a visibly impaired deal holds back carry before it is sold.
- **Total carry is the same in most cases** because both waterfalls end on the same cumulative test;
  what changes is *when* it is paid. Carry paid early that later proves too much is recovered by the
  clawback, subject to its cap. With a partial catch-up the two totals can differ slightly, because
  the preferred return each tier 2 pays depends on the path.

`compare_waterfalls` in [`analytics.py`](../src/pefund/analytics.py) runs the same deals through
both and reports the carry timing, its present value and the peak clawback exposure.

## 8. Escrow and clawback

**Escrow.** `clawback.escrow_pct` of every carry distribution is held back and released at
liquidation, less any clawback. An escrow reduces what the GP receives early without changing what
it earns.

**Clawback** (`clawback_due`). At liquidation the GP returns the greater of two amounts:

- **(A) Excess carry:** carry received − `carry_rate` × max(fund profit, 0), where profit is LP
  distributions plus carry less LP contributions.
- **(B) LP shortfall:** contributed capital plus the preferred return on it, less LP distributions.

subject to two limits:

- never more than the carry actually received;
- if `after_tax_limit` is set, never more than carry net of `assumed_tax_rate`, the usual
  concession that the GP cannot return money it has paid in tax.

The preferred return in test (B) is measured on the **whole fund**, even under deal-by-deal. The
engine therefore keeps a second, whole-of-fund ledger alongside the live waterfall
(`whole_fund_pref_accrued`). The clawback is paid from the escrow first. The amount the after-tax cap
prevents the LPs from recovering is reported as `clawback_shortfall`.

## 9. Interim valuation and accrued carry

At every quarter-end the engine asks what would happen if the remaining portfolio were sold at
fair value that day:

1. the LP share of fair value, less any unpaid fees, is run through the waterfall with **all**
   capital eligible (a liquidation realises everything), after settling any subscription-line
   balance;
2. the carry that hypothetical distribution would pay is the **accrued (unrealised) carry**;
3. the clawback the GP would owe in that liquidation is the **clawback exposure**;
4. **LP net asset value** = the LPs' hypothetical distribution + clawback receivable − line balance.

This is what makes RVPI, TVPI and the since-inception IRR *net of accrued carry*, the basis on which
funds report to their investors. Under deal-by-deal the clawback exposure shows, quarter by quarter,
how much paid carry is at risk.

## 10. Performance measures

| Measure | Definition | Code |
| --- | --- | --- |
| Gross IRR | IRR of deal cash flows (equity in, proceeds out, fair value as terminal value) | `metrics.xirr` |
| Gross MOIC | (proceeds + fair value) ÷ equity invested | `metrics.multiples` |
| Net IRR | IRR of LP cash flows (contributions, distributions, LP NAV as terminal value) | `FundResult.metrics` |
| DPI | cumulative LP distributions ÷ paid-in capital | |
| RVPI | LP net asset value ÷ paid-in capital | |
| TVPI | DPI + RVPI | |
| Gross-to-net spread | gross IRR − net IRR | `analytics.gross_to_net_bridge` |
| KS-PME | Kaplan–Schoar public-market equivalent against a constant-return benchmark; above 1 beats it | `metrics.ks_pme` |
| J-curve | cumulative LP net cash flow by quarter | `quarterly` table |

IRR is solved by bisection on actual / 365 year fractions and returns `nan` when cash flows never
change sign, so a deal that is written off entirely shows a multiple of 0.00x and no IRR. All measures are available **as of any quarter** (`result.metrics(as_of)`), which is
how the valuation-date snapshot and the TVPI build-up are produced. Deal-level IRR, MOIC, holding
period and value creation are in `result.deals`; sector roll-ups, concentration and per-investor
capital accounts are in `analytics.py`.

## 11. Gross-to-net bridge and LP / GP economics

**Bridge.** The gap between gross and net IRR is attributed by switching on one fund-level cost at
a time and re-measuring the IRR on the resulting cash flows:

```
gross (deal level) → after management fees → after expenses → after carry → net
```

A final "financing and timing" step captures anything that changes *when* LPs pay rather than *what*
they pay: the subscription line, and fees settled out of proceeds. It is zero in the base case. The
attribution is sequential, so the size of each step depends on the order; the order used is the
order in which the costs are borne.

**Who gets the profit.** `value_split` divides the fund's gross profit between LP net profit,
carried interest, management fees, the profit on the GP commitment and partnership expenses.
`lp_gp_economics` gives contributions, distributions, profit, multiple and IRR for each party.

## 12. Scenario and sensitivity engine

Everything in [`scenarios.py`](../src/pefund/scenarios.py) re-runs the full fund model. No result is
interpolated or approximated.

| Tool | What it does |
| --- | --- |
| **Named scenarios** | A scenario is a set of overrides on the configuration (`configs/scenarios.yaml`): market shocks, changes of terms, single-deal outcomes. Each is reported with its change from the base case. |
| **Two-way tables** | Any two parameters against any metric. Five default pairs are produced, for example exit multiple × holding period, and carry rate × preferred return. |
| **Tornado** | Each parameter moved to a low and a high value on its own, ranked by the swing in net IRR. |
| **Breakeven solver** | Bisection for the change in exit valuations at which LPs just get their capital back, just earn the hurdle (where carry starts), or reach a target net IRR. |

Parameters available to all four: exit multiple, EBITDA growth, holding period, carry rate,
preferred return, catch-up rate, management fee rate (the post-period step-down is kept), GP
commitment and carry escrow. Adding one is a single entry in `PARAMETERS`.

## 13. Monte Carlo simulation

[`montecarlo.py`](../src/pefund/montecarlo.py) simulates deal outcomes around the base case. In each
trial:

- one **market factor** (lognormal, 15% volatility by default) scales every exit valuation, and a
  weak market also delays exits;
- each deal has its own **idiosyncratic factor** (lognormal, 25%), a small random change in holding
  period, and a 5% chance of being **written off**;
- the same simulated deals are run through **both** waterfalls.

The output is the distribution of net IRR, TVPI and carry; the probability that LPs fall short of
the hurdle or lose capital; and how often deal-by-deal ends in a clawback, how large it is, and how
much the after-tax cap leaves unrecovered. A clawback is counted as material above 0.1% of
commitments, which separates real over-distribution from small true-ups (for example a late fee
called after carry was paid). The simulation is seeded and reproducible. The volatilities are
illustrative assumptions, not calibrated estimates.

## 14. How the model is verified

| Check | Where |
| --- | --- |
| Closed-form single-deal cases worked by hand (all four tiers, each compounding rule, hard hurdle, partial catch-up) | `tests/test_waterfall.py`, `tests/test_worked_examples.py` |
| An **independent** textbook running-ledger waterfall, written separately in the tests, agrees with the engine on the whole-of-fund base case | `tests/test_engine.py` |
| **Independent** per-deal ledgers agree with the engine's deal-by-deal waterfall on random portfolios | `tests/test_engine.py` |
| Conservation: every dollar of proceeds is distributed exactly once, tiers sum to distributions, contributions never exceed commitments plus recycling, across 14 combinations of features | `tests/test_engine.py` |
| The **Excel model recomputes both waterfalls with formulas**; after recalculation in LibreOffice each total matches Python to within 0.000001, including after the levers are changed | `tests/test_excel.py`, the workbook's `Checks` sheet |
| Every figure in the generated commentary is traced back to the model output | `tests/test_ai.py` |
| The results quoted in the README are regenerated from the model | `tests/test_readme.py` |

Limits of the model are listed in [assumptions_and_limitations.md](assumptions_and_limitations.md).
