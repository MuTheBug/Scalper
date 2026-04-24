"""Signal generation: fuses the PDF's 9-step confluence logic.

A trade is only authorized when, in this order:
  Step 3 — Macro trend on the 15m chart agrees with the intended side.
  Step 5 — Lorentzian Classification fires in the same direction.
  Step 6 — Stochastic RSI crosses out of oversold/overbought, EWO agrees,
           and current volume > 20-MA * confirmation_multiplier.
  Step 4 — Order-flow check (walls/absorption) either confirms or, at
           minimum, does not directly contradict the setup.

Every gate emits a DEBUG log so you can see exactly why a tick was or
wasn't traded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..config import BotConfig
from .indicators import (
    atr,
    elder_weight_oscillator,
    ema,
    lorentzian_classify,
    parabolic_sar,
    stochastic_rsi,
)
from .order_flow import OrderBookSnapshot, detect_absorption, find_walls, nearest_support_resistance


logger = logging.getLogger(__name__)


@dataclass
class Signal:
    side: str  # "long" or "short"
    entry: float
    atr: float
    stop: float
    support: Optional[float]
    resistance: Optional[float]
    reason: str


class SignalEngine:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    # -- individual gates ----------------------------------------------------

    def trend_bias(self, higher_tf: pd.DataFrame) -> int:
        trend_ema = ema(higher_tf["close"], self.config.trend_ema_period)
        last_close = higher_tf["close"].iloc[-1]
        last_ema = trend_ema.iloc[-1]
        if pd.isna(last_ema):
            logger.debug("Trend bias: EMA still warming up (NaN)")
            return 0
        bias = 1 if last_close > last_ema else -1 if last_close < last_ema else 0
        logger.debug(
            "Step 3 — Trend bias: close=%.6f ema%d=%.6f -> %s",
            last_close, self.config.trend_ema_period, last_ema,
            {1: "LONG_ONLY", -1: "SHORT_ONLY", 0: "FLAT"}[bias],
        )
        return bias

    def lorentzian(self, execution_tf: pd.DataFrame) -> int:
        series = lorentzian_classify(
            execution_tf,
            neighbors=self.config.lorentzian_neighbors,
            lookback=min(self.config.lorentzian_lookback, max(100, len(execution_tf) - 50)),
            threshold=self.config.lorentzian_threshold,
        )
        signal = int(series.iloc[-1])
        logger.debug(
            "Step 5 — Lorentzian: signal=%+d (neighbors=%d threshold=%d)",
            signal, self.config.lorentzian_neighbors, self.config.lorentzian_threshold,
        )
        return signal

    def confluence(self, df: pd.DataFrame, side: str) -> bool:
        cfg = self.config
        stoch = stochastic_rsi(
            df["close"], rsi_period=cfg.stoch_rsi_period,
            stoch_period=cfg.stoch_rsi_period, k=cfg.stoch_rsi_k, d=cfg.stoch_rsi_d,
        )
        ewo = elder_weight_oscillator(df["close"], cfg.ewo_fast, cfg.ewo_slow)
        vol_ma = df["volume"].rolling(cfg.volume_ma_period).mean()

        k_now, k_prev = float(stoch["k"].iloc[-1]), float(stoch["k"].iloc[-2])
        d_now = float(stoch["d"].iloc[-1])
        ewo_now = float(ewo.iloc[-1])
        vol_now = float(df["volume"].iloc[-1])
        vol_ref = float(vol_ma.iloc[-1]) if not pd.isna(vol_ma.iloc[-1]) else 0.0

        volume_ok = vol_ref > 0 and vol_now >= vol_ref * cfg.volume_confirmation_multiplier

        logger.debug(
            "Step 6 — Confluence probe (side=%s): stoch K=%.2f (prev %.2f) D=%.2f EWO=%.4f "
            "vol=%.2f vs MA=%.2f x%.2f -> vol_ok=%s",
            side, k_now, k_prev, d_now, ewo_now, vol_now, vol_ref,
            cfg.volume_confirmation_multiplier, volume_ok,
        )

        if side == "long":
            stoch_cross_up = k_prev < cfg.stoch_rsi_oversold and k_now > d_now
            passed = stoch_cross_up and ewo_now > 0 and volume_ok
            logger.debug(
                "  long gate: stoch_cross_up=%s ewo>0=%s vol_ok=%s -> %s",
                stoch_cross_up, ewo_now > 0, volume_ok, "PASS" if passed else "FAIL",
            )
            return passed

        stoch_cross_down = k_prev > cfg.stoch_rsi_overbought and k_now < d_now
        passed = stoch_cross_down and ewo_now < 0 and volume_ok
        logger.debug(
            "  short gate: stoch_cross_down=%s ewo<0=%s vol_ok=%s -> %s",
            stoch_cross_down, ewo_now < 0, volume_ok, "PASS" if passed else "FAIL",
        )
        return passed

    def order_flow_ok(
        self, snapshot: OrderBookSnapshot, trades, side: str
    ) -> bool:
        cfg = self.config
        walls = find_walls(snapshot, cfg.wall_detection_multiplier)
        absorption = detect_absorption(trades, cfg.absorption_delta_threshold)
        support, resistance = nearest_support_resistance(snapshot, walls)
        mid = snapshot.mid

        logger.debug(
            "Step 4 — Order flow: spread=%.6f mid=%.6f walls=%d support=%s resistance=%s absorption=%s",
            snapshot.spread, mid, len(walls),
            f"{support:.6f}" if support else "—",
            f"{resistance:.6f}" if resistance else "—",
            absorption or "neutral",
        )
        for w in walls[:4]:
            logger.debug("  wall %s @ %.6f size=%.1f (%.1fx mean)", w.side, w.price, w.size, w.ratio)

        if side == "long" and resistance is not None and (resistance - mid) / mid < 0.0015:
            logger.debug("  reject: long blocked by sell wall within 0.15%% of mid")
            return False
        if side == "short" and support is not None and (mid - support) / mid < 0.0015:
            logger.debug("  reject: short blocked by buy wall within 0.15%% of mid")
            return False
        if absorption == "bearish" and side == "long":
            logger.debug("  reject: bearish absorption against long intent")
            return False
        if absorption == "bullish" and side == "short":
            logger.debug("  reject: bullish absorption against short intent")
            return False
        return True

    # -- main entry ----------------------------------------------------------

    def evaluate(
        self,
        execution_df: pd.DataFrame,
        trend_df: pd.DataFrame,
        snapshot: OrderBookSnapshot,
        trades,
    ) -> Optional[Signal]:
        bias = self.trend_bias(trend_df)
        if bias == 0:
            logger.debug("No trend bias — skip")
            return None

        lor = self.lorentzian(execution_df)
        if lor == 0:
            logger.debug("Lorentzian neutral — skip")
            return None
        if lor != bias:
            logger.debug("Lorentzian (%+d) disagrees with trend bias (%+d) — skip", lor, bias)
            return None

        side = "long" if bias == 1 else "short"
        if not self.confluence(execution_df, side):
            logger.debug("Confluence failed for %s — skip", side)
            return None
        if not self.order_flow_ok(snapshot, trades, side):
            logger.debug("Order flow failed for %s — skip", side)
            return None

        cfg = self.config
        atr_series = atr(execution_df, cfg.atr_period)
        last_atr = float(atr_series.iloc[-1])
        entry = snapshot.best_ask if side == "long" else snapshot.best_bid
        stop_distance = cfg.atr_stop_multiplier * last_atr
        stop = entry - stop_distance if side == "long" else entry + stop_distance

        walls = find_walls(snapshot, cfg.wall_detection_multiplier)
        support, resistance = nearest_support_resistance(snapshot, walls)

        if side == "long" and support is not None and support < entry:
            structural_stop = support - cfg.atr_stop_multiplier * last_atr * 0.25
            if structural_stop > stop:
                logger.debug("Tightening long stop from %.6f to structural %.6f", stop, structural_stop)
                stop = structural_stop
        elif side == "short" and resistance is not None and resistance > entry:
            structural_stop = resistance + cfg.atr_stop_multiplier * last_atr * 0.25
            if structural_stop < stop:
                logger.debug("Tightening short stop from %.6f to structural %.6f", stop, structural_stop)
                stop = structural_stop

        sig = Signal(
            side=side,
            entry=float(entry),
            atr=last_atr,
            stop=float(stop),
            support=support,
            resistance=resistance,
            reason=(
                f"bias={bias} lorentzian={lor} stoch+EWO+volume confluence, "
                f"ATR={last_atr:.6f}"
            ),
        )
        logger.info(
            "SIGNAL %s entry=%.6f stop=%.6f ATR=%.6f support=%s resistance=%s reason=[%s]",
            side.upper(), sig.entry, sig.stop, sig.atr,
            f"{support:.6f}" if support else "—",
            f"{resistance:.6f}" if resistance else "—",
            sig.reason,
        )
        return sig


def compute_psar_trail(df: pd.DataFrame, step: float, max_af: float) -> float:
    """Return the last PSAR value — used to trail the TP2 runner (Step 9)."""
    return float(parabolic_sar(df, step, max_af).iloc[-1])
