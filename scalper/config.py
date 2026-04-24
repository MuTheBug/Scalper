"""Configuration for the Dogecoin scalping bot.

Values mirror the parameters prescribed by the strategy PDF
(Comprehensive Quantitative Framework for Dogecoin Scalping, 2026):
100-period EMA macro filter, Lorentzian Classification on the 1m chart,
Stochastic RSI + EWO confluence, 1.5x ATR stops, and a bifurcated
TP1/TP2 exit that risks 1-2% of equity per trade.
"""

from dataclasses import dataclass
from typing import Tuple


@dataclass
class BotConfig:
    # --- Market ---
    symbol: str = "DOGEUSDT"
    quote_asset: str = "USDT"
    execution_interval: str = "1m"
    trend_interval: str = "15m"

    # --- Trend filter (Step 3) ---
    trend_ema_period: int = 100

    # --- Lorentzian Classification (Step 5) ---
    lorentzian_neighbors: int = 8
    lorentzian_lookback: int = 2000
    lorentzian_feature_count: int = 5
    # Sensitivity threshold: PDF recommends +4 to +6 for DOGE volatility.
    lorentzian_threshold: int = 5

    # --- Confluence indicators (Step 6) ---
    stoch_rsi_period: int = 14
    stoch_rsi_k: int = 3
    stoch_rsi_d: int = 3
    stoch_rsi_oversold: float = 30.0
    stoch_rsi_overbought: float = 70.0
    ewo_fast: int = 16
    ewo_slow: int = 26
    volume_ma_period: int = 20
    volume_confirmation_multiplier: float = 1.2

    # --- Order flow (Step 4) ---
    orderbook_depth: int = 50
    wall_detection_multiplier: float = 4.0
    absorption_delta_threshold: float = 0.6

    # --- Risk (Step 7) ---
    atr_period: int = 14
    atr_stop_multiplier: float = 1.5
    risk_per_trade: float = 0.01  # 1% of equity
    max_risk_per_trade: float = 0.02
    leverage: int = 10

    # --- Small-account handling ---
    # When True and the computed 1% position is below the exchange's
    # min-notional, the bot widens risk up to `small_account_max_risk`
    # so the trade can still execute. This is the only way a sub-$5
    # wallet will ever satisfy Binance's $5 MIN_NOTIONAL filter.
    small_account_mode: bool = True
    small_account_max_risk: float = 0.25  # cap the auto-widened risk at 25%
    absolute_min_capital: float = 0.50  # refuse to trade below this (USDT)

    # --- Bifurcated exit (Steps 8 & 9) ---
    tp1_rr: float = 1.0
    tp1_fraction: float = 0.70  # liquidate 70% at 1:1
    psar_step: float = 0.02
    psar_max: float = 0.2

    # --- Execution economics (Step 1) ---
    # Binance Futures VIP0: 0.02% maker, 0.04% taker
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
    verbose_ticks: bool = True  # emit per-tick decision diagnostics

    # Binance contract precision is discovered via exchangeInfo at runtime,
    # these are conservative defaults for DOGEUSDT.
    price_precision: int = 5
    quantity_precision: int = 0
    min_notional: float = 5.0

    trading_hours_utc: Tuple[int, int] = (0, 24)


CONFIG = BotConfig()
