"""Typed, validated configuration for the fund model.

One nested dictionary describes a fund: its terms, its limited partners and its
deals.  The dictionary can come from YAML, JSON or the Excel input template.
This module turns it into dataclasses, checks every value and raises a
:class:`ConfigError` with a readable message when something is wrong, so a
typo in an assumption file never silently produces a wrong number.

All money amounts are in one unit (the sample files use USD millions).
All rates are decimals: ``0.08`` means 8%.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .timeline import quarter_end, quarter_index, to_date


class ConfigError(ValueError):
    """Raised when the fund configuration is invalid or internally inconsistent."""


FEE_BASES = ("committed", "net_invested", "nav")
WATERFALL_TYPES = ("european", "american")
PREF_COMPOUNDING = ("annual", "quarterly", "simple")
EXPENSE_ALLOCATION = ("pro_rata", "all_to_date")
PROCEEDS_KINDS = ("dividend_recap", "partial_exit")
DEAL_OVERRIDE_KEYS = (
    "exit_multiple_pct",
    "ebitda_growth_shift",
    "hold_shift_quarters",
    "gross_moic",
    "write_off",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _build(cls: type, data: Any, path: str) -> Any:
    """Create dataclass ``cls`` from a mapping, rejecting unknown keys."""
    if data is None:
        data = {}
    if isinstance(data, cls):
        return data
    _require(isinstance(data, dict), f"'{path}' must be a mapping, got {type(data).__name__}")
    allowed = [f.name for f in fields(cls)]
    unknown = sorted(set(data) - set(allowed))
    _require(not unknown, f"Unknown key(s) {unknown} in '{path}'. Allowed keys: {allowed}")
    try:
        return cls(**data)
    except TypeError as exc:  # missing required field
        raise ConfigError(f"'{path}': {exc}") from exc


# --------------------------------------------------------------------------- #
# Fund terms
# --------------------------------------------------------------------------- #
@dataclass
class FundTerms:
    """Headline fund facts."""

    name: str = "Sample Fund I"
    currency: str = "USD"
    unit: str = "m"
    first_close: dt.date = dt.date(2022, 3, 31)
    committed_capital: float = 500.0
    #: GP commitment as a share of total commitments.  It invests alongside the
    #: LPs, pays no management fee and bears no carried interest.
    gp_commitment_pct: float = 0.02
    investment_period_years: float = 5.0
    term_years: float = 10.0
    max_extension_years: float = 2.0
    #: "As of" date for the interim snapshot (DPI / RVPI / TVPI with unrealised NAV).
    valuation_date: dt.date | None = None

    def __post_init__(self) -> None:
        self.first_close = quarter_end(to_date(self.first_close))
        if self.valuation_date is not None:
            self.valuation_date = quarter_end(to_date(self.valuation_date))
        _require(self.committed_capital > 0, "fund.committed_capital must be positive")
        _require(0 <= self.gp_commitment_pct < 1, "fund.gp_commitment_pct must be in [0, 1)")
        _require(self.investment_period_years > 0, "fund.investment_period_years must be positive")
        _require(
            self.term_years >= self.investment_period_years,
            "fund.term_years must be at least the investment period",
        )
        _require(self.max_extension_years >= 0, "fund.max_extension_years cannot be negative")
        for name in ("investment_period_years", "term_years", "max_extension_years"):
            quarters = getattr(self, name) * 4
            _require(
                abs(quarters - round(quarters)) < 1e-9,
                f"fund.{name} must be a whole number of quarters",
            )

    @property
    def lp_pct(self) -> float:
        return 1.0 - self.gp_commitment_pct

    @property
    def lp_commitment(self) -> float:
        return self.committed_capital * self.lp_pct

    @property
    def gp_commitment(self) -> float:
        return self.committed_capital * self.gp_commitment_pct

    @property
    def investment_period_quarters(self) -> int:
        return int(round(self.investment_period_years * 4))

    @property
    def max_quarters(self) -> int:
        """Last permitted quarter index (term plus all extensions)."""
        return int(round((self.term_years + self.max_extension_years) * 4))


@dataclass
class ManagementFeeTerms:
    """Management fee, charged quarterly in advance on LP capital only."""

    rate_investment_period: float = 0.02
    basis_investment_period: str = "committed"
    rate_post_investment_period: float = 0.02
    basis_post_investment_period: str = "net_invested"
    #: Share of portfolio-company fees (transaction / monitoring) credited
    #: against the management fee.  Unused credit carries forward.
    offset_pct: float = 1.0
    #: Transaction fee the manager charges a portfolio company at entry,
    #: as a share of the equity invested.
    transaction_fee_pct: float = 0.0
    #: Annual monitoring fee as a share of cost still invested.
    monitoring_fee_pct: float = 0.0

    def __post_init__(self) -> None:
        for name in ("rate_investment_period", "rate_post_investment_period"):
            _require(0 <= getattr(self, name) < 0.2, f"management_fee.{name} must be in [0, 0.2)")
        for name in ("basis_investment_period", "basis_post_investment_period"):
            _require(
                getattr(self, name) in FEE_BASES,
                f"management_fee.{name} must be one of {FEE_BASES}",
            )
        _require(0 <= self.offset_pct <= 1, "management_fee.offset_pct must be in [0, 1]")
        _require(0 <= self.transaction_fee_pct < 0.2, "management_fee.transaction_fee_pct out of range")
        _require(0 <= self.monitoring_fee_pct < 0.2, "management_fee.monitoring_fee_pct out of range")


@dataclass
class ExpenseTerms:
    """Partnership expenses, borne by every partner pro rata to commitments."""

    organizational: float = 1.5
    annual_operating: float = 1.0

    def __post_init__(self) -> None:
        _require(self.organizational >= 0, "expenses.organizational cannot be negative")
        _require(self.annual_operating >= 0, "expenses.annual_operating cannot be negative")


@dataclass
class WaterfallTerms:
    """Distribution waterfall terms."""

    #: ``european`` = whole-of-fund; ``american`` = deal-by-deal with loss carryforward.
    type: str = "european"
    pref_rate: float = 0.08
    pref_compounding: str = "annual"
    #: Share of post-pref distributions paid to the GP until it has caught up.
    #: ``1.0`` = full catch-up, ``0.0`` = no catch-up (a "hard" hurdle).
    catch_up_rate: float = 1.0
    carry_rate: float = 0.20
    #: American only: how fees and expenses enter the realised capital base.
    expense_allocation: str = "pro_rata"
    #: American only: treat unrealised write-downs as realised losses.
    writedowns_as_realized: bool = True

    def __post_init__(self) -> None:
        _require(self.type in WATERFALL_TYPES, f"waterfall.type must be one of {WATERFALL_TYPES}")
        _require(0 <= self.pref_rate < 1, "waterfall.pref_rate must be in [0, 1)")
        _require(
            self.pref_compounding in PREF_COMPOUNDING,
            f"waterfall.pref_compounding must be one of {PREF_COMPOUNDING}",
        )
        _require(0 <= self.carry_rate < 1, "waterfall.carry_rate must be in [0, 1)")
        _require(0 <= self.catch_up_rate <= 1, "waterfall.catch_up_rate must be in [0, 1]")
        _require(
            self.catch_up_rate == 0 or self.catch_up_rate > self.carry_rate,
            "waterfall.catch_up_rate must be 0 (no catch-up) or greater than carry_rate; "
            "otherwise the GP can never catch up",
        )
        _require(
            self.expense_allocation in EXPENSE_ALLOCATION,
            f"waterfall.expense_allocation must be one of {EXPENSE_ALLOCATION}",
        )


@dataclass
class ClawbackTerms:
    """GP clawback at liquidation and the carry escrow that secures it."""

    enabled: bool = True
    #: Cap the clawback at carry received net of an assumed tax rate.
    after_tax_limit: bool = True
    assumed_tax_rate: float = 0.40
    #: Share of every carry distribution held in escrow until liquidation.
    escrow_pct: float = 0.0

    def __post_init__(self) -> None:
        _require(0 <= self.assumed_tax_rate < 1, "clawback.assumed_tax_rate must be in [0, 1)")
        _require(0 <= self.escrow_pct <= 1, "clawback.escrow_pct must be in [0, 1]")


@dataclass
class RecyclingTerms:
    """Recallable distributions: cost returned during the investment period can be re-called."""

    enabled: bool = False
    cap_pct_of_commitments: float = 0.10

    def __post_init__(self) -> None:
        _require(0 <= self.cap_pct_of_commitments <= 1, "recycling.cap_pct_of_commitments must be in [0, 1]")


@dataclass
class SubscriptionLineTerms:
    """Capital-call (subscription) facility used during the investment period."""

    enabled: bool = False
    #: How long each drawing stays outstanding before LPs are called.
    quarters_outstanding: int = 2
    interest_rate: float = 0.065

    def __post_init__(self) -> None:
        _require(
            isinstance(self.quarters_outstanding, int) and 1 <= self.quarters_outstanding <= 8,
            "subscription_line.quarters_outstanding must be an integer between 1 and 8",
        )
        _require(0 <= self.interest_rate < 0.5, "subscription_line.interest_rate out of range")


@dataclass
class ValuationPolicy:
    """How unrealised investments are marked between entry and exit."""

    #: Quarters an investment is held at cost before marks move towards exit value.
    hold_at_cost_quarters: int = 4

    def __post_init__(self) -> None:
        _require(
            isinstance(self.hold_at_cost_quarters, int) and self.hold_at_cost_quarters >= 0,
            "valuation.hold_at_cost_quarters must be a non-negative integer",
        )


@dataclass
class Shocks:
    """Scenario shocks applied on top of the base deal assumptions."""

    #: Relative change in the exit valuation multiple (EV/EBITDA for operating
    #: deals, equity value for direct-MOIC deals).  ``-0.10`` = 10% lower.
    exit_multiple_pct: float = 0.0
    #: Additive change to EBITDA growth (operating deals).  ``-0.02`` = 2 points lower.
    ebitda_growth_shift: float = 0.0
    #: Quarters added to (or removed from) every holding period.
    hold_shift_quarters: int = 0
    #: Per-deal shocks keyed by deal name; see ``DEAL_OVERRIDE_KEYS``.
    deal_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(self.exit_multiple_pct > -1, "shocks.exit_multiple_pct must be greater than -1")
        _require(
            float(self.hold_shift_quarters).is_integer(),
            "shocks.hold_shift_quarters must be a whole number of quarters",
        )
        self.hold_shift_quarters = int(self.hold_shift_quarters)
        self.deal_overrides = dict(self.deal_overrides or {})
        for name, override in self.deal_overrides.items():
            _require(isinstance(override, dict), f"shocks.deal_overrides['{name}'] must be a mapping")
            unknown = sorted(set(override) - set(DEAL_OVERRIDE_KEYS))
            _require(
                not unknown,
                f"Unknown key(s) {unknown} in shocks.deal_overrides['{name}']. "
                f"Allowed keys: {list(DEAL_OVERRIDE_KEYS)}",
            )


# --------------------------------------------------------------------------- #
# Investors and deals
# --------------------------------------------------------------------------- #
@dataclass
class Investor:
    """One limited partner in the investor register."""

    name: str
    commitment: float

    def __post_init__(self) -> None:
        _require(self.commitment > 0, f"investor '{self.name}': commitment must be positive")


@dataclass
class Investment:
    """One equity cheque into a deal (initial investment or follow-on)."""

    date: dt.date
    amount: float

    def __post_init__(self) -> None:
        self.date = quarter_end(to_date(self.date))
        _require(self.amount > 0, "investment amount must be positive")


@dataclass
class ProceedsEvent:
    """An interim realisation before the final exit.

    ``share`` is the share of the deal's *total* lifetime proceeds received at
    this event.  The final exit receives whatever share is left.
    """

    date: dt.date
    kind: str
    share: float
    #: ``partial_exit`` only: share of the holding sold (drives cost relieved).
    stake_sold: float | None = None

    def __post_init__(self) -> None:
        self.date = quarter_end(to_date(self.date))
        _require(self.kind in PROCEEDS_KINDS, f"proceeds event kind must be one of {PROCEEDS_KINDS}")
        _require(0 < self.share < 1, "proceeds event share must be between 0 and 1")
        if self.kind == "partial_exit":
            _require(
                self.stake_sold is not None and 0 < self.stake_sold < 1,
                "partial_exit events need stake_sold between 0 and 1",
            )
        else:
            _require(self.stake_sold is None, "stake_sold only applies to partial_exit events")


@dataclass
class OperatingCase:
    """Operating assumptions that drive a buyout deal's exit value.

    Equity value at exit = EBITDA x EV/EBITDA multiple - net debt.  The fund's
    ownership is its cheque divided by the entry equity value.
    """

    entry_ebitda: float
    entry_ev_multiple: float
    #: Net debt / EBITDA at entry (turns of leverage).
    entry_leverage: float
    #: EBITDA compound annual growth rate over the holding period.
    ebitda_growth: float
    exit_ev_multiple: float
    #: Share of each year's EBITDA that pays down net debt.
    cash_conversion: float = 0.30

    def __post_init__(self) -> None:
        _require(self.entry_ebitda > 0, "operating.entry_ebitda must be positive")
        _require(self.entry_ev_multiple > 0, "operating.entry_ev_multiple must be positive")
        _require(
            0 <= self.entry_leverage < self.entry_ev_multiple,
            "operating.entry_leverage must be non-negative and below the entry EV multiple",
        )
        _require(-0.9 < self.ebitda_growth < 2, "operating.ebitda_growth out of range")
        _require(self.exit_ev_multiple > 0, "operating.exit_ev_multiple must be positive")
        _require(0 <= self.cash_conversion <= 1, "operating.cash_conversion must be in [0, 1]")

    @property
    def entry_equity(self) -> float:
        return self.entry_ebitda * (self.entry_ev_multiple - self.entry_leverage)


@dataclass
class Deal:
    """A portfolio investment.

    Give either ``gross_moic`` (direct mode: total proceeds = MOIC x cost) or an
    ``operating`` case (driver mode: proceeds derived from EBITDA, multiple and
    leverage).  A write-off is a direct deal with ``gross_moic: 0``.
    """

    name: str
    investments: list[Investment]
    exit_date: dt.date
    sector: str = "Unclassified"
    strategy: str = "buyout"
    gross_moic: float | None = None
    operating: OperatingCase | None = None
    proceeds_events: list[ProceedsEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        label = f"deal '{self.name}'"
        _require(bool(self.investments), f"{label}: at least one investment is required")
        self.investments = [_build(Investment, i, f"{label}.investments") for i in self.investments]
        self.investments.sort(key=lambda i: i.date)
        self.exit_date = quarter_end(to_date(self.exit_date))
        self.proceeds_events = [
            _build(ProceedsEvent, e, f"{label}.proceeds_events") for e in (self.proceeds_events or [])
        ]
        self.proceeds_events.sort(key=lambda e: e.date)
        if self.operating is not None:
            self.operating = _build(OperatingCase, self.operating, f"{label}.operating")
        _require(
            (self.gross_moic is None) != (self.operating is None),
            f"{label}: give exactly one of 'gross_moic' or 'operating'",
        )
        if self.gross_moic is not None:
            _require(self.gross_moic >= 0, f"{label}: gross_moic cannot be negative")
        if self.operating is not None:
            _require(
                len(self.investments) == 1,
                f"{label}: operating-case deals take a single investment (use gross_moic for follow-ons)",
            )
            ownership = self.invested / self.operating.entry_equity
            _require(
                ownership <= 1.0 + 1e-9,
                f"{label}: cheque of {self.invested:g} exceeds entry equity value "
                f"{self.operating.entry_equity:g} (ownership would be {ownership:.0%})",
            )
        _require(
            self.investments[-1].date < self.exit_date,
            f"{label}: every investment must be dated before the exit",
        )
        _require(
            sum(e.share for e in self.proceeds_events) < 1.0,
            f"{label}: interim proceeds shares must sum to less than 1",
        )
        for event in self.proceeds_events:
            _require(
                self.investments[0].date < event.date < self.exit_date,
                f"{label}: interim proceeds must fall between the first investment and the exit",
            )

    @property
    def invested(self) -> float:
        return float(sum(i.amount for i in self.investments))

    @property
    def entry_date(self) -> dt.date:
        return self.investments[0].date


# --------------------------------------------------------------------------- #
# Top-level configuration
# --------------------------------------------------------------------------- #
_SECTIONS: dict[str, type] = {
    "fund": FundTerms,
    "management_fee": ManagementFeeTerms,
    "expenses": ExpenseTerms,
    "waterfall": WaterfallTerms,
    "clawback": ClawbackTerms,
    "recycling": RecyclingTerms,
    "subscription_line": SubscriptionLineTerms,
    "valuation": ValuationPolicy,
    "shocks": Shocks,
}


@dataclass
class FundConfig:
    """Complete description of a fund: terms, investors, deals and shocks."""

    fund: FundTerms = field(default_factory=FundTerms)
    management_fee: ManagementFeeTerms = field(default_factory=ManagementFeeTerms)
    expenses: ExpenseTerms = field(default_factory=ExpenseTerms)
    waterfall: WaterfallTerms = field(default_factory=WaterfallTerms)
    clawback: ClawbackTerms = field(default_factory=ClawbackTerms)
    recycling: RecyclingTerms = field(default_factory=RecyclingTerms)
    subscription_line: SubscriptionLineTerms = field(default_factory=SubscriptionLineTerms)
    valuation: ValuationPolicy = field(default_factory=ValuationPolicy)
    shocks: Shocks = field(default_factory=Shocks)
    investors: list[Investor] = field(default_factory=list)
    deals: list[Deal] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FundConfig:
        _require(isinstance(data, dict), "Configuration must be a mapping at the top level")
        allowed = [*_SECTIONS, "investors", "deals"]
        unknown = sorted(set(data) - set(allowed))
        _require(not unknown, f"Unknown top-level section(s) {unknown}. Allowed: {allowed}")
        kwargs: dict[str, Any] = {
            name: _build(section_cls, copy.deepcopy(data.get(name)), name)
            for name, section_cls in _SECTIONS.items()
        }
        kwargs["investors"] = [
            _build(Investor, copy.deepcopy(i), "investors") for i in (data.get("investors") or [])
        ]
        kwargs["deals"] = [_build(Deal, copy.deepcopy(d), "deals") for d in (data.get("deals") or [])]
        config = cls(**kwargs)
        config.validate()
        return config

    @classmethod
    def from_file(cls, path: str | Path) -> FundConfig:
        """Load a configuration from ``.yaml`` / ``.yml`` / ``.json`` / ``.xlsx``."""
        path = Path(path)
        _require(path.exists(), f"Configuration file not found: {path}")
        suffix = path.suffix.lower()
        if suffix in (".yaml", ".yml"):
            return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
        if suffix == ".json":
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if suffix in (".xlsx", ".xlsm"):
            from .reporting.excel_inputs import read_config_workbook

            return cls.from_dict(read_config_workbook(path))
        raise ConfigError(f"Unsupported configuration format: {suffix}")

    # ------------------------------------------------------------------ #
    # Validation that needs more than one section
    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        _require(bool(self.deals), "At least one deal is required")
        names = [d.name for d in self.deals]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        _require(not duplicates, f"Deal names must be unique; duplicated: {duplicates}")

        first_close = self.fund.first_close
        ip_quarters = self.fund.investment_period_quarters
        max_quarters = self.fund.max_quarters
        for deal in self.deals:
            entry_q = quarter_index(first_close, deal.entry_date)
            exit_q = quarter_index(first_close, deal.exit_date)
            _require(entry_q >= 0, f"deal '{deal.name}': first investment is before the first close")
            _require(
                entry_q <= ip_quarters,
                f"deal '{deal.name}': new investments must be made within the investment period "
                f"(follow-ons may come later)",
            )
            _require(
                exit_q <= max_quarters,
                f"deal '{deal.name}': exit falls after the fund term including extensions",
            )

        unknown = sorted(set(self.shocks.deal_overrides) - set(names))
        _require(not unknown, f"shocks.deal_overrides refers to unknown deal(s): {unknown}")

        if self.investors:
            total = sum(i.commitment for i in self.investors)
            _require(
                abs(total - self.fund.lp_commitment) < 1e-6 * max(1.0, self.fund.lp_commitment),
                f"Investor commitments sum to {total:,.4f} but LP commitments are "
                f"{self.fund.lp_commitment:,.4f} (committed capital less the GP commitment)",
            )
            investor_names = [i.name for i in self.investors]
            _require(len(set(investor_names)) == len(investor_names), "Investor names must be unique")

        if self.fund.valuation_date is not None:
            _require(
                self.fund.valuation_date >= first_close,
                "fund.valuation_date cannot be before the first close",
            )

    # ------------------------------------------------------------------ #
    # Serialisation and overrides
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """Plain-Python dictionary (dates as ISO strings) that round-trips through ``from_dict``."""

        def clean(value: Any) -> Any:
            if isinstance(value, dt.date):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if not (k == "stake_sold" and v is None)}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value

        raw = clean(asdict(self))
        for deal in raw["deals"]:
            for key in ("gross_moic", "operating"):
                if deal[key] is None:
                    del deal[key]
            if not deal["proceeds_events"]:
                del deal["proceeds_events"]
        return raw

    def with_overrides(self, overrides: dict[str, Any] | None) -> FundConfig:
        """Return a new configuration with ``overrides`` deep-merged in.

        ``overrides`` mirrors the configuration layout, for example
        ``{"waterfall": {"carry_rate": 0.25}, "shocks": {"hold_shift_quarters": 4}}``.
        Lists (``deals``, ``investors``) are replaced, not merged.
        """
        if not overrides:
            return self
        return FundConfig.from_dict(_deep_merge(self.to_dict(), overrides))

    def with_values(self, values: dict[str, Any]) -> FundConfig:
        """Override by dotted path, e.g. ``{"waterfall.pref_rate": 0.06}``."""
        nested: dict[str, Any] = {}
        for dotted, value in values.items():
            cursor = nested
            parts = dotted.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            cursor[parts[-1]] = value
        return self.with_overrides(nested)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | Path) -> FundConfig:
    """Convenience wrapper around :meth:`FundConfig.from_file`."""
    return FundConfig.from_file(path)
