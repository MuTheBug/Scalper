"""Signal generation: fuses the PDF's 9-step confluence logic.

A trade is only authorized when, in this order:
  Step 3 — Macro trend on the 15m chart agrees with the intended side.
  Step 5 — Lorentzian Classification fires in the same direction.
  Step 6 — Stochastic RSI crosses out of oversold/overbought, EWO agrees,
           and current volume > 20-MA * confirmation_multiplier.
  Step 4 — Order-flow check (walls/absorption) either confirms or, at
           minimum, does not directly contradict the setup.
"""

from __future__ import annotations

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
            return 0
        if last_close > last_ema:
            return 1
        if last_close < last_ema:
            return -1
        return 0

    def lorentzian(self, execution_tf: pd.DataFrame) -> int:
        series = lorentzian_classify(
            execution_tf,
            neighbors=self.config.lorentzian_neighbors,
            lookback=min(self.config.lorentzian_lookback, max(100, len(execution_tf) - 50)),
            threshold=self.config.lorentzian_threshold,
        )
        return int(series.iloc[-1])

    def confluence(self, df: pd.DataFrame, side: str) -> bool:
        cfg = self.config
        stoch = stochastic_rsi(
            df["close"], rsi_period=cfg.stoch_rsi_period,
            stoch_period=cfg.stoch_rsi_period, k=cfg.stoch_rsi_k, d=cfg.stoch_rsi_d,
        )
        ewo = elder_weight_oscillator(df["close"], cfg.ewo_fast, cfg.ewo_slow)
        vol_ma = df["volume"].rolling(cfg.volume_ma_period).mean()

        k_now, k_prev = stoch["k"].iloc[-1], stoch["k"].iloc[-2]
        d_now = stoch["d"].iloc[-1]
        ewo_now = ewo.iloc[-1]
        vol_now = df["volume"].iloc[-1]
        vol_ref = vol_ma.iloc[-1]

        volume_ok = bool(vol_ref) and vol_now >= vol_ref * cfg.volume_confirmation_multiplier

        if side == "long":
            stoch_cross_up = k_prev < cfg.stoch_rsi_oversold and k_now > d_now
            return stoch_cross_up and ewo_now > 0 and volume_ok
        # short
        stoch_cross_down = k_prev > cfg.stoch_rsi_overbought and k_now < d_now
        return stoch_cross_down and ewo_now < 0 and volume_ok

    def order_flow_ok(
        self, snapshot: OrderBookSnapshot, trades, side: str
    ) -> bool:
        cfg = self.config
        walls = find_walls(snapshot, cfg.wall_detection_multiplier)
        absorption = detect_absorption(trades, cfg.absorption_delta_threshold)

        # Reject if a large wall stands immediately in the trade's path.
        support, resistance = nearest_support_resistance(snapshot, walls)
        mid = snapshot.mid
        if side == "long" and resistance is not None and (resistance - mid) / mid < 0.0015:
            return False
        if side == "short" and support is not None and (mid - support) / mid < 0.0015:
            return False

        # Absorption, when present, must agree with the intended side.
        if absorption == "bearish" and side == "long":
            return False
        if absorption == "bullish" and side == "short":
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
            return None

        lor = self.lorentzian(execution_df)
        if lor == 0 or lor != bias:
            return None

        side = "long" if bias == 1 else "short"
        if not self.confluence(execution_df, side):
            return None
        if not self.order_flow_ok(snapshot, trades, side):
            return None

        cfg = self.config
        atr_series = atr(execution_df, cfg.atr_period)
        last_atr = float(atr_series.iloc[-1])
        entry = snapshot.best_ask if side == "long" else snapshot.best_bid
        stop_distance = cfg.atr_stop_multiplier * last_atr
        stop = entry - stop_distance if side == "long" else entry + stop_distance

        walls = find_walls(snapshot, cfg.wall_detection_multiplier)
        support, resistance = nearest_support_resistance(snapshot, walls)

        # Prefer the structural invalidation point when it is tighter than ATR.
        if side == "long" and support is not None and support < entry:
            structural_stop = support - cfg.atr_stop_multiplier * last_atr * 0.25
            if structural_stop > stop:
                stop = structural_stop
        elif side == "short" and resistance is not None and resistance > entry:
            structural_stop = resistance + cfg.atr_stop_multiplier * last_atr * 0.25
            if structural_stop < stop:
                stop = structural_stop

        return Signal(
            side=side,
            entry=float(entry),
            atr=last_atr,
            stop=float(stop),
            support=support,
            resistance=resistance,
            reason=(
                f"bias={bias} lorentzian={lor} stoch+EWO+vol confluence, "
                f"ATR={last_atr:.6f}"
            ),
        )


def compute_psar_trail(df: pd.DataFrame, step: float, max_af: float) -> float:
    """Return the last PSAR value — used to trail the TP2 runner (Step 9)."""
    return float(parabolic_sar(df, step, max_af).iloc[-1])
