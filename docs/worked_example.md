# Worked examples

Two small funds that can be checked with a calculator. Both have no fees, no expenses and no GP
commitment, so the only thing happening is the waterfall. The configurations are in
[`configs/examples/`](../configs/examples), and
[`tests/test_worked_examples.py`](../tests/test_worked_examples.py) asserts every figure on this
page against the engine.

Terms in both examples: 8% preferred return compounded annually, 100% GP catch-up, 20% carry.

## Example 1: one deal, all four tiers

LPs contribute 100 on 31 March 2022. The deal is sold for 200 on 31 March 2026.

```bash
pefund run --config configs/examples/single_deal_four_tiers.yaml
pefund explain --config configs/examples/single_deal_four_tiers.yaml --date 2026-03-31
```

The holding period is 1,461 days (2024 has a leap day), so `τ = 1461 / 365 = 4.0027` years.

| Tier | Calculation | LPs | GP | Left to distribute |
| --- | --- | ---: | ---: | ---: |
| Cash available | | | | 200.00 |
| 1. Return of capital | capital contributed | 100.00 | | 100.00 |
| 2. Preferred return | 100 × (1.08^4.0027 − 1) | 36.08 | | 63.92 |
| 3. GP catch-up | 20% × 36.08 ÷ (100% − 20%) | | 9.02 | 54.90 |
| 4. Residual split | 54.90 split 80 / 20 | 43.92 | 10.98 | – |
| **Total** | | **180.00** | **20.00** | |

Things to notice:

- **The catch-up restores the headline split.** After tier 2 the LPs hold 36.08 of profit and the
  GP none. Tier 3 gives the GP 9.02, which is 20% of the 45.10 of profit distributed so far
  (36.08 + 9.02). From there the 80 / 20 split keeps it at 20%.
- **With a full catch-up, a fund that clears its hurdle pays carry on all profit.** The GP ends with
  20.00, exactly 20% of the 100 gain. The preferred return decides *whether and when* carry is paid,
  not *how much*, which is why the preferred-return rate has almost no effect on returns in the base
  case (see the tornado chart).
- **Net return.** LPs receive 180 on 100: a net TVPI of 1.80x and a net IRR of
  `1.80^(1/4.0027) − 1 = 15.8%`, against a gross 2.00x and 18.9%.

What changes if the terms change (same deal):

| Terms | Carry | Why |
| --- | ---: | --- |
| 100% catch-up (above) | 20.00 | GP catches up fully |
| 50% catch-up | 20.00 | Catch-up needs 24.05 instead of 9.02, still within the 63.92 available |
| No catch-up (hard hurdle) | 12.78 | 20% of the 63.92 above the hurdle only |
| No preferred return | 20.00 | Straight 80 / 20 on the 100 of profit |

## Example 2: an early winner and a late loss

LPs contribute 100 on 31 March 2022, split equally between two deals.

- **Winner:** 50 invested, sold for 150 on 31 March 2024 (3.0x).
- **Loser:** 50 invested, written off on 31 March 2027.

The fund as a whole turns 100 into 150. The profit is 50, so the GP should end with 20% × 50 = 10.

```bash
pefund compare --config configs/examples/two_deal_clawback.yaml
```

### Whole-of-fund

At the March 2024 distribution **all 100** of contributed capital is eligible, the loser's included.
`τ = 731 / 365 = 2.0027` years.

| Tier | Calculation | LPs | GP | Left |
| --- | --- | ---: | ---: | ---: |
| Cash available | | | | 150.00 |
| 1. Return of capital | all capital contributed | 100.00 | | 50.00 |
| 2. Preferred return | 100 × (1.08^2.0027 − 1) | 16.66 | | 33.34 |
| 3. GP catch-up | 20% × 16.66 ÷ 80% | | 4.17 | 29.17 |
| 4. Residual split | 29.17 split 80 / 20 | 23.34 | 5.83 | – |
| **Total** | | **140.00** | **10.00** | |

The loser later returns nothing. The GP holds 10.00, exactly 20% of the fund's profit, and nothing
needs correcting.

### Deal-by-deal

At the same distribution only the **50 invested in the realised deal** is eligible.

| Tier | Calculation | LPs | GP | Left |
| --- | --- | ---: | ---: | ---: |
| Cash available | | | | 150.00 |
| 1. Return of capital | capital in the realised deal | 50.00 | | 100.00 |
| 2. Preferred return | 50 × (1.08^2.0027 − 1) | 8.33 | | 91.67 |
| 3. GP catch-up | 20% × 8.33 ÷ 80% | | 2.08 | 89.58 |
| 4. Residual split | 89.58 split 80 / 20 | 71.67 | 17.92 | – |
| **Total** | | **130.00** | **20.00** | |

The GP has been paid 20.00, which is 20% of the winner's gain of 100. Three years later the loser
is written off and the fund's profit turns out to be 50, not 100.

**Clawback test at liquidation (March 2027)**

| | Calculation | Amount |
| --- | --- | ---: |
| Fund profit | LP distributions 130 + carry 20 − contributions 100 | 50.00 |
| (A) Excess carry | 20 − 20% × 50 | 10.00 |
| (B) LP shortfall | 100 + whole-fund pref 16.66 − 130, floored at zero | 0.00 |
| Clawback before limits | greater of (A) and (B) | 10.00 |
| Limit: carry received | | 20.00 |
| Limit: after-tax carry | 20 × (1 − 40%) | 12.00 |
| **Clawback paid** | | **10.00** |

### Same money, different timing

| | Whole-of-fund | Deal-by-deal |
| --- | ---: | ---: |
| LPs receive in March 2024 | 140.00 | 130.00 |
| LPs receive in March 2027 (clawback) | – | 10.00 |
| LP total | 140.00 | 140.00 |
| GP carry, net of clawback | 10.00 | 10.00 |
| Net TVPI | 1.40x | 1.40x |
| **Net IRR** | **18.3%** | **16.7%** |

Both waterfalls end in the same place. Deal-by-deal costs the LPs 1.6 points of IRR because 10 of
their money sat with the GP for three years, and they carried the risk that it would not come back.

### When the clawback does not make LPs whole

Here the clawback (10.00) is below the after-tax limit (12.00), so the LPs recover everything. Had
the excess carry been larger than 60% of what the GP received, the limit would bind and the LPs would
bear the difference. The base case's "Late losses, deal-by-deal" scenario shows this: carry of 29.2
is paid on early winners, the full correction would be 29.2, the after-tax limit caps the clawback at
17.5, and the LPs are left 11.7 short. Under whole-of-fund the same outcomes pay no carry at all.

### What write-down protection does

The example switches `writedowns_as_realized` off so that the loss stays invisible until 2027. With
it on, the model treats the loser's interim write-down as a realised loss. By March 2024 the loser
is marked at 37.5 against a cost of 50, so 12.5 more capital must be returned before carry:

| | Protection off | Protection on |
| --- | ---: | ---: |
| Eligible capital in March 2024 | 50.00 | 62.50 |
| Carry paid in March 2024 | 20.00 | 17.50 |
| Clawback in March 2027 | 10.00 | 7.50 |
| Carry, net of clawback | 10.00 | 10.00 |

The protection reduces the overpayment. It cannot remove it, because the loss was only partly
visible when the winner was sold.
