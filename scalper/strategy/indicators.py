"""Technical indicators used by the scalping strategy.

Each function operates on pandas.Series / DataFrame objects and returns
Series so they can be composed freely. Implementations are kept small and
dependency-free (numpy/pandas) so the bot can run without TA-Lib.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Classical indicators
# ---------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def stochastic_rsi(
    series: pd.Series,
    rsi_period: int = 14,
    stoch_period: int = 14,
    k: int = 3,
    d: int = 3,
) -> pd.DataFrame:
    r = rsi(series, rsi_period)
    lowest = r.rolling(stoch_period).min()
    highest = r.rolling(stoch_period).max()
    stoch = 100 * (r - lowest) / (highest - lowest).replace(0.0, np.nan)
    k_line = stoch.rolling(k).mean()
    d_line = k_line.rolling(d).mean()
    return pd.DataFrame({"k": k_line, "d": d_line}).fillna(50.0)


def elder_weight_oscillator(series: pd.Series, fast: int = 16, slow: int = 26) -> pd.Series:
    """EWO = (SMA_fast - SMA_slow) / close * 100."""
    return (sma(series, fast) - sma(series, slow)) / series * 100.0


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def cci(df: pd.DataFrame, period: int = 20) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    return ((tp - ma) / (0.015 * md.replace(0.0, np.nan))).fillna(0.0)


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


def parabolic_sar(df: pd.DataFrame, step: float = 0.02, max_af: float = 0.2) -> pd.Series:
    """Wilder's Parabolic SAR. Used as the runner's trailing stop (Step 9)."""
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    length = len(df)
    sar = np.zeros(length)
    if length < 2:
        return pd.Series(sar, index=df.index)

    bull = True
    af = step
    ep = high[0]
    sar[0] = low[0]

    for i in range(1, length):
        prev_sar = sar[i - 1]
        if bull:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low[i - 1], low[max(i - 2, 0)])
            if low[i] < sar[i]:
                bull = False
                sar[i] = ep
                ep = low[i]
                af = step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + step, max_af)
        else:
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high[i - 1], high[max(i - 2, 0)])
            if high[i] > sar[i]:
                bull = True
                sar[i] = ep
                ep = high[i]
                af = step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + step, max_af)
    return pd.Series(sar, index=df.index)


# ---------------------------------------------------------------------------
# Lorentzian Classification (k-NN with time-warped distance)
# ---------------------------------------------------------------------------

def _normalize(series: pd.Series) -> pd.Series:
    lo, hi = series.min(), series.max()
    if hi - lo == 0 or np.isnan(hi - lo):
        return pd.Series(np.zeros(len(series)), index=series.index)
    return (series - lo) / (hi - lo)


def lorentzian_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer the RSI/CCI/ADX/volume feature set described in the PDF."""
    features = pd.DataFrame(index=df.index)
    features["rsi14"] = _normalize(rsi(df["close"], 14))
    features["rsi9"] = _normalize(rsi(df["close"], 9))
    features["cci"] = _normalize(cci(df, 20))
    features["adx"] = _normalize(adx(df, 14))
    features["vol"] = _normalize(df["volume"])
    return features.fillna(0.5)


def lorentzian_classify(
    df: pd.DataFrame,
    neighbors: int = 8,
    lookback: int = 2000,
    threshold: int = 5,
    forward: int = 4,
) -> pd.Series:
    """Return +1 (long bias), -1 (short bias), or 0 (neutral) per bar.

    Uses the Lorentzian distance d(x,y) = sum(log(1 + |x_i - y_i|)) which,
    per the PDF, introduces a temporal warping that outperforms Euclidean
    distance in high-volatility regimes like DOGE.
    """
    feats = lorentzian_features(df).to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    labels = np.where(close[forward:] > close[:-forward] * 1.001, 1, 0)
    labels = np.where(close[forward:] < close[:-forward] * 0.999, -1, labels)
    labels = np.concatenate([labels, np.zeros(forward, dtype=int)])

    signals = np.zeros(n, dtype=int)
    for i in range(lookback, n):
        start = max(0, i - lookback)
        history = feats[start:i]
        history_labels = labels[start:i]
        current = feats[i]
        distances = np.log1p(np.abs(history - current)).sum(axis=1)

        # Subsample every 4th bar to reduce temporal autocorrelation
        mask = np.arange(len(distances)) % 4 == 0
        distances = distances[mask]
        history_labels = history_labels[mask]
        if len(distances) < neighbors:
            continue

        idx = np.argpartition(distances, neighbors)[:neighbors]
        score = history_labels[idx].sum()
        if score >= threshold:
            signals[i] = 1
        elif score <= -threshold:
            signals[i] = -1
    return pd.Series(signals, index=df.index)
