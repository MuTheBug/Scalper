"""Lightweight backtest harness.

Re-runs the full PDF confluence (trend filter, Lorentzian, Stoch RSI,
EWO, volume) against historical klines and simulates the bifurcated
TP1/TP2 exit. Order-book and absorption checks are skipped in backtest
because historical L2 data isn't available from the REST API — this
makes the backtest conservative relative to live performance.

Usage:
    python -m scalper.backtest            # uses defaults from CONFIG
    python -m scalper.backtest --bars 5000
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from typing import List

import pandas as pd
from dotenv import load_dotenv

from .config import CONFIG
from .execution.binance_client import BinanceFuturesClient
from .risk.manager import RiskManager
from .strategy.indicators import (
    atr,
    elder_weight_oscillator,
    ema,
    lorentzian_classify,
    parabolic_sar,
    stochastic_rsi,
)


logger = logging.getLogger("scalper.backtest")


@dataclass
class Trade:
    side: str
    entry_idx: int
    entry: float
    stop: float
    tp1: float
    exit_idx: int = -1
    exit_price: float = 0.0
    pnl_r: float = 0.0  # realized PnL in R-multiples
    outcome: str = ""


def run(bars: int = 3000) -> List[Trade]:
    cfg = CONFIG
    load_dotenv()
    client = BinanceFuturesClient(
        api_key=os.environ.get("BINANCE_API_KEY", ""),
        api_secret=os.environ.get("BINANCE_API_SECRET", ""),
        testnet=True,
        symbol=cfg.symbol,
    )
    exec_df = client.klines(cfg.execution_interval, limit=min(bars, 1500))
    trend_df = client.klines(cfg.trend_interval, limit=min(bars // 15 + 100, 1500))

    # Precompute series
    trend_ema = ema(trend_df["close"], cfg.trend_ema_period)
    stoch = stochastic_rsi(
        exec_df["close"], rsi_period=cfg.stoch_rsi_period,
        stoch_period=cfg.stoch_rsi_period, k=cfg.stoch_rsi_k, d=cfg.stoch_rsi_d,
    )
    ewo = elder_weight_oscillator(exec_df["close"], cfg.ewo_fast, cfg.ewo_slow)
    vol_ma = exec_df["volume"].rolling(cfg.volume_ma_period).mean()
    atr_series = atr(exec_df, cfg.atr_period)
    psar = parabolic_sar(exec_df, cfg.psar_step, cfg.psar_max)
    lor = lorentzian_classify(
        exec_df,
        neighbors=cfg.lorentzian_neighbors,
        lookback=min(cfg.lorentzian_lookback, len(exec_df) - 50),
        threshold=cfg.lorentzian_threshold,
    )

    risk = RiskManager(cfg)
    equity = 1000.0
    trades: List[Trade] = []
    open_trade: Trade | None = None
    tp1_filled = False
    trail_stop = 0.0
    r_unit = 0.0
    runner_fraction = 1.0 - cfg.tp1_fraction

    # Reindex trend_df to exec_df by as-of merge
    trend_aligned = trend_ema.reindex(exec_df.index, method="ffill")

    for i in range(max(cfg.lorentzian_lookback, cfg.atr_period + 5), len(exec_df)):
        row = exec_df.iloc[i]
        price = float(row["close"])

        if open_trade is not None:
            # Manage open trade on this bar's high/low
            high = float(row["high"])
            low = float(row["low"])
            plan_side = open_trade.side

            # Check stop first (conservative)
            stop = trail_stop if tp1_filled else open_trade.stop
            if plan_side == "long" and low <= stop:
                exit_price = stop
                partial_r = cfg.tp1_fraction * cfg.tp1_rr if tp1_filled else 0.0
                runner_r = runner_fraction * (exit_price - open_trade.entry) / r_unit if tp1_filled else -1.0
                open_trade.pnl_r = partial_r + runner_r
                open_trade.exit_idx = i
                open_trade.exit_price = exit_price
                open_trade.outcome = "stop" if not tp1_filled else "trail"
                trades.append(open_trade)
                open_trade = None
                tp1_filled = False
                continue
            if plan_side == "short" and high >= stop:
                exit_price = stop
                partial_r = cfg.tp1_fraction * cfg.tp1_rr if tp1_filled else 0.0
                runner_r = runner_fraction * (open_trade.entry - exit_price) / r_unit if tp1_filled else -1.0
                open_trade.pnl_r = partial_r + runner_r
                open_trade.exit_idx = i
                open_trade.exit_price = exit_price
                open_trade.outcome = "stop" if not tp1_filled else "trail"
                trades.append(open_trade)
                open_trade = None
                tp1_filled = False
                continue

            if not tp1_filled:
                if plan_side == "long" and high >= open_trade.tp1:
                    tp1_filled = True
                    trail_stop = open_trade.entry
                elif plan_side == "short" and low <= open_trade.tp1:
                    tp1_filled = True
                    trail_stop = open_trade.entry

            if tp1_filled:
                new_psar = float(psar.iloc[i])
                if plan_side == "long" and new_psar > trail_stop:
                    trail_stop = new_psar
                elif plan_side == "short" and new_psar < trail_stop:
                    trail_stop = new_psar
            continue

        # Look for a new entry
        trend = trend_aligned.iloc[i]
        if pd.isna(trend):
            continue
        bias = 1 if price > trend else -1 if price < trend else 0
        if bias == 0:
            continue

        lor_sig = int(lor.iloc[i])
        if lor_sig == 0 or lor_sig != bias:
            continue

        k_now, k_prev = stoch["k"].iloc[i], stoch["k"].iloc[i - 1]
        d_now = stoch["d"].iloc[i]
        ewo_now = ewo.iloc[i]
        vol_now = row["volume"]
        vol_ref = vol_ma.iloc[i]
        if pd.isna(vol_ref):
            continue
        volume_ok = vol_now >= vol_ref * cfg.volume_confirmation_multiplier
        if not volume_ok:
            continue

        side = "long" if bias == 1 else "short"
        if side == "long" and not (k_prev < cfg.stoch_rsi_oversold and k_now > d_now and ewo_now > 0):
            continue
        if side == "short" and not (k_prev > cfg.stoch_rsi_overbought and k_now < d_now and ewo_now < 0):
            continue

        last_atr = float(atr_series.iloc[i])
        if last_atr <= 0:
            continue
        stop_distance = cfg.atr_stop_multiplier * last_atr
        entry = price
        stop = entry - stop_distance if side == "long" else entry + stop_distance
        tp1 = entry + stop_distance * cfg.tp1_rr if side == "long" else entry - stop_distance * cfg.tp1_rr

        r_unit = stop_distance
        open_trade = Trade(side=side, entry_idx=i, entry=entry, stop=stop, tp1=tp1)
        tp1_filled = False
        trail_stop = stop

    _report(trades, equity)
    return trades


def _report(trades: List[Trade], start_equity: float) -> None:
    if not trades:
        logger.warning("No trades generated in backtest window")
        return
    wins = sum(1 for t in trades if t.pnl_r > 0)
    losses = sum(1 for t in trades if t.pnl_r <= 0)
    win_rate = wins / len(trades) * 100
    avg_r = sum(t.pnl_r for t in trades) / len(trades)
    total_r = sum(t.pnl_r for t in trades)
    logger.info(
        "Trades=%d wins=%d losses=%d win_rate=%.1f%% avg_R=%.3f total_R=%.2f",
        len(trades), wins, losses, win_rate, avg_r, total_r,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=1500)
    args = parser.parse_args()
    run(bars=args.bars)


if __name__ == "__main__":
    main()
