import os
import time
import jwt
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
API_KEY        = os.environ.get("COINBASE_API_KEY", "")
API_SECRET     = os.environ.get("COINBASE_API_SECRET", "")
TRADE_AMOUNT   = float(os.environ.get("TRADE_AMOUNT_USDC", "20"))
PAIRS          = os.environ.get("PAIRS", "FET-USDC,OCEAN-USDC").split(",")
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "60"))

# RSI settings
RSI_PERIOD     = 14
RSI_BUY        = 35
RSI_SELL       = 65
MIN_CANDLES    = 50

# MACD settings
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9

BASE_URL       = "https://api.coinbase.com"

# ── Auth ──────────────────────────────────────────────────────────────────────
def build_jwt(method: str, path: str) -> str:
    """Build JWT for Coinbase Advanced Trade API (CDP key format)."""
    uri = f"{method} api.coinbase.com{path}"
    payload = {
        "sub": API_KEY,
        "iss": "cdp",
        "nbf": int(time.time()),
        "exp": int(time.time()) + 120,
        "uri": uri,
    }
    headers = {"kid": API_KEY, "nonce": os.urandom(16).hex()}
    token = jwt.encode(payload, API_SECRET, algorithm="ES256", headers=headers)
    return token


def get_headers(method: str, path: str) -> dict:
    return {
        "Authorization": f"Bearer {build_jwt(method, path)}",
        "Content-Type": "application/json",
    }


# ── Market Data ───────────────────────────────────────────────────────────────
def get_candles(product_id: str, limit: int = 100) -> pd.DataFrame:
    """Fetch OHLCV candles — ONE_MINUTE granularity."""
    path = f"/api/v3/brokerage/products/{product_id}/candles"
    params = {"granularity": "ONE_MINUTE", "limit": limit}
    resp = requests.get(
        f"{BASE_URL}{path}",
        headers=get_headers("GET", path),
        params=params,
        timeout=10
    )
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    if not candles:
        raise ValueError(f"No candle data for {product_id}")

    df = pd.DataFrame(candles)
    df = df.rename(columns={"start": "timestamp"})
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def get_best_ask(product_id: str) -> float:
    """Get current best ask price for sizing sell orders."""
    path = "/api/v3/brokerage/best_bid_ask"
    resp = requests.get(
        f"{BASE_URL}{path}",
        headers=get_headers("GET", path),
        params={"product_ids": product_id},
        timeout=10
    )
    resp.raise_for_status()
    pricebooks = resp.json().get("pricebooks", [])
    if not pricebooks or not pricebooks[0].get("asks"):
        raise ValueError(f"No ask price for {product_id}")
    return float(pricebooks[0]["asks"][0]["price"])


def get_balance(currency: str) -> float:
    """Get available balance for a given currency."""
    path = "/api/v3/brokerage/accounts"
    resp = requests.get(
        f"{BASE_URL}{path}",
        headers=get_headers("GET", path),
        timeout=10
    )
    resp.raise_for_status()
    for acct in resp.json().get("accounts", []):
        if acct.get("currency") == currency:
            return float(acct["available_balance"]["value"])
    return 0.0


# ── Indicators ────────────────────────────────────────────────────────────────
def calc_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calc_macd(closes: pd.Series):
    """Returns (macd_line, signal_line, histogram) as floats for latest candle."""
    ema_fast  = closes.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow  = closes.ewm(span=MACD_SLOW, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal    = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
    histogram = macd_line - signal
    return float(macd_line.iloc[-1]), float(signal.iloc[-1]), float(histogram.iloc[-1])


def get_signal(df: pd.DataFrame) -> str:
    """
    Combined RSI + MACD signal:
    BUY  — RSI oversold (<35) AND MACD crosses above signal line
    SELL — RSI overbought (>65) AND MACD crosses below signal line
    HOLD — anything else
    Requires MIN_CANDLES history to avoid false signals on startup.
    """
    if len(df) < MIN_CANDLES:
        return "HOLD"

    closes = df["close"]
    rsi    = calc_rsi(closes)

    # Current and previous candle MACD values to detect crossover
    macd_now,  sig_now,  _ = calc_macd(closes)
    macd_prev, sig_prev, _ = calc_macd(closes.iloc[:-1])

    macd_cross_up   = (macd_prev < sig_prev) and (macd_now >= sig_now)
    macd_cross_down = (macd_prev > sig_prev) and (macd_now <= sig_now)

    if rsi < RSI_BUY and macd_cross_up:
        return "BUY"
    elif rsi > RSI_SELL and macd_cross_down:
        return "SELL"
    else:
        return "HOLD"


# ── Order Execution ───────────────────────────────────────────────────────────
def place_market_order(product_id: str, side: str, usdc_amount: float) -> dict:
    """
    Place a market order via Coinbase Advanced Trade.
    BUY  uses quote_size  (spend X USDC)
    SELL uses base_size   (sell equivalent base tokens)
    """
    path = "/api/v3/brokerage/orders"

    if side == "BUY":
        order_config = {
            "market_market_ioc": {
                "quote_size": str(round(usdc_amount, 2))
            }
        }
    else:
        ask      = get_best_ask(product_id)
        base_qty = usdc_amount / ask
        order_config = {
            "market_market_ioc": {
                "base_size": str(round(base_qty, 6))
            }
        }

    body = {
        "client_order_id":     f"cb-bot-{int(time.time()*1000)}",
        "product_id":          product_id,
        "side":                side,
        "order_configuration": order_config,
    }

    resp = requests.post(
        f"{BASE_URL}{path}",
        headers=get_headers("POST", path),
        json=body,
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()


# ── Position State ────────────────────────────────────────────────────────────
# Simple in-memory state — True = currently holding this pair
positions = {pair: False for pair in PAIRS}


# ── Main Loop ─────────────────────────────────────────────────────────────────
def run():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Coinbase bot starting.")
    print(f"  Pairs:         {PAIRS}")
    print(f"  Trade size:    ${TRADE_AMOUNT} USDC")
    print(f"  RSI buy/sell:  <{RSI_BUY} / >{RSI_SELL}")
    print(f"  MACD:          {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}")
    print(f"  Min candles:   {MIN_CANDLES}")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    print("-" * 55)

    while True:
        for pair in PAIRS:
            try:
                df              = get_candles(pair, limit=100)
                signal          = get_signal(df)
                rsi             = calc_rsi(df["close"])
                macd, sig, hist = calc_macd(df["close"])
                price           = df["close"].iloc[-1]

                print(
                    f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                    f"{pair:12s} | ${price:.6f} | RSI: {rsi:.1f} | "
                    f"MACD: {macd:.6f} | Sig: {sig:.6f} | {signal}"
                )

                if signal == "BUY" and not positions[pair]:
                    usdc_bal = get_balance("USDC")
                    if usdc_bal >= TRADE_AMOUNT:
                        result = place_market_order(pair, "BUY", TRADE_AMOUNT)
                        positions[pair] = True
                        print(f"  ✅ BUY  {pair} — ${TRADE_AMOUNT} USDC | Order: {result.get('order_id', '?')}")
                    else:
                        print(f"  ⚠️  BUY signal but insufficient USDC balance (${usdc_bal:.2f})")

                elif signal == "SELL" and positions[pair]:
                    result = place_market_order(pair, "SELL", TRADE_AMOUNT)
                    positions[pair] = False
                    print(f"  ✅ SELL {pair} — ~${TRADE_AMOUNT} USDC | Order: {result.get('order_id', '?')}")

            except Exception as e:
                print(f"  ❌ {pair} error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
