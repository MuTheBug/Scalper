"""Backtest harness with gate-by-gate instrumentation.

Re-runs the full PDF confluence (trend filter + Lorentzian + Stoch RSI +
EWO + volume) against historical klines and simulates the bifurcated
TP1/TP2 exit. Order-book / absorption checks are skipped because
historical L2 data isn't available from the REST API — this makes the
backtest conservative vs. live.

Usage:
    python -m scalper.backtest                       # balanced profile, 1500 bars
    python -m scalper.backtest --bars 3000 --profile aggressive
    python -m scalper.backtest --profile strict --verbose
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv

from .config import CONFIG, apply_profile
from .execution.binance_client import BinanceFuturesClient
from .risk.manager import RiskManager
from .strategy.indicators import atr, parabolic_sar
from .strategy.signals import SignalEngine


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
    pnl_r: float = 0.0
    outcome: str = ""
    reason: str = ""


@dataclass
class GateCounts:
    ticks_evaluated: int = 0
    trend_flat: int = 0
    lor_neutral: int = 0
    lor_disagree: int = 0
    primary_confluence: int = 0
    pullback: int = 0
    breakout: int = 0
    no_pattern: int = 0
    signals_emitted: int = 0
    trades_opened: int = 0
    trades_skipped_risk: int = 0


def _run_bar_signals(engine: SignalEngine, exec_df: pd.DataFrame,
                     trend_aligned: pd.Series, counts: GateCounts,
                     start_idx: int, end_idx: int) -> List[tuple]:
    """Evaluate signals bar-by-bar using the live SignalEngine.

    Returns a list of (idx, Signal) for every bar that produced a signal.
    """
    signals: List[tuple] = []
    cfg = engine.config

    for i in range(start_idx, end_idx):
        counts.ticks_evaluated += 1
        trend = trend_aligned.iloc[i]
        if pd.isna(trend):
            counts.trend_flat += 1
            continue
        close = float(exec_df["close"].iloc[i])
        bias = 1 if close > trend else -1 if close < trend else 0
        if bias == 0:
            counts.trend_flat += 1
            continue

        # Slice the execution_df up to bar i for gate evaluation
        history = exec_df.iloc[: i + 1]

        lor = engine.lorentzian(history)
        if lor == 0:
            counts.lor_neutral += 1
        if lor != 0 and lor != bias:
            counts.lor_disagree += 1

        side = "long" if bias == 1 else "short"
        primary = lor == bias and engine.confluence(history, side)
        pullback = lor != -bias and engine.pullback_entry(history, side)
        breakout = lor != -bias and engine.breakout_entry(history, side)

        if not (primary or pullback or breakout):
            counts.no_pattern += 1
            continue
        if primary: counts.primary_confluence += 1
        if pullback: counts.pullback += 1
        if breakout: counts.breakout += 1

        last_atr = float(atr(history, cfg.atr_period).iloc[-1])
        if last_atr <= 0 or pd.isna(last_atr):
            continue
        stop_distance = cfg.atr_stop_multiplier * last_atr
        entry = close
        stop = entry - stop_distance if side == "long" else entry + stop_distance
        tp1 = entry + stop_distance * cfg.tp1_rr if side == "long" else entry - stop_distance * cfg.tp1_rr
        reason = []
        if primary: reason.append("primary")
        if pullback: reason.append("pullback")
        if breakout: reason.append("breakout")

        counts.signals_emitted += 1
        signals.append((i, side, entry, stop, tp1, ",".join(reason)))
    return signals


def run(bars: int = 1500, profile: str = "balanced") -> Dict:
    cfg = CONFIG
    apply_profile(cfg, profile)
    load_dotenv()
    client = BinanceFuturesClient(
        api_key=os.environ.get("BINANCE_API_KEY", ""),
        api_secret=os.environ.get("BINANCE_API_SECRET", ""),
        testnet=True,
        symbol=cfg.symbol,
    )

    logger.info("Fetching %d bars of %s klines for %s (profile=%s)",
                bars, cfg.execution_interval, cfg.symbol, profile)
    exec_df = client.klines(cfg.execution_interval, limit=min(bars, 1500))
    trend_df = client.klines(cfg.trend_interval, limit=min(bars // 15 + 200, 1500))

    from .strategy.indicators import ema
    trend_ema = ema(trend_df["close"], cfg.trend_ema_period)
    trend_aligned = trend_ema.reindex(exec_df.index, method="ffill")

    engine = SignalEngine(cfg)
    # Dial signal-engine DEBUG to WARN so the backtest doesn't drown us.
    logging.getLogger("scalper.strategy.signals").setLevel(logging.WARNING)

    counts = GateCounts()
    # Start after indicators have warmed up
    warmup = max(cfg.atr_period + 5, cfg.stoch_rsi_period + 5, cfg.trend_ema_period)
    warmup = max(warmup, int(len(exec_df) * cfg.lorentzian_lookback_min_fraction) + 20)
    start_idx = min(warmup, len(exec_df) - 50)

    logger.info("Warmup=%d, evaluating bars %d..%d (%d bars)",
                warmup, start_idx, len(exec_df), len(exec_df) - start_idx)

    signals = _run_bar_signals(engine, exec_df, trend_aligned, counts,
                               start_idx, len(exec_df) - 1)

    # Simulate trades
    psar_series = parabolic_sar(exec_df, cfg.psar_step, cfg.psar_max)
    risk = RiskManager(cfg)
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None
    tp1_filled = False
    trail_stop = 0.0
    r_unit = 0.0
    runner_fraction = 1.0 - cfg.tp1_fraction

    signal_by_idx = {idx: (side, entry, stop, tp1, reason)
                     for idx, side, entry, stop, tp1, reason in signals}

    for i in range(start_idx, len(exec_df)):
        row = exec_df.iloc[i]
        high, low = float(row["high"]), float(row["low"])

        if open_trade is not None:
            plan_side = open_trade.side
            stop = trail_stop if tp1_filled else open_trade.stop

            hit_stop = (plan_side == "long" and low <= stop) or (plan_side == "short" and high >= stop)
            if hit_stop:
                exit_price = stop
                if tp1_filled:
                    partial_r = cfg.tp1_fraction * cfg.tp1_rr
                    runner_r = runner_fraction * (
                        (exit_price - open_trade.entry) if plan_side == "long"
                        else (open_trade.entry - exit_price)
                    ) / r_unit
                    open_trade.pnl_r = partial_r + runner_r
                    open_trade.outcome = "trail"
                else:
                    open_trade.pnl_r = -1.0
                    open_trade.outcome = "stop"
                open_trade.exit_idx = i
                open_trade.exit_price = exit_price
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
                new_psar = float(psar_series.iloc[i])
                if plan_side == "long" and new_psar > trail_stop:
                    trail_stop = new_psar
                elif plan_side == "short" and new_psar < trail_stop:
                    trail_stop = new_psar
            continue

        # No open trade — look up signal for this bar
        if i in signal_by_idx:
            side, entry, stop, tp1, reason = signal_by_idx[i]
            counts.trades_opened += 1
            open_trade = Trade(side=side, entry_idx=i, entry=entry, stop=stop, tp1=tp1,
                               reason=reason)
            tp1_filled = False
            r_unit = abs(entry - stop)
            trail_stop = stop

    # Force-close any still-open trade at final price
    if open_trade is not None:
        last_price = float(exec_df["close"].iloc[-1])
        delta = (last_price - open_trade.entry) if open_trade.side == "long" else (open_trade.entry - last_price)
        open_trade.pnl_r = delta / r_unit if r_unit > 0 else 0.0
        open_trade.exit_idx = len(exec_df) - 1
        open_trade.exit_price = last_price
        open_trade.outcome = "eod"
        trades.append(open_trade)

    _report(trades, counts, cfg)
    return {"trades": trades, "counts": counts, "config": cfg}


def _report(trades: List[Trade], c: GateCounts, cfg) -> None:
    print()
    print("=" * 72)
    print(f"Backtest report — profile={cfg.strategy_profile}")
    print("=" * 72)
    print("Gate funnel:")
    print(f"  Ticks evaluated        : {c.ticks_evaluated}")
    print(f"  Trend-flat rejections  : {c.trend_flat}")
    print(f"  Lorentzian neutral     : {c.lor_neutral}")
    print(f"  Lorentzian disagreed   : {c.lor_disagree}")
    print(f"  No entry pattern       : {c.no_pattern}")
    print(f"  Primary confluence     : {c.primary_confluence}")
    print(f"  Pullback pattern       : {c.pullback}")
    print(f"  Breakout pattern       : {c.breakout}")
    print(f"  Signals emitted        : {c.signals_emitted}")
    print(f"  Trades opened          : {c.trades_opened}")
    print()

    if not trades:
        print("No trades executed. Try --profile aggressive or increase --bars.")
        return

    wins = sum(1 for t in trades if t.pnl_r > 0)
    losses = len(trades) - wins
    total_r = sum(t.pnl_r for t in trades)
    win_rate = wins / len(trades) * 100
    avg_r = total_r / len(trades)
    avg_win = sum(t.pnl_r for t in trades if t.pnl_r > 0) / max(wins, 1)
    avg_loss = sum(t.pnl_r for t in trades if t.pnl_r <= 0) / max(losses, 1)

    # Max drawdown in R
    equity_r, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        equity_r += t.pnl_r
        peak = max(peak, equity_r)
        max_dd = min(max_dd, equity_r - peak)

    print("Performance:")
    print(f"  Trades                 : {len(trades)} ({wins}W / {losses}L)")
    print(f"  Win rate               : {win_rate:.1f}%")
    print(f"  Average R per trade    : {avg_r:+.3f}")
    print(f"  Average winning trade  : {avg_win:+.3f} R")
    print(f"  Average losing trade   : {avg_loss:+.3f} R")
    print(f"  Cumulative R           : {total_r:+.2f}")
    print(f"  Max drawdown (R)       : {max_dd:.2f}")
    print()
    print("First 10 trades:")
    for t in trades[:10]:
        print(f"  #{t.entry_idx:>5} {t.side:5s} entry={t.entry:.6f} "
              f"stop={t.stop:.6f} tp1={t.tp1:.6f} exit={t.exit_price:.6f} "
              f"pnl={t.pnl_r:+.3f}R [{t.outcome}] {t.reason}")
    if len(trades) > 10:
        print(f"  ... and {len(trades) - 10} more")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=1500)
    parser.add_argument("--profile", choices=["strict", "balanced", "aggressive"], default="balanced")
    parser.add_argument("--verbose", action="store_true", help="Enable per-bar DEBUG logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("scalper.strategy.signals").setLevel(logging.DEBUG)
    run(bars=args.bars, profile=args.profile)


if __name__ == "__main__":
    main()
