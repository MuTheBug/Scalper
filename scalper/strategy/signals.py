"""Signal generation: fuses the PDF's 9-step confluence logic.

A trade is authorized when, in this order:
  Step 3 — Macro trend on the 15m chart agrees with the intended side.
  Step 5 — Lorentzian Classification fires in the same direction.
  Step 6 — Stochastic RSI crossed out of oversold/overbought within the
           last `stoch_cross_lookback` bars, EWO agrees, and volume
           clears `volume_confirmation_multiplier * 20-MA`.
  Step 4 — Order-flow check (skipped in backtest; see order_flow.py).

In addition, two alternate entry patterns are available when the
strict confluence misses:
  * Pullback — price retraces to a short-term EMA in the direction of
    the macro trend and the Stoch RSI turns back up.
  * Breakout — close of the current bar breaches the prior-window
    high/low with volume expansion.

Every gate emits a DEBUG log so you can see exactly why a tick traded
or didn't.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..config import BotConfig
from .indicators import (
    adx,
    atr,
    elder_weight_oscillator,
    ema,
    lorentzian_classify,
    parabolic_sar,
    rsi,
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
        last_close = float(higher_tf["close"].iloc[-1])
        last_ema = trend_ema.iloc[-1]
        if pd.isna(last_ema):
            logger.debug("Trend bias: EMA still warming up (NaN)")
            return 0
        last_ema = float(last_ema)
        bias = 1 if last_close > last_ema else -1 if last_close < last_ema else 0
        logger.debug(
            "Step 3 — Trend bias: close=%.6f ema%d=%.6f -> %s",
            last_close, self.config.trend_ema_period, last_ema,
            {1: "LONG_ONLY", -1: "SHORT_ONLY", 0: "FLAT"}[bias],
        )
        return bias

    def _auto_lookback(self, series_len: int) -> int:
        cfg = self.config
        capped = min(cfg.lorentzian_lookback, max(50, int(series_len * cfg.lorentzian_lookback_min_fraction)))
        return max(50, capped)

    def lorentzian(self, execution_tf: pd.DataFrame) -> int:
        lookback = self._auto_lookback(len(execution_tf))
        series = lorentzian_classify(
            execution_tf,
            neighbors=self.config.lorentzian_neighbors,
            lookback=lookback,
            threshold=self.config.lorentzian_threshold,
        )
        signal = int(series.iloc[-1])
        logger.debug(
            "Step 5 — Lorentzian: signal=%+d (neighbors=%d threshold=%d lookback=%d)",
            signal, self.config.lorentzian_neighbors, self.config.lorentzian_threshold, lookback,
        )
        return signal

    def _confluence_values(self, df: pd.DataFrame):
        cfg = self.config
        stoch = stochastic_rsi(
            df["close"], rsi_period=cfg.stoch_rsi_period,
            stoch_period=cfg.stoch_rsi_period, k=cfg.stoch_rsi_k, d=cfg.stoch_rsi_d,
        )
        ewo = elder_weight_oscillator(df["close"], cfg.ewo_fast, cfg.ewo_slow)
        vol_ma = df["volume"].rolling(cfg.volume_ma_period).mean()
        return stoch, ewo, vol_ma

    def confluence(self, df: pd.DataFrame, side: str) -> bool:
        cfg = self.config
        stoch, ewo, vol_ma = self._confluence_values(df)
        window = max(2, cfg.stoch_cross_lookback)

        k_series = stoch["k"].tail(window + 1)
        d_now = float(stoch["d"].iloc[-1])
        k_now = float(k_series.iloc[-1])
        ewo_now = float(ewo.iloc[-1])
        vol_now = float(df["volume"].iloc[-1])
        vol_ref = float(vol_ma.iloc[-1]) if not pd.isna(vol_ma.iloc[-1]) else 0.0

        volume_ok = vol_ref > 0 and vol_now >= vol_ref * cfg.volume_confirmation_multiplier

        logger.debug(
            "Step 6 — Confluence (side=%s): K=%.2f D=%.2f EWO=%.4f vol=%.2f/MA=%.2f x%.2f vol_ok=%s window=%d",
            side, k_now, d_now, ewo_now, vol_now, vol_ref,
            cfg.volume_confirmation_multiplier, volume_ok, window,
        )

        if side == "long":
            recent_oversold = (k_series.iloc[:-1] < cfg.stoch_rsi_oversold).any()
            k_turning_up = k_now > d_now or k_now > float(k_series.iloc[-2])
            passed = bool(recent_oversold) and bool(k_turning_up) and ewo_now > 0 and volume_ok
            logger.debug(
                "  long gate: recent_oversold=%s k_turning_up=%s ewo>0=%s vol_ok=%s -> %s",
                recent_oversold, k_turning_up, ewo_now > 0, volume_ok,
                "PASS" if passed else "FAIL",
            )
            return passed

        recent_overbought = (k_series.iloc[:-1] > cfg.stoch_rsi_overbought).any()
        k_turning_down = k_now < d_now or k_now < float(k_series.iloc[-2])
        passed = bool(recent_overbought) and bool(k_turning_down) and ewo_now < 0 and volume_ok
        logger.debug(
            "  short gate: recent_overbought=%s k_turning_down=%s ewo<0=%s vol_ok=%s -> %s",
            recent_overbought, k_turning_down, ewo_now < 0, volume_ok,
            "PASS" if passed else "FAIL",
        )
        return passed

    # -- quality filters ----------------------------------------------------

    def _trend_strength_ok(self, df: pd.DataFrame) -> bool:
        cfg = self.config
        if cfg.min_adx <= 0:
            return True
        adx_value = float(adx(df, cfg.atr_period).iloc[-1])
        ok = adx_value >= cfg.min_adx
        logger.debug("Trend strength: ADX=%.2f >= %.2f -> %s", adx_value, cfg.min_adx, ok)
        return ok

    def _ewo_magnitude_ok(self, df: pd.DataFrame, side: str) -> bool:
        cfg = self.config
        if cfg.min_ewo_magnitude <= 0:
            return True
        ewo_value = float(elder_weight_oscillator(df["close"], cfg.ewo_fast, cfg.ewo_slow).iloc[-1])
        if side == "long":
            ok = ewo_value >= cfg.min_ewo_magnitude
        else:
            ok = ewo_value <= -cfg.min_ewo_magnitude
        logger.debug("EWO magnitude: %.4f side=%s threshold=%.4f -> %s",
                     ewo_value, side, cfg.min_ewo_magnitude, ok)
        return ok

    # -- alternate entry patterns -------------------------------------------

    def pullback_entry(self, df: pd.DataFrame, side: str) -> bool:
        """High-quality pullback: real dip into trend EMA + RSI confirmation + bounce volume."""
        cfg = self.config
        if not cfg.enable_pullback_entries:
            return False

        if not self._trend_strength_ok(df):
            logger.debug("Pullback rejected: trend too weak")
            return False
        if not self._ewo_magnitude_ok(df, side):
            logger.debug("Pullback rejected: EWO magnitude insufficient")
            return False

        ema_short = ema(df["close"], cfg.pullback_ema_period)
        last_close = float(df["close"].iloc[-1])
        last_ema = float(ema_short.iloc[-1])
        if last_ema == 0:
            return False

        # Must have ACTUALLY pulled back: prior N bars touched the EMA from the trend side
        lookback = cfg.pullback_dip_bars
        recent = df.iloc[-lookback - 1:-1]
        recent_low = float(recent["low"].min())
        recent_high = float(recent["high"].max())
        if side == "long":
            actually_dipped = recent_low <= last_ema * (1 + cfg.pullback_touch_tolerance)
        else:
            actually_dipped = recent_high >= last_ema * (1 - cfg.pullback_touch_tolerance)
        if not actually_dipped:
            logger.debug("Pullback rejected: no real dip in last %d bars", lookback)
            return False

        # RSI must be in the pullback zone (not in extreme territory either way)
        rsi_value = float(rsi(df["close"], 14).iloc[-1])
        if side == "long":
            rsi_ok = cfg.pullback_rsi_min_long <= rsi_value <= cfg.pullback_rsi_max_long
        else:
            rsi_ok = cfg.pullback_rsi_min_short <= rsi_value <= cfg.pullback_rsi_max_short
        if not rsi_ok:
            logger.debug("Pullback rejected: RSI=%.1f out of zone", rsi_value)
            return False

        # Confirm the bounce: current bar moved in trend direction with above-average volume
        prev_close = float(df["close"].iloc[-2])
        vol_now = float(df["volume"].iloc[-1])
        vol_ref = float(df["volume"].rolling(cfg.volume_ma_period).mean().iloc[-1])
        vol_ok = vol_ref > 0 and vol_now >= vol_ref * cfg.volume_confirmation_multiplier
        if side == "long":
            bouncing = last_close > prev_close and last_close > last_ema
        else:
            bouncing = last_close < prev_close and last_close < last_ema

        passed = bouncing and vol_ok
        logger.debug(
            "Pullback probe (%s): close=%.6f ema%d=%.6f rsi=%.1f bouncing=%s vol_ok=%s -> %s",
            side, last_close, cfg.pullback_ema_period, last_ema, rsi_value, bouncing, vol_ok,
            "PASS" if passed else "FAIL",
        )
        return passed

    def breakout_entry(self, df: pd.DataFrame, side: str) -> bool:
        """Volatility breakout: clean breach of N-bar high/low + ATR expansion + body-dominant bar."""
        cfg = self.config
        if not cfg.enable_breakout_entries:
            return False
        if not self._trend_strength_ok(df):
            return False
        if not self._ewo_magnitude_ok(df, side):
            return False

        window = cfg.breakout_window
        if len(df) < window + cfg.atr_period + 5:
            return False

        recent = df.iloc[-window - 1:-1]
        last = df.iloc[-1]
        last_close = float(last["close"])
        last_open = float(last["open"])
        last_high = float(last["high"])
        last_low = float(last["low"])

        if side == "long":
            breach = last_close > float(recent["high"].max())
        else:
            breach = last_close < float(recent["low"].min())
        if not breach:
            return False

        # Volume expansion
        vol_now = float(last["volume"])
        vol_ref = float(df["volume"].rolling(cfg.volume_ma_period).mean().iloc[-1])
        vol_ok = vol_ref > 0 and vol_now >= vol_ref * cfg.breakout_volume_multiplier

        # ATR expansion: current ATR > moving average of ATR (volatility regime change)
        atr_series = atr(df, cfg.atr_period)
        atr_now = float(atr_series.iloc[-1])
        atr_ma = float(atr_series.rolling(cfg.atr_period).mean().iloc[-1])
        atr_ok = atr_ma > 0 and atr_now >= atr_ma * cfg.breakout_atr_expansion

        # Body must dominate the bar (not a wick)
        bar_range = max(1e-12, last_high - last_low)
        body = abs(last_close - last_open) / bar_range
        body_ok = body >= cfg.breakout_min_body_ratio
        body_in_direction = (last_close > last_open) if side == "long" else (last_close < last_open)

        passed = vol_ok and atr_ok and body_ok and body_in_direction
        logger.debug(
            "Breakout probe (%s): breach=%s vol_ok=%s atr_ok=%s body=%.2f dir_ok=%s -> %s",
            side, breach, vol_ok, atr_ok, body, body_in_direction,
            "PASS" if passed else "FAIL",
        )
        return passed

    # -- order flow ----------------------------------------------------------

    def order_flow_ok(
        self, snapshot: Optional[OrderBookSnapshot], trades, side: str
    ) -> bool:
        if snapshot is None:
            return True
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
        if side == "long" and resistance is not None and (resistance - mid) / mid < 0.0015:
            logger.debug("  reject: long blocked by sell wall within 0.15%% of mid")
            return False
        if side == "short" and support is not None and (mid - support) / mid < 0.0015:
            logger.debug("  reject: short blocked by buy wall within 0.15%% of mid")
            return False
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
        snapshot: Optional[OrderBookSnapshot],
        trades,
    ) -> Optional[Signal]:
        bias = self.trend_bias(trend_df)
        if bias == 0:
            return None

        lor = self.lorentzian(execution_df)
        side = "long" if bias == 1 else "short"

        # Apply quality filters once for all paths (cheap to recompute, clearer logs).
        quality_ok = self._trend_strength_ok(execution_df) and self._ewo_magnitude_ok(execution_df, side)
        if not quality_ok:
            logger.debug("Quality filters failed — no entry")
            return None

        # Primary path: PDF confluence backed by Lorentzian agreement.
        primary = lor == bias and self.confluence(execution_df, side)
        alt_pullback = lor != -bias and self.pullback_entry(execution_df, side)
        alt_breakout = lor != -bias and self.breakout_entry(execution_df, side)

        if not (primary or alt_pullback or alt_breakout):
            logger.debug(
                "No entry pattern: primary=%s pullback=%s breakout=%s",
                primary, alt_pullback, alt_breakout,
            )
            return None

        if not self.order_flow_ok(snapshot, trades, side):
            return None

        reason_bits = []
        if primary: reason_bits.append("primary-confluence")
        if alt_pullback: reason_bits.append("pullback")
        if alt_breakout: reason_bits.append("breakout")

        cfg = self.config
        atr_series = atr(execution_df, cfg.atr_period)
        last_atr = float(atr_series.iloc[-1])
        if snapshot is not None:
            entry = snapshot.best_ask if side == "long" else snapshot.best_bid
        else:
            entry = float(execution_df["close"].iloc[-1])
        stop_distance = cfg.atr_stop_multiplier * last_atr
        stop = entry - stop_distance if side == "long" else entry + stop_distance

        support = resistance = None
        if snapshot is not None:
            walls = find_walls(snapshot, cfg.wall_detection_multiplier)
            support, resistance = nearest_support_resistance(snapshot, walls)
            if side == "long" and support is not None and support < entry:
                structural_stop = support - cfg.atr_stop_multiplier * last_atr * 0.25
                if structural_stop > stop:
                    stop = structural_stop
            elif side == "short" and resistance is not None and resistance > entry:
                structural_stop = resistance + cfg.atr_stop_multiplier * last_atr * 0.25
                if structural_stop < stop:
                    stop = structural_stop

        sig = Signal(
            side=side,
            entry=float(entry),
            atr=last_atr,
            stop=float(stop),
            support=support,
            resistance=resistance,
            reason=(
                f"bias={bias} lorentzian={lor} patterns=[{','.join(reason_bits)}] ATR={last_atr:.6f}"
            ),
        )
        logger.info(
            "SIGNAL %s entry=%.6f stop=%.6f ATR=%.6f reason=[%s]",
            side.upper(), sig.entry, sig.stop, sig.atr, sig.reason,
        )
        return sig


def compute_psar_trail(df: pd.DataFrame, step: float, max_af: float) -> float:
    return float(parabolic_sar(df, step, max_af).iloc[-1])
