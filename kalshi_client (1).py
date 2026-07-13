"""Kalshi API client — RSA-signed requests for markets, orders, positions."""
import base64
import datetime
import json
import logging
import os
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger("kalshi")

BASE_URL = os.environ.get("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")
KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY", "")  # full PEM contents


class KalshiClient:
    def __init__(self):
        if not KEY_ID or not PRIVATE_KEY_PEM:
            raise RuntimeError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY env vars required")
        pem_str = PRIVATE_KEY_PEM.replace("\\n", "\n")
        # If Railway collapsed the key to one line, reconstruct proper PEM
        if "\n" not in pem_str:
            header = "-----BEGIN RSA PRIVATE KEY-----"
            footer = "-----END RSA PRIVATE KEY-----"
            body = pem_str.replace(header, "").replace(footer, "").strip()
            wrapped = "\n".join(body[i:i+64] for i in range(0, len(body), 64))
            pem_str = header + "\n" + wrapped + "\n" + footer
        pem = pem_str.encode()
        self._key = serialization.load_pem_private_key(pem, password=None)
        self._session = requests.Session()

    # ── auth ────────────────────────────────────────────────────────────
    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + "/trade-api/v2" + path
        sig = self._key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    def _req(self, method: str, path: str, params=None, body=None):
        url = BASE_URL + path
        for attempt in range(3):
            try:
                r = self._session.request(
                    method, url,
                    headers=self._headers(method, path),
                    params=params,
                    json=body,
                    timeout=15,
                )
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    log.warning(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                    return None
                return r.json()
            except requests.RequestException as e:
                log.warning(f"{method} {path} network error: {e}")
                time.sleep(1.0 * (attempt + 1))
        return None

    # ── markets ─────────────────────────────────────────────────────────
    def get_markets(self, series_ticker=None, status="open", limit=200):
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        out = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._req("GET", "/markets", params=params)
            if not data:
                break
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor or len(out) >= 1000:
                break
        return out

    def get_orderbook(self, ticker: str, depth: int = 10):
        return self._req("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ── portfolio ───────────────────────────────────────────────────────
    def get_balance(self):
        data = self._req("GET", "/portfolio/balance")
        return (data or {}).get("balance")  # cents

    def get_positions(self):
        data = self._req("GET", "/portfolio/positions", params={"limit": 200})
        return (data or {}).get("market_positions", [])

    def get_open_orders(self):
        data = self._req("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
        return (data or {}).get("orders", [])

    def get_fills(self, min_ts=None):
        params = {"limit": 200}
        if min_ts:
            params["min_ts"] = min_ts
        data = self._req("GET", "/portfolio/fills", params=params)
        return (data or {}).get("fills", [])

    # ── orders ──────────────────────────────────────────────────────────
    def place_limit(self, ticker: str, side: str, price_cents: int, count: int):
        """side: 'yes' or 'no'. price_cents 1-99. count = # contracts."""
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
        }
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        data = self._req("POST", "/portfolio/orders", body=body)
        if data and "order" in data:
            return data["order"].get("order_id")
        return None

    def cancel_order(self, order_id: str) -> bool:
        return self._req("DELETE", f"/portfolio/orders/{order_id}") is not None

    def cancel_all(self):
        orders = self.get_open_orders()
        n = 0
        for o in orders:
            if self.cancel_order(o.get("order_id", "")):
                n += 1
            time.sleep(0.15)
        log.info(f"cancel_all: cancelled {n}/{len(orders)}")
        return n


def kalshi_fee_cents(price_cents: int, count: int) -> float:
    """Kalshi trading fee: 0.07 * C * P * (1-P), rounded up to next cent."""
    import math
    p = price_cents / 100.0
    fee = 0.07 * count * p * (1 - p)
    return math.ceil(fee * 100) / 100 * 100  # return in cents
