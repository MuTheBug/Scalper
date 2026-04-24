"""Configuration for the Dogecoin scalping bot.

Values mirror the parameters prescribed by the strategy PDF
(Comprehensive Quantitative Framework for Dogecoin Scalping, 2026):
100-period EMA macro filter, Lorentzian Classification on the 1m chart,
Stochastic RSI + EWO confluence, 1.5x ATR stops, and a bifurcated
TP1/TP2 exit that risks 1-2% of equity per trade.

`strategy_profile` selects how aggressively the confluence engine fires:
  * "strict"     — faithful to the PDF's stated thresholds; few trades,
                   high-quality setups only.
  * "balanced"   — default; moderate relaxation of Stoch/Lorentzian so
                   signals appear in realistic quantities on a noisy
                   symbol like DOGE.
  * "aggressive" — prioritises signal frequency over setup quality.
                   Expect many trades per day; win-rate will suffer.
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple


@dataclass
class BotConfig:
    # --- Market ---
    symbol: str = "DOGEUSDT"
    quote_asset: str = "USDT"
    execution_interval: str = "1m"
    trend_interval: str = "15m"

    # --- Strategy profile (select at runtime; see apply_profile) ---
    strategy_profile: str = "balanced"

    # --- Trend filter (Step 3) ---
    trend_ema_period: int = 100

    # --- Lorentzian Classification (Step 5) ---
    lorentzian_neighbors: int = 8
    lorentzian_lookback: int = 500
    lorentzian_feature_count: int = 5
    # Sensitivity threshold. PDF recommends +4 to +6 for DOGE volatility,
    # but that's a very tight filter; `balanced` profile uses +3.
    lorentzian_threshold: int = 3
    # Auto-scale: if dataset is smaller than lookback, use this fraction
    # of the available history instead.
    lorentzian_lookback_min_fraction: float = 0.25

    # --- Confluence indicators (Step 6) ---
    stoch_rsi_period: int = 14
    stoch_rsi_k: int = 3
    stoch_rsi_d: int = 3
    stoch_rsi_oversold: float = 35.0
    stoch_rsi_overbought: float = 65.0
    # Allow the k-below-oversold event to have happened within the last
    # N bars, rather than demanding it on the bar immediately prior.
    stoch_cross_lookback: int = 5
    ewo_fast: int = 16
    ewo_slow: int = 26
    volume_ma_period: int = 20
    volume_confirmation_multiplier: float = 1.0

    # --- Alternate entry patterns ---
    # Besides the strict PDF confluence, the bot can optionally take a
    # pullback-to-EMA or momentum-breakout trade. These share the same
    # risk and trend-bias rules.
    enable_pullback_entries: bool = True
    pullback_ema_period: int = 21
    pullback_touch_tolerance: float = 0.002  # within 0.2% of EMA
    enable_breakout_entries: bool = True
    breakout_window: int = 20

    # --- Order flow (Step 4) ---
    orderbook_depth: int = 50
    wall_detection_multiplier: float = 4.0
    absorption_delta_threshold: float = 0.6
    # In backtest (no L2 data) skip order-flow gating altogether.
    skip_order_flow_in_backtest: bool = True

    # --- Risk (Step 7) ---
    atr_period: int = 14
    atr_stop_multiplier: float = 1.5
    risk_per_trade: float = 0.01
    max_risk_per_trade: float = 0.02
    leverage: int = 10

    # --- Small-account handling ---
    small_account_mode: bool = True
    small_account_max_risk: float = 0.25
    absolute_min_capital: float = 0.50

    # --- Bifurcated exit (Steps 8 & 9) ---
    tp1_rr: float = 1.0
    tp1_fraction: float = 0.70
    psar_step: float = 0.02
    psar_max: float = 0.2

    # --- Execution economics ---
    maker_fee: float = 0.0002
    taker_fee: float = 0.0004
    prefer_maker: bool = True
    maker_post_only_timeout_s: int = 5

    # --- Loop cadence ---
    poll_interval_s: float = 2.0
    klines_limit: int = 500

    # --- Operational ---
    testnet: bool = True
    dry_run: bool = True
    log_level: str = "DEBUG"
    log_file: str = "scalper.log"
    verbose_ticks: bool = True

    price_precision: int = 5
    quantity_precision: int = 0
    min_notional: float = 5.0

    trading_hours_utc: Tuple[int, int] = (0, 24)


PROFILES: Dict[str, Dict] = {
    "strict": {
        # Faithful to PDF thresholds; rare but high-conviction.
        "lorentzian_threshold": 5,
        "lorentzian_neighbors": 8,
        "lorentzian_lookback": 2000,
        "stoch_rsi_oversold": 30.0,
        "stoch_rsi_overbought": 70.0,
        "stoch_cross_lookback": 1,
        "volume_confirmation_multiplier": 1.2,
        "enable_pullback_entries": False,
        "enable_breakout_entries": False,
    },
    "balanced": {
        "lorentzian_threshold": 3,
        "lorentzian_neighbors": 8,
        "lorentzian_lookback": 500,
        "stoch_rsi_oversold": 35.0,
        "stoch_rsi_overbought": 65.0,
        "stoch_cross_lookback": 5,
        "volume_confirmation_multiplier": 1.0,
        "enable_pullback_entries": True,
        "enable_breakout_entries": True,
    },
    "aggressive": {
        "lorentzian_threshold": 2,
        "lorentzian_neighbors": 5,
        "lorentzian_lookback": 300,
        "stoch_rsi_oversold": 45.0,
        "stoch_rsi_overbought": 55.0,
        "stoch_cross_lookback": 10,
        "volume_confirmation_multiplier": 0.8,
        "enable_pullback_entries": True,
        "enable_breakout_entries": True,
        "trend_ema_period": 50,
    },
}


def apply_profile(cfg: BotConfig, name: str) -> BotConfig:
    """Mutate cfg in-place with the named profile's overrides."""
    if name not in PROFILES:
        raise ValueError(
            f"Unknown profile '{name}'. Options: {sorted(PROFILES.keys())}"
        )
    cfg.strategy_profile = name
    for key, value in PROFILES[name].items():
        setattr(cfg, key, value)
    return cfg


CONFIG = BotConfig()
apply_profile(CONFIG, CONFIG.strategy_profile)
