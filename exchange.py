from binance import Client, ThreadedWebsocketManager, ThreadedDepthCacheManager
from binance.exceptions import BinanceAPIException
import pandas as pd
import time
from config import BINANCE_API_KEY, BINANCE_API_SECRET

class BinanceFutures:
    def __init__(self, api_key, api_secret):
        try:
            self.client = Client(api_key, api_secret)
            self.client.ping()
        except BinanceAPIException as e:
            if "restricted location" in str(e).lower():
                print("CRITICAL: This environment is in a restricted location (e.g. US) and cannot trade Binance Futures.")
                raise e
            else:
                raise e
        self.api_key = api_key
        self.api_secret = api_secret

    def get_klines(self, symbol, interval, limit=1000):
        """Fetch historical klines for indicator calculation."""
        try:
            klines = self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)
            df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 
                                               'close_time', 'quote_asset_volume', 'number_of_trades',
                                               'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
            
            # Use float for numeric columns
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            df[numeric_cols] = df[numeric_cols].astype(float)
            
            # Timestamp to datetime
            df['time'] = pd.to_datetime(df['time'], unit='ms')
            
            return df
        except BinanceAPIException as e:
            print(f"Error fetching klines: {e}")
            return None

    def get_balance(self, asset='USDT'):
        """Get futures account balance."""
        try:
            balance = self.client.futures_account_balance()
            for b in balance:
                if b['asset'] == asset:
                    return float(b['balance'])
            return 0.0
        except BinanceAPIException as e:
            print(f"Error getting balance: {e}")
            return 0.0

    def get_mark_price(self, symbol):
        """Get the mark price for a symbol."""
        try:
            res = self.client.futures_mark_price(symbol=symbol)
            return float(res['markPrice'])
        except BinanceAPIException as e:
            print(f"Error getting mark price: {e}")
            return None

    def place_order(self, symbol, side, order_type, quantity, price=None, stop_price=None, time_in_force='GTC'):
        """Place a futures order."""
        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': order_type,
                'quantity': quantity
            }
            if price:
                params['price'] = price
                params['timeInForce'] = time_in_force
            if stop_price:
                params['stopPrice'] = stop_price

            order = self.client.futures_create_order(**params)
            return order
        except BinanceAPIException as e:
            print(f"Error placing order: {e}")
            return None

    def get_symbol_info(self, symbol):
        """Get symbol information (precision, etc)."""
        try:
            info = self.client.futures_exchange_info()
            for s in info['symbols']:
                if s['symbol'] == symbol:
                    return s
            return None
        except BinanceAPIException as e:
            print(f"Error getting symbol info: {e}")
            return None
