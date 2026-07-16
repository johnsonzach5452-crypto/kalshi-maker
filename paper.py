"""Paper-trading engine (item C15). Active when KILL=1.

Runs the exact same pricing pipeline as live mode, but instead of sending
orders it maintains simulated resting quotes and fills them against REAL
market prints: when a taker sells through our simulated bid's price level,
we assume we'd have been filled (we were the better bid, so FIFO would hit
us first). Conservative in the right direction — it never fills us on
trades that wouldn't have reached our price.

Everything is recorded to the same SQLite tables with is_sim=1, so a
KILL-on dry run produces a full simulated P&L + maker-CLV dataset.
"""
import logging
import time
import uuid

import store
from kalshi_client import KalshiError
from quoter import maker_fee_cents

log = logging.getLogger("paper")


class PaperEngine:
    def __init__(self):
        # sim_id -> {ticker, side, price, count, fair}
        self.orders: dict = {}
        # ticker -> {"yes": [count, cost], "no": [count, cost]}
        self.positions: dict = {}
        self.last_trade_ts = int(time.time())
        self._seen_trades: set = set()
        self._rebuild_positions()

    def _rebuild_positions(self):
        """Survive restarts: rebuild open sim positions from recorded sim
        fills, excluding tickers whose sim settlement is already recorded."""
        con = store.db()
        settled = {r[0] for r in con.execute(
            "SELECT ticker FROM settlements WHERE is_sim=1")}
        rows = con.execute(
            "SELECT ticker, side, price, count FROM fills "
            "WHERE is_sim=1 AND side IN ('yes','no')").fetchall()
        con.close()
        for tkr, side, price, count in rows:
            if tkr in settled:
                continue
            pos = self.positions.setdefault(tkr, {"yes": [0, 0], "no": [0, 0]})
            pos[side][0] += count
            pos[side][1] += price * count
        if self.positions:
            log.info(f"paper: rebuilt {len(self.positions)} open sim positions")

    # ── quote sync (mirrors live sync_quotes) ──────────────────────────
    def sync(self, want: dict):
        """want: {(ticker, side): quote_dict} — same shape as live mode."""
        for sid, o in list(self.orders.items()):
            key = (o["ticker"], o["side"])
            w = want.get(key)
            if w is None or w["price"] != o["price"]:
                store.record_quote_event("cancel", sid, o["ticker"], o["side"],
                                         o["price"], o["count"],
                                         reason="sim resync", is_sim=True)
                del self.orders[sid]
            else:
                o["fair"] = w["fair"]

        have = {(o["ticker"], o["side"]) for o in self.orders.values()}
        for key, q in want.items():
            if key in have:
                continue
            sid = "sim-" + str(uuid.uuid4())[:13]
            self.orders[sid] = {"ticker": q["ticker"], "side": q["side"],
                                "price": q["price"], "count": q["count"],
                                "fair": q["fair"]}
            store.record_quote_event("post", sid, q["ticker"], q["side"],
                                     q["price"], q["count"], fair=q["fair"],
                                     edge=q.get("edge"), reason="sim",
                                     is_sim=True)

    # ── fills from real prints ─────────────────────────────────────────
    def poll_trades(self, client) -> list:
        """Check real trades against sim bids. A YES bid at p fills when a
        taker SELLS yes (taker_side='no') at yes_price <= p; symmetric for
        NO bids. Returns list of sim-fill dicts for alerting."""
        tickers = {o["ticker"] for o in self.orders.values()}
        out = []
        min_ts = max(0, self.last_trade_ts - 90)
        for tkr in tickers:
            try:
                trades = client.get_trades(tkr, min_ts=min_ts)
            except KalshiError as e:
                log.warning(f"paper: trades fetch failed for {tkr}: {e}")
                continue
            for t in trades:
                tid = t.get("trade_id") or ""
                if not tid or tid in self._seen_trades:
                    continue
                self._seen_trades.add(tid)
                taker = t.get("taker_side")
                yes_px = t.get("yes_price") or 0
                cnt = t.get("count") or 0
                if not taker or not cnt:
                    continue
                # taker sold YES -> hits our YES bids; taker sold NO -> NO bids
                hit_side = "yes" if taker == "no" else "no"
                trade_px = yes_px if hit_side == "yes" else 100 - yes_px
                remaining = cnt
                for sid, o in sorted(self.orders.items(),
                                     key=lambda kv: -kv[1]["price"]):
                    if remaining <= 0:
                        break
                    if o["ticker"] != tkr or o["side"] != hit_side:
                        continue
                    if o["price"] < trade_px:
                        continue  # print never reached our level
                    take = min(remaining, o["count"])
                    out.append(self._fill(sid, o, take, tid))
                    remaining -= take
        if len(self._seen_trades) > 5000:
            self._seen_trades = set(list(self._seen_trades)[-2500:])
        self.last_trade_ts = int(time.time())
        return out

    def _fill(self, sid, o, count, trade_id) -> dict:
        fee = maker_fee_cents(o["price"], count)
        edge = (o["fair"] - o["price"]) if o.get("fair") is not None else None
        fid = f"simfill-{trade_id}-{sid[-6:]}"
        store.record_fill(fid, o["ticker"], o["side"], o["price"], count,
                          fair_at_fill=o.get("fair"), edge_at_fill=edge,
                          fee_cents=fee, is_sim=True)
        pos = self.positions.setdefault(o["ticker"],
                                        {"yes": [0, 0], "no": [0, 0]})
        pos[o["side"]][0] += count
        pos[o["side"]][1] += o["price"] * count
        # fee is charged at execution: book it into sim P&L now
        store.add_realized(-int(round(fee)), is_sim=True)
        o["count"] -= count
        filled = {"ticker": o["ticker"], "side": o["side"], "price": o["price"],
                  "count": count, "fair": o.get("fair"), "edge": edge}
        if o["count"] <= 0:
            del self.orders[sid]
        return filled

    # ── settlement of sim positions ────────────────────────────────────
    def check_settlements(self, client) -> list:
        """Poll markets we hold sim positions in; when one settles, realize
        sim P&L, record it, and score maker CLV. Returns settled summaries."""
        out = []
        for tkr in list(self.positions.keys()):
            try:
                m = client.get_market(tkr)
            except KalshiError:
                continue
            status = (m.get("status") or "").lower()
            result = (m.get("result") or "").lower()
            if status not in ("settled", "finalized") or result not in ("yes", "no"):
                continue
            pos = self.positions.pop(tkr)
            pnl = 0
            for side in ("yes", "no"):
                n, cost = pos[side]
                if n <= 0:
                    continue
                pnl += (n * 100 - cost) if side == result else -cost
            sid = f"sim-{tkr}-{result}"
            if store.record_settlement(sid, tkr, result, revenue=0, cost=0,
                                       pnl=pnl, settled_at=store.now_iso(),
                                       is_sim=True):
                store.add_realized(pnl, is_sim=True)
                store.compute_clv_for_ticker(tkr, is_sim=True)
                out.append({"ticker": tkr, "result": result, "pnl_cents": pnl})
        return out

    def inventory(self) -> list:
        out = []
        for tkr, pos in self.positions.items():
            net = pos["yes"][0] - pos["no"][0]
            cost = pos["yes"][1] + pos["no"][1]
            out.append({"ticker": tkr, "net": net, "cost_cents": cost})
        return out
