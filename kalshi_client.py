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


def _load_private_key(raw: str):
    """Accept the key in any of the formats Railway users end up with:
    raw multiline PEM, PEM with literal \\n escapes, or a base64-encoded
    single-line PEM (the standard workaround for Railway's variable editor
    mangling multiline values)."""
    raw = (raw or "").strip()
    if "-----BEGIN" not in raw:
        try:
            raw = base64.b64decode(raw).decode()
        except Exception:
            raise RuntimeError(
                "KALSHI_PRIVATE_KEY is neither a PEM nor base64-encoded PEM")
    pem = raw.replace("\\n", "\n").encode()
    try:
        return serialization.load_pem_private_key(pem, password=None)
    except ValueError as e:
        raise RuntimeError(f"KALSHI_PRIVATE_KEY failed to load: {e}") from e


class KalshiClient:
    def __init__(self):
        if not KEY_ID or not PRIVATE_KEY_PEM:
            raise RuntimeError("KALSHI_KEY_ID and KALSHI_PRIVATE_KEY env vars required")
        self._key = _load_private_key(PRIVATE_KEY_PEM)
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
        """Normalize both dialects to {'yes': [[cents,count],...], 'no': ...}.
        V2 returns orderbook_fp with dollar-string levels; the bot was
        book-blind for a night because of this."""
        data = self._req("GET", f"/markets/{ticker}/orderbook",
                         params={"depth": depth})

        def conv(levels):
            out = []
            for lv in levels or []:
                try:
                    out.append([int(round(float(lv[0]) * 100)),
                                int(float(lv[1]))])
                except (TypeError, ValueError, IndexError):
                    pass
            return out

        ob = data.get("orderbook") or {}
        if ob.get("yes") or ob.get("no"):
            return ob                     # legacy integer-cent shape
        fp = data.get("orderbook_fp") or ob
        if fp:
            return {"yes": conv(fp.get("yes_dollars") or fp.get("yes")),
                    "no": conv(fp.get("no_dollars") or fp.get("no"))}
        return {}

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
    # Kalshi deprecated POST/DELETE /portfolio/orders (410 Gone) in favor
    # of the event-order surface at /portfolio/events/orders (May 2026
    # changelog). We hit the new surface first and fall back to legacy so
    # the client works on either side of the migration.
    ORDER_PATHS = ("/portfolio/events/orders", "/portfolio/orders")

    def place_limit(self, ticker: str, side: str, price_cents: int,
                    count: int) -> PlaceResult:
        """side: 'yes' or 'no'. price_cents 1-99. count = # contracts.

        V2 event-order surface speaks book language: side is bid/ask on
        YES, prices are fixed-point dollar strings, counts are strings.
        Buying NO at q cents == asking YES at (100-q) cents. post_only
        guarantees we can never accidentally take (we are a maker)."""
        coid = str(uuid.uuid4())
        if side == "yes":
            book_side, px = "bid", price_cents / 100.0
        else:
            book_side, px = "ask", (100 - price_cents) / 100.0
        v2_body = {
            "ticker": ticker, "client_order_id": coid,
            "side": book_side,
            "count": str(int(count)),
            "price": f"{px:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": True,
            "cancel_order_on_pause": True,
        }
        legacy_body = {
            "ticker": ticker, "client_order_id": coid, "action": "buy",
            "side": side, "type": "limit", "count": count,
            ("yes_price" if side == "yes" else "no_price"): price_cents,
        }
        last = ""
        for path, body in (("/portfolio/events/orders", v2_body),
                           ("/portfolio/orders", legacy_body)):
            try:
                data = self._req("POST", path, body=body)
            except KalshiRejected as e:
                if e.status_code in (404, 410):   # wrong surface — try next
                    last = f"{e.status_code} on {path}"
                    continue
                return PlaceResult("rejected", client_order_id=coid,
                                   reason=f"{e.status_code}: {e.body[:160]}")
            except KalshiUnavailable as e:
                # The order may or may not be resting. Caller MUST reconcile.
                return PlaceResult("unknown", client_order_id=coid,
                                   reason=str(e))
            order = data.get("order") or (data.get("orders") or [None])[0] or data
            oid = order.get("order_id") if isinstance(order, dict) else None
            if oid:
                return PlaceResult("placed", order_id=oid,
                                   client_order_id=coid)
            return PlaceResult("unknown", client_order_id=coid,
                               reason="no order_id in response")
        return PlaceResult("rejected", client_order_id=coid,
                           reason=f"order endpoints unavailable ({last})")

    def cancel_order(self, order_id: str) -> str:
        """Returns 'cancelled' | 'gone' (already filled/cancelled) |
        'unavailable' (unknown state — order may still be live)."""
        for path in self.ORDER_PATHS:
            try:
                self._req("DELETE", f"{path}/{order_id}")
                return "cancelled"
            except KalshiRejected as e:
                if e.status_code in (404, 410):   # not on this surface
                    continue
                return "gone"                     # 400 etc: terminal state
            except KalshiUnavailable:
                return "unavailable"
        return "gone"   # found on neither surface -> no longer resting

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


# ── response normalization: legacy ints AND V2 dollar-strings ─────────
def _num_cents(obj: dict, cents_key: str, dollars_key: str) -> int:
    """Read a money field that may be legacy int cents or V2 fixed-point
    dollar string ('0.4400'). Returns cents (int)."""
    v = obj.get(cents_key)
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    d = obj.get(dollars_key)
    if d is not None:
        try:
            return int(round(float(d) * 100))
        except (TypeError, ValueError):
            pass
    return 0


def norm_order(o: dict):
    """-> (side 'yes'/'no', price_cents, remaining_count) from an order in
    ANY of Kalshi's response dialects (legacy yes/no + int cents, or V2
    bid/ask + dollar strings + *_fp counts)."""
    # V2 trap: orders carry a legacy 'side' field that can read "yes"
    # even on NO orders. Authority order: outcome_side (the truth) >
    # book_side (bid=buy YES, ask=buy NO) > side > price-field inference.
    s = o.get("outcome_side")
    if s not in ("yes", "no"):
        bs = o.get("book_side") or ""
        s = {"bid": "yes", "ask": "no"}.get(bs)
    if s not in ("yes", "no"):
        s = o.get("side")
        if s == "bid":
            s = "yes"
        elif s == "ask":
            s = "no"
    if s not in ("yes", "no"):
        s = "yes" if (o.get("yes_price") is not None
                      or o.get("yes_price_dollars") is not None) else "no"
    if s == "yes":
        px = _num_cents(o, "yes_price", "yes_price_dollars")
    else:
        px = _num_cents(o, "no_price", "no_price_dollars")
        if not px:   # V2 ask orders are YES-denominated: no = 100 - yes
            ypx = _num_cents(o, "yes_price", "yes_price_dollars")
            if ypx:
                px = 100 - ypx
    cnt = 0
    for k in ("remaining_count", "count"):
        if o.get(k) is not None:
            try:
                cnt = int(o[k]); break
            except (TypeError, ValueError):
                pass
    if not cnt:
        for k in ("count_fp", "remaining_count_fp", "initial_count_fp"):
            if o.get(k) is not None:
                try:
                    cnt = int(float(o[k])); break
                except (TypeError, ValueError):
                    pass
    return s, px, cnt


def fill_fee_cents(f: dict):
    """Kalshi reports the actual fee on each fill (fee_cost, dollars).
    Prefer it over our estimate — WNBA maker fills come back 0."""
    for k in ("fee_cost", "fee_paid_dollars", "maker_fees_dollars"):
        if f.get(k) is not None:
            try:
                return round(float(f[k]) * 100, 2)
            except (TypeError, ValueError):
                pass
    return None


def position_exposure_cents(p: dict) -> int:
    return abs(_num_cents(p, "market_exposure", "market_exposure_dollars"))


def norm_position(p: dict):
    """-> (ticker, net_contracts, exposure_cents) across dialects.
    V2 uses position_fp (fixed-point) and market_exposure_dollars; net is
    +long YES / -long NO."""
    net = 0
    for k in ("position", "position_fp"):
        if p.get(k) is not None:
            try:
                net = int(float(p[k]))
                break
            except (TypeError, ValueError):
                pass
    return p.get("ticker"), net, position_exposure_cents(p)


def norm_settlement(s: dict):
    """-> (revenue_cents, cost_cents, fee_cents) across dialects.

    V2 returns revenue as int CENTS but costs/fees as DOLLAR STRINGS
    ('47.845200'). The old integer keys (yes_total_cost) are gone, so
    reading them yielded 0 -- wins were booked at full payout and losses
    at zero. This normalizer is the fix.
    """
    rev = _num_cents(s, "revenue", "revenue_dollars")
    cost = (_num_cents(s, "yes_total_cost", "yes_total_cost_dollars")
            + _num_cents(s, "no_total_cost", "no_total_cost_dollars"))
    fee = _num_cents(s, "fee_paid", "fee_cost")
    return rev, cost, fee


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
