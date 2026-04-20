import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def send_telegram_message(message):
    """Send a message to a Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def notify_trade(side, qty, entry_price, sl_price, tp_price):
    """Alert for a new trade entry."""
    msg = (
        f"🚀 *New Trade Entry: {side}*\n"
        f"💰 Symbol: DOGEUSDT\n"
        f"📊 Quantity: {qty:.2f}\n"
        f"🎯 Entry: {entry_price:.5f}\n"
        f"🛑 SL: {sl_price:.5f}\n"
        f"📈 TP1: {tp_price:.5f}"
    )
    send_telegram_message(msg)

def notify_error(error_msg):
    """Alert for critical errors."""
    msg = f"⚠️ *Bot Error*\n{error_msg}"
    send_telegram_message(msg)
