# Glossary

Terms as they are used in this project. Where a term maps to a setting or an output, its name in the
configuration or in the results is given in `code`.

## People and vehicles

| Term | Meaning |
| --- | --- |
| **GP** (general partner) | The manager of the fund. Earns the management fee and the carried interest, and invests its own money through the GP commitment. |
| **LP** (limited partner) | An investor in the fund: a pension plan, endowment, insurer, family office. Commits capital and receives distributions. |
| **LPA** (limited partnership agreement) | The contract that defines the fund's terms, including the waterfall. |
| **GP commitment** | The manager's own investment in the fund, typically 1% to 5% of commitments. Pays no fee and bears no carry. `fund.gp_commitment_pct` |

## Capital

| Term | Meaning |
| --- | --- |
| **Commitment** | The amount an investor has promised. Not paid up front. `fund.committed_capital` |
| **Capital call** (drawdown) | A request to investors to pay part of their commitment, made when the fund needs cash. `lp_contributions` |
| **Paid-in capital** | Cumulative capital called. The denominator of DPI, RVPI and TVPI. |
| **Unfunded commitment** | Commitment not yet called. `lp_unfunded` |
| **Investment period** | The first years of the fund (commonly five), when new deals may be made. `fund.investment_period_years` |
| **Recycling** | Re-calling capital that was returned early, so the fund can invest more than once. `recycling` |
| **Subscription line** | A bank facility secured on investors' commitments, used to fund deals before calling capital. `subscription_line` |

## Fees and expenses

| Term | Meaning |
| --- | --- |
| **Management fee** | The annual fee paid to the manager, commonly 2% of commitments in the investment period and a lower amount afterwards. `management_fee` |
| **Fee basis** | What the fee rate is applied to: commitments, net invested capital or NAV. |
| **Step-down** | The reduction in fee rate or basis after the investment period. |
| **Transaction / monitoring fees** | Fees the manager charges portfolio companies directly. |
| **Fee offset** | The share of those portfolio-company fees credited against the management fee. `management_fee.offset_pct` |
| **Partnership expenses** | Costs borne by the fund itself: formation, audit, administration, legal. `expenses` |

## The waterfall

| Term | Meaning |
| --- | --- |
| **Distribution waterfall** | The order in which cash from realisations is shared between LPs and the GP. |
| **Return of capital** | Tier 1. LPs get back the capital they contributed. |
| **Preferred return** (pref, hurdle) | Tier 2. A minimum annual return to LPs, commonly 8%, before the GP earns carry. `waterfall.pref_rate` |
| **GP catch-up** | Tier 3. The GP receives most or all of the next cash until it holds its carry share of all profit distributed. `waterfall.catch_up_rate` |
| **Carried interest** (carry) | The GP's share of profit, commonly 20%. Tier 4 splits what remains 80 / 20. `waterfall.carry_rate` |
| **Hard hurdle** | No catch-up: the GP earns carry only on profit above the preferred return. |
| **Whole-of-fund** (European) waterfall | Carry is paid only after LPs have received *all* contributed capital plus the preferred return on it. |
| **Deal-by-deal** (American) waterfall | Carry is paid as each deal is realised, after returning the capital in realised deals with their share of fees and expenses. |
| **Loss carryforward** | Under deal-by-deal, a realised loss must be made up from later exits before more carry is paid. |
| **Clawback** | The GP's obligation to return carry at the end of the fund if it received more than its share of the fund's overall profit. `clawback` |
| **After-tax limit** | A cap on the clawback at the carry received net of tax, because the GP cannot return what it paid in tax. |
| **Escrow** (holdback) | A share of each carry payment held back to secure the clawback. `clawback.escrow_pct` |
| **Accrued carry** | Carry the GP would be owed if the remaining portfolio were sold at fair value today. Unrealised. `accrued_carry` |

## Deals

| Term | Meaning |
| --- | --- |
| **Realisation** (exit) | The sale of a portfolio company, in whole or in part. |
| **Partial exit** | Sale of part of a holding; relieves part of its cost. |
| **Dividend recapitalisation** | A dividend paid to the fund, usually funded by new debt at the company. |
| **Cost relieved** | The part of a deal's cost that a realisation is treated as returning. |
| **Write-down / write-off** | A reduction of a holding's value below cost; a write-off is a total loss. |
| **EV / EBITDA multiple** | Enterprise value divided by EBITDA. The usual way a buyout is priced at entry and exit. |
| **Leverage** | Net debt divided by EBITDA. |
| **Value-creation bridge** | The split of a deal's gain between EBITDA growth, multiple expansion and debt paydown. |
| **Fair value** (NAV) | The estimated current value of unrealised holdings. |

## Performance

| Term | Meaning |
| --- | --- |
| **IRR** | Internal rate of return: the annual rate that discounts a set of dated cash flows to zero. Sensitive to timing. |
| **MOIC** | Multiple of invested capital: total value divided by capital invested. Ignores time. |
| **Gross** | Measured on the deals, before fund-level fees, expenses and carry. |
| **Net** | Measured on the LPs' own cash flows, after everything. |
| **DPI** | Distributions to paid-in capital. Cash actually returned. |
| **RVPI** | Residual value to paid-in capital. Value still held, net of accrued carry. |
| **TVPI** | Total value to paid-in capital. DPI plus RVPI: the net multiple. |
| **Gross-to-net spread** | Gross IRR less net IRR: what fees, expenses and carry cost LPs. |
| **J-curve** | The typical path of cumulative net cash flow: negative for years while capital is called and fees are paid, then rising as exits come. |
| **PME** | Public-market equivalent. Compares the fund with investing the same cash flows in a public index; above 1.0 means the fund did better. |
| **bps** | Basis points. 100 bps is one percentage point. |
