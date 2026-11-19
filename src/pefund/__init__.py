"""AI-enabled private equity fund waterfall and carry analytics platform.

Typical use::

    from pefund import load_config, run_fund

    result = run_fund(load_config("configs/base_case.yaml"))
    print(result.metrics())            # IRR, MOIC, DPI, RVPI, TVPI at liquidation
    print(result.quarterly.head())     # quarter-by-quarter cash flows and waterfall tiers
"""

from .config import ConfigError, FundConfig, load_config
from .fund import FundResult, run_fund

__all__ = ["ConfigError", "FundConfig", "FundResult", "load_config", "run_fund"]
__version__ = "1.0.0"
