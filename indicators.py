import pandas as pd
import ta
from advanced_ta import LorentzianClassification
import numpy as np

def calculate_macro_trend(df, period=100):
    """Calculate EMA for macro trend filtering."""
    df['ema_macro'] = ta.trend.ema_indicator(df['close'], window=period)
    return df

def calculate_scalp_indicators(df):
    """Calculate indicators for scalping logic."""
    # EMA for trend filter on scalp timeframe as well
    df['ema_100'] = ta.trend.ema_indicator(df['close'], window=100)
    
    # RSI
    df['rsi'] = ta.momentum.rsi(df['close'], window=14)
    
    # Stochastic RSI
    # ta library provides stochrsi through momentum
    df['stoch_rsi_k'] = ta.momentum.stochrsi_k(df['close'], window=14, smooth1=3, smooth2=3)
    df['stoch_rsi_d'] = ta.momentum.stochrsi_d(df['close'], window=14, smooth1=3, smooth2=3)
    
    # ATR for Stop Loss
    df['atr'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=14)
    
    # Volume MA
    df['vol_ma'] = ta.trend.sma_indicator(df['volume'], window=20)
    
    # Elder's Weight Oscillator (EWO)
    # EWO = 5-period SMA - 35-period SMA
    df['ewo'] = ta.trend.sma_indicator(df['close'], window=5) - ta.trend.sma_indicator(df['close'], window=35)
    
    # Parabolic SAR for trailing stop
    df['psar'] = ta.trend.psar_up(df['high'], df['low'], df['close']).fillna(
        ta.trend.psar_down(df['high'], df['low'], df['close'])
    )
    
    # Lorentzian Classification
    # Note: advanced-ta expects specific column names in lowercase: open, high, low, close, volume
    # Our dataframe already has these names.
    lc = LorentzianClassification(df)
    df = lc.df
    
    return df

def get_lorentzian_signal(df):
    """
    Extract signal from Lorentzian Classification.
    """
    if 'prediction' in df.columns:
        return df['prediction'].iloc[-1]
    return 0 # Neutral
