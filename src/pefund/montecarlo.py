"""Monte Carlo simulation of deal outcomes.

Each trial redraws the portfolio's outcomes and re-runs the full fund model
under both waterfall types, giving a distribution for LP returns, carried
interest and - the reason to simulate at all - the GP clawback, which only
appears in the tail where early winners are followed by late losers.

Shock model (deliberately simple and transparent)
-------------------------------------------------
* One **market factor** per trial moves every exit multiple together and,
  because weak markets also delay exits, shifts every holding period.
* An **idiosyncratic factor** per deal adds deal-specific dispersion to the
  exit multiple and the holding period.
* Each deal has an independent chance of an outright **write-off**.

Multiples are lognormal with a mean of one, so the simulation is centred on
the base case rather than drifting above it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .config import FundConfig, Shocks
from .deals import build_deal_schedule
from .fund import run_fund


@dataclass(frozen=True)
class MonteCarloSettings:
    n_sims: int = 2000
    seed: int = 42
    #: Volatility (lognormal sigma) of the market-wide exit-multiple factor.
    market_vol: float = 0.15
    #: Volatility of each deal's own exit-multiple factor.
    idiosyncratic_vol: float = 0.25
    #: Chance that any one deal is written off, on top of its base outcome.
    write_off_probability: float = 0.05
    #: Quarters of delay per one standard deviation of market weakness.
    hold_shift_per_market_sigma: float = 2.0
    #: Each deal's holding period also moves by up to this many quarters either way.
    hold_shift_noise_quarters: int = 2


def run_monte_carlo(cfg: FundConfig, settings: MonteCarloSettings | None = None) -> pd.DataFrame:
    """Simulate ``settings.n_sims`` trials; one row of results per trial."""
    settings = settings or MonteCarloSettings()
    rng = np.random.default_rng(settings.seed)
    names = [deal.name for deal in cfg.deals]
    n_deals = len(names)
    european_cfg = replace(cfg, waterfall=replace(cfg.waterfall, type="european"))
    american_cfg = replace(cfg, waterfall=replace(cfg.waterfall, type="american"))
    pref = cfg.waterfall.pref_rate

    rows = []
    for trial in range(settings.n_sims):
        z_market = rng.standard_normal()
        market = np.exp(settings.market_vol * z_market - 0.5 * settings.market_vol**2) - 1.0
        z_deal = rng.standard_normal(n_deals)
        deal_factor = np.exp(settings.idiosyncratic_vol * z_deal - 0.5 * settings.idiosyncratic_vol**2) - 1.0
        written_off = rng.random(n_deals) < settings.write_off_probability
        noise = rng.integers(-settings.hold_shift_noise_quarters, settings.hold_shift_noise_quarters + 1, n_deals)
        market_delay = int(round(-settings.hold_shift_per_market_sigma * z_market))

        overrides = {}
        for i, name in enumerate(names):
            override = {"exit_multiple_pct": float(deal_factor[i]), "hold_shift_quarters": int(noise[i])}
            if written_off[i]:
                override["write_off"] = True
            overrides[name] = override
        shocks = Shocks(
            exit_multiple_pct=cfg.shocks.exit_multiple_pct + float(market),
            ebitda_growth_shift=cfg.shocks.ebitda_growth_shift,
            hold_shift_quarters=cfg.shocks.hold_shift_quarters + market_delay,
            deal_overrides=overrides,
        )
        european_trial = replace(european_cfg, shocks=shocks)
        schedule = build_deal_schedule(european_trial)  # identical deals for both waterfalls
        european = run_fund(european_trial, interim=False, schedule=schedule)
        american = run_fund(replace(american_cfg, shocks=shocks), interim=False, schedule=schedule)
        e, a = european.summary, american.summary
        rows.append(
            {
                "trial": trial,
                "market_factor": float(market),
                "write_offs": int(written_off.sum()),
                "gross_moic": e["gross_moic"],
                "gross_irr": e["gross_irr"],
                "net_irr": e["net_irr"],
                "tvpi": e["tvpi"],
                "carry_net": e["carry_net"],
                "management_fees": e["total_management_fees"],
                "lp_profit": e["total_lp_profit"],
                "fund_life_years": e["fund_life_years"],
                "american_net_irr": a["net_irr"],
                "american_tvpi": a["tvpi"],
                "american_carry_gross": a["carry_gross"],
                "american_clawback_uncapped": a["clawback_uncapped"],
                "american_clawback_paid": a["clawback_paid"],
                "american_clawback_shortfall": a["clawback_shortfall"],
                "american_carry_net": a["carry_net"],
                "european_clawback_uncapped": e["clawback_uncapped"],
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs.update(settings=settings, pref_rate=pref, committed_capital=cfg.fund.committed_capital)
    return frame


#: A clawback is "material" above this share of committed capital.  Whole-of-fund
#: waterfalls can show tiny true-ups (late fees called after carry was paid);
#: the threshold separates those from genuine over-distribution of carry.
MATERIAL_CLAWBACK_PCT = 0.001


def summarize_monte_carlo(
    sims: pd.DataFrame, pref_rate: float | None = None, committed_capital: float | None = None
) -> dict[str, float]:
    """Headline statistics from a simulation table."""
    pref = pref_rate if pref_rate is not None else float(sims.attrs.get("pref_rate", 0.08))
    committed = committed_capital if committed_capital is not None else float(sims.attrs.get("committed_capital", 0.0))
    threshold = max(MATERIAL_CLAWBACK_PCT * committed, 1e-6)
    irr = sims["net_irr"].fillna(-1.0)  # an undefined IRR means a total loss
    clawback = sims["american_clawback_uncapped"]
    triggered = clawback > threshold

    def pct(series: pd.Series, q: float) -> float:
        return float(np.nanpercentile(series, q))

    return {
        "n_sims": int(len(sims)),
        "net_irr_mean": float(irr.mean()),
        "net_irr_median": pct(irr, 50),
        "net_irr_p5": pct(irr, 5),
        "net_irr_p25": pct(irr, 25),
        "net_irr_p75": pct(irr, 75),
        "net_irr_p95": pct(irr, 95),
        "tvpi_mean": float(sims["tvpi"].mean()),
        "tvpi_median": pct(sims["tvpi"], 50),
        "tvpi_p5": pct(sims["tvpi"], 5),
        "tvpi_p95": pct(sims["tvpi"], 95),
        "gross_moic_mean": float(sims["gross_moic"].mean()),
        "prob_net_irr_below_pref": float((irr < pref).mean()),
        "prob_capital_loss": float((sims["tvpi"] < 1.0).mean()),
        "prob_no_carry": float((sims["carry_net"] <= 1e-6).mean()),
        "carry_mean": float(sims["carry_net"].mean()),
        "carry_median": pct(sims["carry_net"], 50),
        "carry_p5": pct(sims["carry_net"], 5),
        "carry_p95": pct(sims["carry_net"], 95),
        "prob_clawback_american": float(triggered.mean()),
        "clawback_mean_when_triggered": float(clawback[triggered].mean()) if triggered.any() else 0.0,
        "clawback_p95": pct(clawback, 95),
        "clawback_max": float(clawback.max()),
        "clawback_shortfall_mean_when_triggered": (
            float(sims.loc[triggered, "american_clawback_shortfall"].mean()) if triggered.any() else 0.0
        ),
        "prob_clawback_european": float((sims["european_clawback_uncapped"] > threshold).mean()),
        "clawback_max_european": float(sims["european_clawback_uncapped"].max()),
        "material_clawback_threshold": threshold,
        "american_irr_drag_mean": float((sims["net_irr"] - sims["american_net_irr"]).mean()),
    }
