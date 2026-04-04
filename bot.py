#!/usr/bin/env python3
"""
Crypto.com Trading Bot — AI Enhanced + HMI Status Server
Strategy: RSI + Price Move triggers, Claude AI confirms/vetoes
Serves live status on port 8080 for HA Lovelace dashboard
"""

import hmac
import hashlib
import time
import json
import requests
import logging
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ─── CONFIG ───────────────────────────────────────────────────────────────────

CDC_API_KEY    = os.getenv("CDC_API_KEY", "YOUR_CDC_KEY")
CDC_API_SECRET = os.getenv("CDC_API_SECRET", "YOUR_CDC_SECRET")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")

TRADING_PAIRS = [
    "ETH_USDT",
    "SOL_USDT",
    "BTC_USDT",
    # "TURBO_USDT",
    # "AKT_USDT",
]

TRADE_SIZE_USDT   = 20.0
RSI_PERIOD        = 14
RSI_BUY_THRESHOLD  = 30
RSI_SELL_THRESHOLD = 70
PRICE_DROP_PCT     = -5.0
PRICE_SPIKE_PCT    = 5.0
PRICE_WINDOW       = 6
CANDLE_INTERVAL    = "5m"
MIN_CANDLES        = 20
POLL_INTERVAL      = 300
STATUS_PORT        = 8080

HA_NOTIFICATIONS_ENABLED = False
HA_URL            = os.getenv("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN          = os.getenv("HA_TOKEN", "")
HA_NOTIFY_SERVICE = "notify.mobile_app_your_phone"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

holdings = {pair: False for pair in TRADING_PAIRS}

# ─── SHARED STATUS (written by bot, read by HTTP server) ──────────────────────

status = {
    "last_update": None,
    "bot_running": True,
    "fear_greed": {"value": None, "label": "Unknown"},
    "pairs": {},
    "last_signal": None,
    "trade_history": [],
    "next_cycle_in": POLL_INTERVAL,
}

def update_pair_status(pair, price, rsi, price_move, signal=None, ai_decision=None, ai_confidence=None, ai_reasoning=None):
    status["pairs"][pair] = {
        "price": price,
        "rsi": round(rsi, 1) if rsi else None,
        "price_move": round(price_move, 2) if price_move else None,
        "signal": signal,
        "ai_decision": ai_decision,
        "ai_confidence": ai_confidence,
        "ai_reasoning": ai_reasoning,
        "updated": datetime.utcnow().strftime("%H:%M:%S UTC"),
    }

def log_trade(pair, side, price, quantity, rsi, ai_confidence, ai_reasoning, order_id):
    trade = {
        "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "pair": pair,
        "side": side,
        "price": price,
        "quantity": quantity,
        "value_usdt": round(price * quantity, 2),
        "rsi": round(rsi, 1),
        "ai_confidence": ai_confidence,
        "ai_reasoning": ai_reasoning,
        "order_id": order_id,
    }
    status["trade_history"].insert(0, trade)
    status["trade_history"] = status["trade_history"][:20]  # Keep last 20
    status["last_signal"] = trade

# ─── HTTP STATUS SERVER ───────────────────────────────────────────────────────

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Crypto Bot Status Server running. GET /status for data.")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs

def run_status_server():
    server = HTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler)
    log.info(f"Status server running on port {STATUS_PORT}")
    server.serve_forever()

# ─── CRYPTO.COM API ───────────────────────────────────────────────────────────

BASE_URL = "https://api.crypto.com/exchange/v1"

def sign_request(method, params=None):
    params = params or {}
    nonce = str(int(time.time() * 1000))
    param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else ""
    sig_payload = f"{method}{nonce}{CDC_API_KEY}{param_str}{nonce}"
    sig = hmac.new(CDC_API_SECRET.encode(), sig_payload.encode(), hashlib.sha256).hexdigest()
    return nonce, sig

def api_request(endpoint, params=None, private=False):
    url = f"{BASE_URL}/{endpoint}"
    headers = {"Content-Type": "application/json"}
    if private:
        nonce, sig = sign_request(endpoint, params or {})
        payload = {
            "id": int(time.time() * 1000),
            "method": endpoint,
            "api_key": CDC_API_KEY,
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
    data = api_request("public/get-candlestick", {
        "instrument_name": pair,
        "timeframe": interval,
        "count": depth
    })
    return [float(c["c"]) for c in data["result"]["data"]]

def get_balance(currency):
    data = api_request("private/get-account-summary", {"currency": currency}, private=True)
    for acc in data["result"]["accounts"]:
        if acc["currency"] == currency:
            return float(acc["available"])
    return 0.0

def place_market_order(pair, side, quantity):
    data = api_request("private/create-order", {
        "instrument_name": pair,
        "side": side.upper(),
        "type": "MARKET",
        "quantity": str(quantity),
    }, private=True)
    return data["result"]["order_id"]

# ─── MARKET DATA ──────────────────────────────────────────────────────────────

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = r.json()["data"][0]
        return int(data["value"]), data["value_classification"]
    except Exception as e:
        log.warning(f"Fear & Greed fetch failed: {e}")
        return None, "Unknown"

def get_recent_news(coin):
    try:
        symbol = coin.replace("_USDT", "")
        r = requests.get(
            f"https://api.duckduckgo.com/?q={symbol}+crypto+news&format=json&no_html=1&skip_disambig=1",
            timeout=5
        )
        data = r.json()
        topics = data.get("RelatedTopics", [])[:3]
        headlines = [t["Text"][:120] for t in topics if "Text" in t]
        return headlines if headlines else ["No recent news found"]
    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        return ["News unavailable"]

# ─── RSI & SIGNALS ────────────────────────────────────────────────────────────

def calculate_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(len(closes)-period, len(closes))]
    gains = [d for d in deltas if d > 0]
    losses = [abs(d) for d in deltas if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def check_price_move(closes, window=PRICE_WINDOW):
    if len(closes) < window:
        return 0.0
    return ((closes[-1] - closes[-window]) / closes[-window]) * 100

def get_rsi_signal(pair):
    try:
        closes = get_candles(pair)
        if len(closes) < MIN_CANDLES:
            log.warning(f"{pair}: Only {len(closes)} candles — skipping")
            return None, closes, None, None
        rsi = calculate_rsi(closes)
        price_move = check_price_move(closes)
        log.info(f"{pair} | Price: {closes[-1]:.6f} | RSI: {rsi:.1f} | Move: {price_move:+.2f}%")
        update_pair_status(pair, closes[-1], rsi, price_move)
        if rsi is None:
            return None, closes, rsi, price_move
        if rsi <= RSI_BUY_THRESHOLD or price_move <= PRICE_DROP_PCT:
            return "BUY", closes, rsi, price_move
        elif rsi >= RSI_SELL_THRESHOLD or price_move >= PRICE_SPIKE_PCT:
            return "SELL", closes, rsi, price_move
        return None, closes, rsi, price_move
    except Exception as e:
        log.error(f"Signal error for {pair}: {e}")
        return None, [], None, None

# ─── AI ANALYSIS ──────────────────────────────────────────────────────────────

def ai_confirm_signal(pair, signal, closes, rsi, price_move, fear_greed_val, fear_greed_label, news):
    symbol = pair.replace("_USDT", "")
    recent_prices = closes[-10:]
    price_change_24h = ((closes[-1] - closes[-48]) / closes[-48] * 100) if len(closes) >= 48 else None

    prompt = f"""You are a crypto trading assistant. A rule-based RSI bot has triggered a {signal} signal for {symbol}/USDT.

MARKET DATA:
- Current price: ${closes[-1]:.6f}
- RSI (14): {rsi:.1f}
- Price move (last 30 min): {price_move:+.2f}%
- 24h price change: {f'{price_change_24h:+.2f}%' if price_change_24h else 'N/A'}
- Last 10 closes: {[round(p, 6) for p in recent_prices]}

SENTIMENT:
- Crypto Fear & Greed Index: {fear_greed_val}/100 ({fear_greed_label})

RECENT NEWS:
{chr(10).join(f'- {h}' for h in news)}

The bot wants to {signal} ${TRADE_SIZE_USDT} USD worth of {symbol}.

Respond in this exact format:
DECISION: [CONFIRM or VETO]
CONFIDENCE: [HIGH, MEDIUM, or LOW]
REASONING: [2-3 sentences max]"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        log.info(f"AI Analysis for {pair}:\n{text}")
        confirmed = "DECISION: CONFIRM" in text
        confidence = "HIGH" if "CONFIDENCE: HIGH" in text else "MEDIUM" if "CONFIDENCE: MEDIUM" in text else "LOW"
        reasoning = next((l.replace("REASONING:", "").strip() for l in text.split("\n") if l.startswith("REASONING:")), "")
        return confirmed, confidence, reasoning
    except Exception as e:
        log.error(f"AI analysis failed: {e}")
        return True, "LOW", "AI unavailable, proceeding on RSI signal"

# ─── HA NOTIFICATIONS ─────────────────────────────────────────────────────────

def send_ha_notification(title, message):
    if not HA_NOTIFICATIONS_ENABLED:
        log.info(f"[HA NOTIFY DISABLED] {title}: {message}")
        return
    try:
        url = f"{HA_URL}/api/services/notify/{HA_NOTIFY_SERVICE.split('.')[-1]}"
        headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
        requests.post(url, headers=headers, json={"title": title, "message": message}, timeout=5).raise_for_status()
    except Exception as e:
        log.warning(f"HA notification failed: {e}")

# ─── TRADE EXECUTION ──────────────────────────────────────────────────────────

def execute_trade(pair, signal, closes, rsi, price_move, ai_reasoning, ai_confidence):
    base_currency = pair.split("_")[0]
    try:
        if signal == "BUY":
            if holdings.get(pair):
                log.info(f"Already holding {pair} — skipping BUY")
                return
            usdt_balance = get_balance("USDT")
            if usdt_balance < TRADE_SIZE_USDT:
                log.warning(f"Insufficient USDT for {pair}. Have {usdt_balance:.2f}")
                return
            price = closes[-1]
            quantity = round(TRADE_SIZE_USDT / price, 6)
            order_id = place_market_order(pair, "BUY", quantity)
            holdings[pair] = True
            log_trade(pair, "BUY", price, quantity, rsi, ai_confidence, ai_reasoning, order_id)
            update_pair_status(pair, price, rsi, price_move, "BUY", "CONFIRMED", ai_confidence, ai_reasoning)
            msg = f"🟢 BUY {quantity} {base_currency} @ ~{price:.6f}\nRSI: {rsi:.1f} | AI: {ai_confidence}\n{ai_reasoning}\nOrder: {order_id}"
            log.info(msg)
            send_ha_notification(f"Bot: BUY {base_currency}", msg)

        elif signal == "SELL":
            base_balance = get_balance(base_currency)
            if base_balance <= 0:
                log.warning(f"Nothing to sell for {pair}")
                return
            quantity = round(base_balance, 6)
            order_id = place_market_order(pair, "SELL", quantity)
            holdings[pair] = False
            price = closes[-1]
            log_trade(pair, "SELL", price, quantity, rsi, ai_confidence, ai_reasoning, order_id)
            update_pair_status(pair, price, rsi, price_move, "SELL", "CONFIRMED", ai_confidence, ai_reasoning)
            msg = f"🔴 SELL {quantity} {base_currency} @ ~{price:.6f}\nRSI: {rsi:.1f} | AI: {ai_confidence}\n{ai_reasoning}\nOrder: {order_id}"
            log.info(msg)
            send_ha_notification(f"Bot: SELL {base_currency}", msg)

    except Exception as e:
        log.error(f"Trade execution error for {pair}: {e}")
        send_ha_notification(f"Bot ERROR: {pair}", str(e))

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info("Crypto.com AI Trading Bot Starting")
    log.info(f"Pairs: {', '.join(TRADING_PAIRS)}")
    log.info(f"Strategy: RSI({RSI_BUY_THRESHOLD}/{RSI_SELL_THRESHOLD}) + Price Move + Claude AI")
    log.info(f"Trade size: {TRADE_SIZE_USDT} USDT | Status port: {STATUS_PORT}")
    log.info("=" * 55)

    # Start status server in background thread
    t = threading.Thread(target=run_status_server, daemon=True)
    t.start()

    send_ha_notification("AI Trading Bot Started", f"Watching: {', '.join(TRADING_PAIRS)}")

    while True:
        try:
            status["last_update"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            status["bot_running"] = True

            fear_val, fear_label = get_fear_greed()
            status["fear_greed"] = {"value": fear_val, "label": fear_label}
            log.info(f"Fear & Greed: {fear_val}/100 ({fear_label})")

            for pair in TRADING_PAIRS:
                signal, closes, rsi, price_move = get_rsi_signal(pair)

                if signal and rsi is not None:
                    log.info(f"RSI Signal: {signal} on {pair} — consulting AI...")
                    news = get_recent_news(pair)
                    confirmed, confidence, reasoning = ai_confirm_signal(
                        pair, signal, closes, rsi, price_move, fear_val, fear_label, news
                    )
                    update_pair_status(pair, closes[-1], rsi, price_move, signal,
                                       "CONFIRMED" if confirmed else "VETOED", confidence, reasoning)
                    if confirmed:
                        log.info(f"AI CONFIRMED {signal} on {pair} ({confidence}) — executing")
                        execute_trade(pair, signal, closes, rsi, price_move, reasoning, confidence)
                    else:
                        log.info(f"AI VETOED {signal} on {pair} ({confidence}): {reasoning}")
                        send_ha_notification(f"Bot: {signal} VETOED {pair.split('_')[0]}", f"AI blocked the trade.\n{reasoning}")

            log.info(f"Cycle complete. Sleeping {POLL_INTERVAL}s...\n")

            # Count down next cycle in status
            for i in range(POLL_INTERVAL, 0, -10):
                status["next_cycle_in"] = i
                time.sleep(10)

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
