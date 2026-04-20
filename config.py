import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')

# Strategy Parameters
SYMBOL = 'DOGEUSDT'
TIMEFRAME_MACRO = '15m'
TIMEFRAME_SCALP = '1m'
EMA_PERIOD = 100
RSI_PERIOD = 14
STOCH_RSI_PERIOD = 14
ATR_PERIOD = 14
ATR_MULTIPLIER = 1.5

# Risk Management
RISK_PERCENT = 0.01  # 1% per trade
TP1_PERCENT = 0.60  # 60% of position
TP2_PERCENT = 0.40  # 40% of position
RR_RATIO = 1.0

# Telegram Alerts
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
