"""Kalshi passive maker — main loop.

scan -> price -> quote -> monitor -> re-quote on moves -> hard risk gates.
"""
import datetime
import logging
import os
import signal
import sqlite3
import sys
import time

import requests

from kalshi_client import KalshiClient
from fair_value import fetch_games, cache_age
from matcher import match_markets
import quoter
from quoter import Risk, desired_quotes, init_db, db, REQUOTE_MOVE, cooldowns, FILL_COOLDOWN_SECS
from dashboard import start_dashboard, STATS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("maker")

SERIES = os.environ.get("KALSHI_SERIES", "KXMLBGAME")
LOOP_SECS = int(os.environ.get("LOOP_SECS", "30"))
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
STALE_FAIR_SECS = int(os.environ.get("STALE_FAIR_SECS", "240"))


def notify(msg: str):
    log.info(f"NOTIFY: {msg}")
    if not DISCORD_WEBHOOK:
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg[:1900]}, timeout=10)
    except requests.RequestException:
        pass


class Maker:
    def __init__(self):
        self.client = KalshiClient()
        self.risk = Risk(self.client)
        # order_id -> {ticker, side, price, count, fair}
        self.live: dict = {}
        self.last_fill_ts = int(time.time())
        self._settle_log = []

    # ── startup reconcile ──────────────────────────────────────────────
    def prime_settlements(self):
        """Mark pre-existing settlements as seen WITHOUT counting their P&L,
        so a restart or a pre-used account never pollutes today's numbers."""
        data = self.client._req("GET", "/portfolio/settlements", params={"limit": 100})
        if not data:
            return
        con = db()
        n = 0
        for s in data.get("settlements", []):
            sid = s.get("ticker", "") + str(s.get("settled_time", ""))
            cur = con.execute(
                "INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?)",
                (sid, s.get("ticker", "?"), "settle", 0, 0,
                 datetime.datetime.utcnow().isoformat()))
            n += cur.rowcount
        con.commit()
        con.close()
        log.info(f"primed {n} pre-existing settlements (not counted in P&L)")

    def reconcile(self):
        fresh = {}
        for o in self.client.get_open_orders():
            oid = o.get("order_id")
            side = o.get("side") or ("yes" if o.get("yes_price") else "no")
            px = o.get("yes_price") if side == "yes" else o.get("no_price")
            fresh[oid] = {
                "ticker": o.get("ticker"),
                "side": side,
                "price": px or 0,
                "count": o.get("remaining_count") or o.get("count") or 0,
                "fair": (self.live.get(oid) or {}).get("fair"),
            }
        self.live = fresh
        log.info(f"reconciled {len(self.live)} resting orders from exchange")

    # ── fills ──────────────────────────────────────────────────────────
    def poll_fills(self):
        # overlap window: never miss fills that landed mid-request; DB dedupes
        fills = self.client.get_fills(min_ts=max(0, self.last_fill_ts - 120))
        if not fills:
            return
        con = db()
        for f in fills:
            fid = f.get("trade_id") or f.get("fill_id") or ""
            exists = con.execute("SELECT 1 FROM fills WHERE fill_id=?", (fid,)).fetchone()
            if exists:
                continue
            side = f.get("side", "?")
            px = f.get("yes_price") if side == "yes" else f.get("no_price")
            cnt = f.get("count", 0)
            tkr = f.get("ticker", "?")
            con.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?)",
                        (fid, tkr, side, px or 0, cnt,
                         datetime.datetime.utcnow().isoformat()))
            notify(f"🟢 FILL {tkr} — bought {cnt}x {side.upper()} @ {px}¢")
            # adverse-selection guard: pause re-quoting this (ticker, side)
            cooldowns[(tkr, side)] = time.time() + FILL_COOLDOWN_SECS
            oid = f.get("order_id")
            if oid in self.live:
                self.live[oid]["count"] -= cnt
                if self.live[oid]["count"] <= 0:
                    del self.live[oid]
        con.commit()
        con.close()
        self.last_fill_ts = int(time.time())

    # ── settlements → realized P&L ─────────────────────────────────────
    def poll_settlements(self):
        # Positions that disappear settled; simplest robust proxy:
        # track via balance delta is noisy, so record settlements from API
        data = self.client._req("GET", "/portfolio/settlements", params={"limit": 50})
        if not data:
            return
        con = db()
        for s in data.get("settlements", []):
            sid = s.get("ticker", "") + str(s.get("settled_time", ""))
            exists = con.execute("SELECT 1 FROM fills WHERE fill_id=?", (sid,)).fetchone()
            if exists:
                continue
            rev = s.get("revenue", 0)  # cents credited
            cost = s.get("yes_total_cost", 0) + s.get("no_total_cost", 0)
            pnl = rev - cost
            con.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?)",
                        (sid, s.get("ticker", "?"), "settle", 0, 0,
                         datetime.datetime.utcnow().isoformat()))
            self.risk.record_realized(pnl)
            emoji = "✅" if pnl >= 0 else "❌"
            notify(f"{emoji} SETTLED {s.get('ticker','?')} — P&L {pnl/100:+.2f}$")
            self._settle_log.append({
                "ticker": s.get("ticker", "?"), "pnl_cents": pnl,
                "at": datetime.datetime.now().strftime("%m-%d %H:%M")})
            if len(self._settle_log) > 50:
                self._settle_log = self._settle_log[-50:]
        con.commit()
        con.close()

    # ── quote maintenance ──────────────────────────────────────────────
    def sync_quotes(self, targets):
        # index desired quotes per (ticker, side)
        want = {}
        for t in targets:
            for q in desired_quotes(t):
                want[(t["ticker"], q["side"])] = {**q, "ticker": t["ticker"]}

        # cancel: orders no longer wanted, or fair moved >= REQUOTE_MOVE
        for oid, o in list(self.live.items()):
            key = (o["ticker"], o["side"])
            w = want.get(key)
            stale = w is None
            moved = (w is not None and o.get("fair") is not None
                     and abs(w["fair"] - o["fair"]) >= REQUOTE_MOVE)
            mispriced = w is not None and w["price"] != o["price"]
            if stale or moved or mispriced:
                self.client.cancel_order(oid)
                # remove regardless; periodic reconcile heals any drift and a
                # cancel that failed because the order filled is handled by
                # poll_fills
                del self.live[oid]
                time.sleep(0.12)
            elif w is not None:
                # keeping this order: refresh its fair so the move trigger
                # stays armed (fair is None after restarts)
                o["fair"] = w["fair"]

        # exposure gate before posting new
        exposure = self.risk.exposure_cents()

        # post missing quotes
        have = {(o["ticker"], o["side"]) for o in self.live.values()}
        for key, q in want.items():
            if key in have:
                continue
            cost = q["price"] * q["count"]
            if exposure + cost > quoter.TOTAL_CAP:
                continue
            oid = self.client.place_limit(q["ticker"], q["side"], q["price"], q["count"])
            if oid:
                self.live[oid] = {"ticker": q["ticker"], "side": q["side"],
                                  "price": q["price"], "count": q["count"],
                                  "fair": q["fair"]}
                exposure += cost
                log.info(f"posted {q['ticker']} {q['side']} {q['count']}x @ {q['price']}c "
                         f"(fair {q['fair']:.1f}, edge {q['edge']:.1f}c)")
            time.sleep(0.15)

    def update_stats(self, n_games=0, halted=False, reason=""):
        try:
            bal = self.client.get_balance()
            exp = self.risk.exposure_cents()
            con = db()
            total = con.execute("SELECT COALESCE(SUM(realized),0) FROM pnl_days").fetchone()[0]
            fills = con.execute(
                "SELECT ticker, side, price, count, filled_at FROM fills "
                "WHERE side IN ('yes','no') ORDER BY filled_at DESC LIMIT 12").fetchall()
            con.close()
            settles = [f for f in self._settle_log[-12:]][::-1]
            STATS.update({
                "status": "halted" if halted else "running",
                "halted": halted, "halt_reason": reason,
                "balance_cents": bal, "exposure_cents": exp,
                "equity_cents": (bal + exp) if bal is not None else None,
                "today_realized_cents": self.risk.today_realized(),
                "total_realized_cents": total,
                "open_quotes": [
                    {"ticker": o["ticker"], "side": o["side"], "price": o["price"],
                     "count": o["count"], "fair": o.get("fair")}
                    for o in self.live.values()],
                "recent_fills": [
                    {"ticker": f[0], "side": f[1], "price": f[2], "count": f[3],
                     "at": (f[4] or "")[:16].replace("T", " ")} for f in fills],
                "recent_settles": settles,
                "n_markets_quoted": len({o["ticker"] for o in self.live.values()}),
                "fair_games": n_games,
                "updated": datetime.datetime.now().strftime("%H:%M:%S"),
            })
        except Exception:
            log.exception("stats update failed")

    # ── main loop ──────────────────────────────────────────────────────
    def run(self):
        init_db()
        start_dashboard()
        self.reconcile()
        self.prime_settlements()
        notify("🤖 Kalshi maker online — caps: "
               f"${quoter.PER_MARKET_CAP/100:.0f}/mkt, ${quoter.TOTAL_CAP/100:.0f} total, "
               f"${quoter.DAILY_LOSS_LIMIT/100:.0f} daily stop")
        halted_notified = False
        loop_n = 0
        while True:
            loop_n += 1
            try:
                if loop_n % 20 == 0:
                    self.reconcile()
                if not self.risk.check():
                    if not halted_notified:
                        notify(f"🛑 HALTED: {self.risk.halt_reason} — cancelling all quotes")
                        halted_notified = True
                    self.client.cancel_all()
                    self.live.clear()
                    self.update_stats(halted=True, reason=self.risk.halt_reason)
                    time.sleep(60)
                    continue
                halted_notified = False

                games = fetch_games()
                if cache_age() > STALE_FAIR_SECS or not games:
                    log.warning("fair values stale/empty — cancelling all quotes")
                    self.client.cancel_all()
                    self.live.clear()
                    self.update_stats(n_games=0, halted=False, reason="")
                    time.sleep(LOOP_SECS)
                    continue

                markets = self.client.get_markets(series_ticker=SERIES)
                if not markets and not getattr(self, "_warned_series", False):
                    self._warned_series = True
                    notify(f"⚠️ No open markets found for series '{SERIES}'. "
                           f"If this persists, check KALSHI_SERIES env var against "
                           f"kalshi.com market tickers.")
                targets = match_markets(markets, games)
                self.sync_quotes(targets)
                self.poll_fills()
                self.poll_settlements()
                self.update_stats(n_games=len(games))

            except Exception:
                log.exception("loop error — cancelling all as a precaution")
                try:
                    self.client.cancel_all()
                    self.live.clear()
                except Exception:
                    pass
            time.sleep(LOOP_SECS)


def main():
    maker = Maker()

    def shutdown(signum, frame):
        notify("⚠️ Maker shutting down — cancelling all resting orders")
        try:
            maker.client.cancel_all()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    maker.run()


if __name__ == "__main__":
    main()
