import time
import logging
from config import BINANCE_API_KEY, BINANCE_API_SECRET, SYMBOL, TIMEFRAME_SCALP, TP1_PERCENT
from exchange import BinanceFutures
from strategy import ScalpingStrategy
from indicators import calculate_scalp_indicators
from notifications import notify_trade, notify_error, send_telegram_message
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger()

def manage_open_positions(exchange, strategy):
    """Step 9: Manage the Tail Risk and Trail the Asymmetric Runner."""
    try:
        positions = exchange.client.futures_position_information(symbol=SYMBOL)
        for pos in positions:
            amount = float(pos['positionAmt'])
            if amount != 0:
                side = 'LONG' if amount > 0 else 'SHORT'
                entry_price = float(pos['entryPrice'])
                
                # Fetch latest scalp indicators
                df_scalp = exchange.get_klines(SYMBOL, TIMEFRAME_SCALP, limit=100)
                df_scalp = calculate_scalp_indicators(df_scalp)
                last_data = df_scalp.iloc[-1]
                
                # Parabolic SAR trailing logic
                psar_val = last_data['psar']
                
                # Update stop loss if PSAR has moved in our favor
                # (Simplified stop loss update logic)
                logger.info(f"Trailing {side} position. PSAR: {psar_val}")
                # exchange.place_order(SYMBOL, 'SELL' if side == 'LONG' else 'BUY', 'STOP_MARKET', abs(amount), stop_price=psar_val)

    except Exception as e:
        logger.error(f"Error managing positions: {e}")

def run_bot():
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        logger.error("Binance API credentials not found. Set them in .env")
        return

    # Initialize exchange and strategy
    exchange = BinanceFutures(BINANCE_API_KEY, BINANCE_API_SECRET)
    strategy = ScalpingStrategy(exchange)

    logger.info(f"Starting Scalper Bot for {SYMBOL}...")

    # Set leverage (Optional, set it manually in Binance for safety or add it here)
    # exchange.client.futures_change_leverage(symbol=SYMBOL, leverage=10)

    while True:
        try:
            # 0. Manage open positions
            manage_open_positions(exchange, strategy)
            
            # 1. Check for Strategy Signal
            side, data = strategy.get_signal()
            
            if side:
                logger.info(f"Signal detected: {side}")
                
                # Fetch balance
                balance = exchange.get_balance('USDT')
                current_price = data['close']
                atr = data['atr']
                
                # Calculate position size and SL/TP
                quantity = strategy.calculate_position_size(balance, current_price, atr)
                sl_price, tp1_price = strategy.get_tp_sl_levels(side, current_price, atr)
                
                # Round quantity and prices based on symbol info
                info = exchange.get_symbol_info(SYMBOL)
                # (Omitted rounding logic for brevity, but crucial in production)
                
                logger.info(f"Placing {side} order. Qty: {quantity}, SL: {sl_price}, TP1: {tp1_price}")
                
                # Step 8: Execute the Trade and Secure Initial Expectancy
                # Note: This is simplified. For Maker orders, we'd need to use Limit orders.
                # For this prototype, we'll use Market for execution but strategy suggests Limit.
                
                # 1. Market Order to Enter
                order_side = 'BUY' if side == 'LONG' else 'SELL'
                entry_order = exchange.place_order(SYMBOL, order_side, 'MARKET', quantity)
                
                if entry_order:
                    # 2. Place Take Profit Order (Limit)
                    # Close 60% of position at TP1
                    tp_quantity = quantity * TP1_PERCENT
                    tp_side = 'SELL' if side == 'LONG' else 'BUY'
                    exchange.place_order(SYMBOL, tp_side, 'LIMIT', tp_quantity, price=tp1_price)
                    
                    # 3. Place Stop Loss Order
                    sl_side = 'SELL' if side == 'LONG' else 'BUY'
                    exchange.place_order(SYMBOL, sl_side, 'STOP_MARKET', quantity, stop_price=sl_price)
                    
                    logger.info("Orders placed successfully.")
                    notify_trade(side, quantity, current_price, sl_price, tp1_price)
                    
                    # Sleep after placing orders to avoid immediate re-entry
                    time.sleep(300) # Sleep for 5 mins
            
            # Wait for the next candle (approx 1 minute)
            time.sleep(60)

        except Exception as e:
            logger.error(f"Error in main loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
