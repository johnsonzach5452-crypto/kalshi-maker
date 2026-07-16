"""Kalshi API client — RSA-signed requests with a real error taxonomy.

Error model (item A1):
  * KalshiRejected    — the API understood us and said no (4xx). The request
                        definitively did NOT happen. Don't retry blindly.
  * KalshiUnavailable — network error / 5xx / 429s exhausted. We DON'T KNOW
                        what happened. Callers must not assume "no data =
                        flat"; portfolio getters raise instead of returning [].

Order placement returns a PlaceResult with status:
  'placed'   — order_id confirmed resting
  'rejected' — exchange refused it (bad price/params); safe to move on
  'unknown'  — sent but no confirmation; the order MAY be live. Caller must
               reconcile against the exchange before quoting that market again.

Rate limiting (item A5): Kalshi basic tier publishes ~10 reads/s and
~5 transactions/s. All requests pass through a pacer set below those caps
(READ_RPS / WRITE_RPS in config.py).
"""
import base64
import logging
import math
import os
import threading
import time
import uuid

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import KALSHI_BASE_URL, READ_RPS, WRITE_RPS

log = logging.getLogger("kalshi")

KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY", "")


class KalshiError(Exception):
    pass


class KalshiRejected(KalshiError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = (body or "")[:300]
        super().__init__(f"HTTP {status_code}: {self.body}")


class KalshiUnavailable(KalshiError):
    """Network failure / 5xx / rate-limit exhaustion — outcome unknown."""


class PlaceResult:
    def __init__(self, status: str, order_id=None, client_order_id=None, reason=""):
        self.status = status            # 'placed' | 'rejected' | 'unknown'
        self.order_id = order_id
        self.client_order_id = client_order_id
        self.reason = reason


class _Pacer:
    """Minimum-interval request spacing (simple, thread-safe)."""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class KalshiClient:
    def __init__(self):
        if not KEY_ID or not PRIVATE_KEY_PEM:
            raise RuntimeError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY env vars required")
        pem = PRIVATE_KEY_PEM.replace("\\n", "\n").encode()
        self._key = serialization.load_pem_private_key(pem, password=None)
        self._session = requests.Session()
        self._read_pacer = _Pacer(READ_RPS)
        self._write_pacer = _Pacer(WRITE_RPS)

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

    def _req(self, method: str, path: str, params=None, body=None) -> dict:
        """Returns parsed JSON dict ({} for empty bodies).
        Raises KalshiRejected on 4xx, KalshiUnavailable otherwise."""
        pacer = self._read_pacer if method.upper() == "GET" else self._write_pacer
        url = KALSHI_BASE_URL + path
        last_err = "retries exhausted"
        for attempt in range(3):
            pacer.wait()
            try:
                r = self._session.request(
                    method, url, headers=self._headers(method, path),
                    params=params, json=body, timeout=15)
            except requests.RequestException as e:
                last_err = f"network: {e.__class__.__name__}"
                log.warning(f"{method} {path} {last_err}")
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code == 429:
                last_err = "rate limited (429)"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 500:
                last_err = f"server error {r.status_code}"
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code >= 400:
                log.warning(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                raise KalshiRejected(r.status_code, r.text)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {}
        raise KalshiUnavailable(f"{method} {path}: {last_err}")

    def _paginate(self, path: str, params: dict, list_key: str,
                  max_items: int = 2000) -> list:
        out, cursor = [], None
        params = dict(params)
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._req("GET", path, params=params)
            out.extend(data.get(list_key, []))
            cursor = data.get("cursor")
            if not cursor or len(out) >= max_items:
                return out

    # ── markets ─────────────────────────────────────────────────────────
    def get_markets(self, series_ticker=None, status="open", limit=200):
        params = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        return self._paginate("/markets", params, "markets", max_items=1000)

    def get_market(self, ticker: str) -> dict:
        return self._req("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 10):
        data = self._req("GET", f"/markets/{ticker}/orderbook",
                         params={"depth": depth})
        return data.get("orderbook") or {}

    def get_trades(self, ticker: str, min_ts: int = None, limit: int = 100):
        params = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = min_ts
        return self._paginate("/markets/trades", params, "trades", max_items=500)

    # ── portfolio (all paginated; all RAISE on failure — never fake-flat) ─
    def get_balance(self) -> int:
        return self._req("GET", "/portfolio/balance").get("balance", 0)

    def get_positions(self) -> list:
        return self._paginate("/portfolio/positions", {"limit": 200},
                              "market_positions")

    def get_open_orders(self) -> list:
        return self._paginate("/portfolio/orders",
                              {"status": "resting", "limit": 200}, "orders")

    def get_fills(self, min_ts=None) -> list:
        params = {"limit": 200}
        if min_ts:
            params["min_ts"] = min_ts
        return self._paginate("/portfolio/fills", params, "fills")

    def get_settlements(self, limit=200) -> list:
        return self._paginate("/portfolio/settlements", {"limit": limit},
                              "settlements", max_items=1000)

    # ── orders ──────────────────────────────────────────────────────────
    def place_limit(self, ticker: str, side: str, price_cents: int,
                    count: int) -> PlaceResult:
        """side: 'yes' or 'no'. price_cents 1-99. count = # contracts."""
        coid = str(uuid.uuid4())
        body = {
            "ticker": ticker, "client_order_id": coid, "action": "buy",
            "side": side, "type": "limit", "count": count,
        }
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        try:
            data = self._req("POST", "/portfolio/orders", body=body)
        except KalshiRejected as e:
            return PlaceResult("rejected", client_order_id=coid,
                               reason=f"{e.status_code}")
        except KalshiUnavailable as e:
            # The order may or may not be resting. Caller MUST reconcile.
            return PlaceResult("unknown", client_order_id=coid, reason=str(e))
        oid = (data.get("order") or {}).get("order_id")
        if oid:
            return PlaceResult("placed", order_id=oid, client_order_id=coid)
        return PlaceResult("unknown", client_order_id=coid,
                           reason="no order_id in response")

    def cancel_order(self, order_id: str) -> str:
        """Returns 'cancelled' | 'gone' (already filled/cancelled) |
        'unavailable' (unknown state — order may still be live)."""
        try:
            self._req("DELETE", f"/portfolio/orders/{order_id}")
            return "cancelled"
        except KalshiRejected as e:
            # 404 / already-terminal — the order is no longer resting.
            return "gone" if e.status_code in (404, 400) else "gone"
        except KalshiUnavailable:
            return "unavailable"

    def cancel_all(self):
        """Best-effort cancel of every resting order.
        Returns (cancelled, failed). failed > 0 means orders may remain."""
        try:
            orders = self.get_open_orders()
        except KalshiError:
            log.warning("cancel_all: could not list open orders")
            return 0, -1
        ok = fail = 0
        for o in orders:
            res = self.cancel_order(o.get("order_id", ""))
            if res == "unavailable":
                fail += 1
            else:
                ok += 1
        log.info(f"cancel_all: cancelled {ok}/{len(orders)} (failed {fail})")
        return ok, fail


def kalshi_fee_cents(price_cents: int, count: int) -> float:
    """Kalshi TAKER fee: ceil(0.07 * C * P * (1-P)) to next cent. Returns cents."""
    p = price_cents / 100.0
    fee_dollars = 0.07 * count * p * (1 - p)
    return float(math.ceil(round(fee_dollars * 100, 6)))


def kalshi_maker_fee_cents(price_cents: int, count: int,
                           mult: float = 0.25) -> float:
    """Kalshi MAKER fee: ceil(mult * 0.07 * C * P * (1-P)) to next cent —
    published as round-up(0.0175 x C x P x (1-P)) at the default mult.
    The ceil applies to the maker product itself; scaling the ceil'd taker
    fee misprices small fills (1x @ 50c is 1c, not 0.5c). Returns cents."""
    p = price_cents / 100.0
    fee_dollars = mult * 0.07 * count * p * (1 - p)
    return float(math.ceil(round(fee_dollars * 100, 6)))
