#!/usr/bin/env python3
"""
Crypto.com Trading Bot
Pairs: ETH/USDT, TURBO/USDT (easily extensible)
Strategy: RSI + Price Spike/Drop
"""

import hmac
import hashlib
import time
import json
import requests
import logging
import os
from datetime import datetime
from collections import deque

# ─── CONFIG ───────────────────────────────────────────────────────────────────

API_KEY = os.getenv("CDC_API_KEY", "YOUR_API_KEY_HERE")
API_SECRET = os.getenv("CDC_API_SECRET", "YOUR_API_SECRET_HERE")

# Trading pairs - add more here as needed
TRADING_PAIRS = [
    "ETH_USDT",
    "TURBO_USDT",
    # "SOL_USDT",   # Uncomment to add more
    # "XRP_USDT",
]

# Trade size per signal (USDT)
TRADE_SIZE_USDT = 20.0

# RSI settings
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 30      # Buy when RSI drops to/below this
RSI_SELL_THRESHOLD = 70     # Sell when RSI rises to/above this

# Price spike/drop alert thresholds (% change over PRICE_WINDOW candles)
PRICE_DROP_PCT = -5.0       # Buy signal on drop >= this %
PRICE_SPIKE_PCT = 5.0       # Sell signal on spike >= this %
PRICE_WINDOW = 6            # Number of candles to measure change over

# Candle interval: 1m, 5m, 15m, 1h, 4h, 1D
CANDLE_INTERVAL = "5m"

# HA notifications (set to True once you have remote access sorted)
HA_NOTIFICATIONS_ENABLED = False
HA_URL = os.getenv("HA_URL", "http://YOUR_HA_IP:8123")
HA_TOKEN = os.getenv("HA_TOKEN", "YOUR_HA_LONG_LIVED_TOKEN")
HA_NOTIFY_SERVICE = "notify.mobile_app_your_phone"  # Change to your device

# Logging
LOG_FILE = "bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── CRYPTO.COM API ───────────────────────────────────────────────────────────

BASE_URL = "https://api.crypto.com/exchange/v1"

def sign_request(method, params=None):
    """Generate HMAC-SHA256 signature for Crypto.com Exchange API."""
    if params is None:
        params = {}
    nonce = str(int(time.time() * 1000))
    param_str = ""
    if params:
        param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    sig_payload = f"{method}{nonce}{API_KEY}{param_str}{nonce}"
    sig = hmac.new(
        API_SECRET.encode("utf-8"),
        sig_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return nonce, sig

def api_request(endpoint, params=None, private=False):
    """Make a request to the Crypto.com Exchange API."""
    url = f"{BASE_URL}/{endpoint}"
    headers = {"Content-Type": "application/json"}

    if private:
        nonce, sig = sign_request(endpoint, params or {})
        payload = {
            "id": int(time.time() * 1000),
            "method": endpoint,
            "api_key": API_KEY,
            "params": params or {},
            "nonce": nonce,
            "sig": sig
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
    else:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=10)

    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 0) != 0:
        raise Exception(f"API error {data.get('code')}: {data.get('message')}")
    return data

def get_candles(pair, interval=CANDLE_INTERVAL, depth=50):
    """Fetch OHLCV candle data for a pair."""
    data = api_request("public/get-candlestick", {
        "instrument_name": pair,
        "timeframe": interval,
        "count": depth
    })
    candles = data["result"]["data"]
    closes = [float(c["c"]) for c in candles]
    return closes

def get_balance(currency):
    """Get available balance for a currency."""
    data = api_request("private/get-account-summary", {
        "currency": currency
    }, private=True)
    accounts = data["result"]["accounts"]
    for acc in accounts:
        if acc["currency"] == currency:
            return float(acc["available"])
    return 0.0

def place_market_order(pair, side, quantity):
    """Place a market buy or sell order."""
    data = api_request("private/create-order", {
        "instrument_name": pair,
        "side": side.upper(),   # BUY or SELL
        "type": "MARKET",
        "quantity": str(quantity),
    }, private=True)
    order_id = data["result"]["order_id"]
    log.info(f"Order placed: {side} {quantity} {pair} | ID: {order_id}")
    return order_id

# ─── STRATEGY ─────────────────────────────────────────────────────────────────

def calculate_rsi(closes, period=RSI_PERIOD):
    """Calculate RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-period + i - 1] - closes[-period + i - 2] if i > 1 else closes[-1] - closes[-2]
        (gains if diff >= 0 else losses).append(abs(diff))

    # Proper RSI calculation
    deltas = [closes[i] - closes[i-1] for i in range(len(closes)-period, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [abs(d) for d in deltas if d < 0]

    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.0001

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def check_price_move(closes, window=PRICE_WINDOW):
    """Check % price change over last N candles."""
    if len(closes) < window:
        return 0.0
    old_price = closes[-window]
    new_price = closes[-1]
    return ((new_price - old_price) / old_price) * 100

def get_signal(pair):
    """
    Returns: 'BUY', 'SELL', or None
    Based on RSI + price move combo.
    """
    try:
        closes = get_candles(pair)
        rsi = calculate_rsi(closes)
        price_move = check_price_move(closes)

        log.info(f"{pair} | Price: {closes[-1]:.6f} | RSI: {rsi:.1f} | Move: {price_move:+.2f}%")

        if rsi is None:
            return None, rsi, price_move

        buy_signal = rsi <= RSI_BUY_THRESHOLD or price_move <= PRICE_DROP_PCT
        sell_signal = rsi >= RSI_SELL_THRESHOLD or price_move >= PRICE_SPIKE_PCT

        if buy_signal:
            reason = f"RSI={rsi:.1f}" if rsi <= RSI_BUY_THRESHOLD else f"Drop={price_move:.1f}%"
            return "BUY", rsi, price_move, reason
        elif sell_signal:
            reason = f"RSI={rsi:.1f}" if rsi >= RSI_SELL_THRESHOLD else f"Spike={price_move:.1f}%"
            return "SELL", rsi, price_move, reason

        return None, rsi, price_move, None

    except Exception as e:
        log.error(f"Signal error for {pair}: {e}")
        return None, None, None, None

# ─── HOME ASSISTANT NOTIFICATIONS ─────────────────────────────────────────────

def send_ha_notification(title, message):
    """Send a push notification via Home Assistant."""
    if not HA_NOTIFICATIONS_ENABLED:
        log.info(f"[HA NOTIFY DISABLED] {title}: {message}")
        return
    try:
        url = f"{HA_URL}/api/services/notify/{HA_NOTIFY_SERVICE.split('.')[-1]}"
        headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"title": title, "message": message}
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        resp.raise_for_status()
        log.info(f"HA notification sent: {title}")
    except Exception as e:
        log.warning(f"HA notification failed: {e}")

# ─── TRADE EXECUTION ──────────────────────────────────────────────────────────

# Track what we're holding to avoid duplicate buys
holdings = {pair: False for pair in TRADING_PAIRS}

def execute_trade(pair, signal, reason):
    """Execute a trade based on signal."""
    base_currency = pair.split("_")[0]   # e.g. ETH
    quote_currency = pair.split("_")[1]  # e.g. USDT

    try:
        if signal == "BUY":
            usdt_balance = get_balance("USDT")
            if usdt_balance < TRADE_SIZE_USDT:
                log.warning(f"Insufficient USDT for {pair} buy. Have {usdt_balance:.2f}")
                return

            # Get current price to calculate quantity
            closes = get_candles(pair, depth=5)
            price = closes[-1]
            quantity = round(TRADE_SIZE_USDT / price, 6)

            order_id = place_market_order(pair, "BUY", quantity)
            holdings[pair] = True

            msg = f"🟢 BUY {quantity} {base_currency} @ ~{price:.6f}\nReason: {reason}\nOrder: {order_id}"
            log.info(msg)
            send_ha_notification(f"Bot: BUY {base_currency}", msg)

        elif signal == "SELL":
            base_balance = get_balance(base_currency)
            if base_balance <= 0:
                log.warning(f"Nothing to sell for {pair}")
                return

            # Sell full balance of that coin
            quantity = round(base_balance, 6)
            order_id = place_market_order(pair, "SELL", quantity)
            holdings[pair] = False

            closes = get_candles(pair, depth=5)
            price = closes[-1]
            msg = f"🔴 SELL {quantity} {base_currency} @ ~{price:.6f}\nReason: {reason}\nOrder: {order_id}"
            log.info(msg)
            send_ha_notification(f"Bot: SELL {base_currency}", msg)

    except Exception as e:
        log.error(f"Trade execution error for {pair}: {e}")
        send_ha_notification(f"Bot ERROR: {pair}", str(e))

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 300  # 5 minutes (matches 5m candles)

def main():
    log.info("=" * 50)
    log.info("Crypto.com Trading Bot Starting")
    log.info(f"Pairs: {', '.join(TRADING_PAIRS)}")
    log.info(f"Strategy: RSI({RSI_BUY_THRESHOLD}/{RSI_SELL_THRESHOLD}) + Price Move({PRICE_DROP_PCT}%/{PRICE_SPIKE_PCT}%)")
    log.info(f"Trade size: {TRADE_SIZE_USDT} USDT")
    log.info(f"HA Notifications: {'ENABLED' if HA_NOTIFICATIONS_ENABLED else 'DISABLED'}")
    log.info("=" * 50)

    send_ha_notification("Trading Bot Started", f"Watching: {', '.join(TRADING_PAIRS)}")

    while True:
        try:
            for pair in TRADING_PAIRS:
                result = get_signal(pair)
                signal = result[0]
                reason = result[3] if len(result) > 3 else None

                if signal:
                    log.info(f"Signal: {signal} on {pair} ({reason})")
                    execute_trade(pair, signal, reason)

            log.info(f"Cycle complete. Sleeping {POLL_INTERVAL_SECONDS}s...\n")
            time.sleep(POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(30)  # Short sleep on error before retrying

if __name__ == "__main__":
    main()
