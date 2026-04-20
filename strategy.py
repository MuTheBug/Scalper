import pandas as pd
import numpy as np
from indicators import calculate_macro_trend, calculate_scalp_indicators, get_lorentzian_signal
from config import SYMBOL, TIMEFRAME_MACRO, TIMEFRAME_SCALP, ATR_MULTIPLIER, RISK_PERCENT, TP1_PERCENT, RR_RATIO

class ScalpingStrategy:
    def __init__(self, exchange_client):
        self.exchange = exchange_client

    def check_macro_trend(self):
        """Step 3: Establish the Macro-Context and Dominant Trend Filter."""
        df_macro = self.exchange.get_klines(SYMBOL, TIMEFRAME_MACRO, limit=200)
        if df_macro is None: return None
        
        df_macro = calculate_macro_trend(df_macro)
        last_close = df_macro['close'].iloc[-1]
        last_ema = df_macro['ema_macro'].iloc[-1]
        
        if last_close > last_ema:
            return 1 # Bullish
        elif last_close < last_ema:
            return -1 # Bearish
        return 0 # Neutral

    def get_signal(self):
        """Combine all signals for decision making."""
        macro_trend = self.check_macro_trend()
        
        # Step 4: Analyze Real-Time klines on scalp timeframe
        df_scalp = self.exchange.get_klines(SYMBOL, TIMEFRAME_SCALP, limit=1000)
        if df_scalp is None: return None
        
        df_scalp = calculate_scalp_indicators(df_scalp)
        
        # Current data point
        last_data = df_scalp.iloc[-1]
        
        # Lorentzian signal (Step 5)
        # Assuming prediction column: 1 for Buy, -1 for Sell
        prediction = get_lorentzian_signal(df_scalp)
        
        # Step 6: Multi-Indicator Confluence
        is_long_confluence = (
            macro_trend == 1 and
            prediction == 1 and
            last_data['stoch_rsi_k'] < 0.30 and # Oversold (ta library scale 0-1)
            last_data['ewo'] > 0 and # Positive momentum
            last_data['volume'] > last_data['vol_ma'] # Volume spike
        )
        
        is_short_confluence = (
            macro_trend == -1 and
            prediction == -1 and
            last_data['stoch_rsi_k'] > 0.70 and # Overbought (ta library scale 0-1)
            last_data['ewo'] < 0 and # Negative momentum
            last_data['volume'] > last_data['vol_ma'] # Volume spike
        )
        
        if is_long_confluence:
            return 'LONG', last_data
        elif is_short_confluence:
            return 'SHORT', last_data
        
        return None, None

    def calculate_position_size(self, balance, price, atr):
        """Step 7: Calculate Dynamic Risk Constraints and Position Sizing."""
        # SL distance in price
        sl_distance = atr * ATR_MULTIPLIER
        
        # Max loss in USDT
        max_loss_usdt = balance * RISK_PERCENT
        
        # Quantity = Max Loss / SL Distance
        # quantity = max_loss_usdt / sl_distance
        # For Futures, need to consider leverage and precision
        
        return max_loss_usdt / sl_distance

    def get_tp_sl_levels(self, side, price, atr):
        """Calculate TP and SL levels."""
        sl_distance = atr * ATR_MULTIPLIER
        
        if side == 'LONG':
            sl_price = price - sl_distance
            tp1_price = price + (sl_distance * RR_RATIO)
        else:
            sl_price = price + sl_distance
            tp1_price = price - (sl_distance * RR_RATIO)
            
        return sl_price, tp1_price
