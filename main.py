"""Kalshi passive maker — main loop.

scan -> price -> quote -> monitor -> re-quote on moves -> hard risk gates.

Operating principles (items A2-A4):
  * NEVER treat "API unavailable" as "flat". If we can't verify our own
    orders/positions this loop, we go DEGRADED: no new quotes, no state
    changes, Discord alert, retry next loop.
  * Settlement polling is GATED until priming succeeds, so a restart can
    never count old settlements into today's P&L.
  * A watchdog thread alerts Discord if the loop stalls >5 min, and when
    the Odds API quota runs hot.

KILL=1 -> paper mode (item C15): full pipeline, simulated quotes/fills
against real prints, everything recorded with is_sim=1.
"""
import datetime
import json
import logging
import signal
import sys
import threading
import time

import config as C
import store
from kalshi_client import (KalshiClient, KalshiError, KalshiUnavailable,
                           norm_order)
from fair_value import fetch_games, cache_age, quota
from matcher import match_markets
from notify import notify, install_log_scrubber
from quoter import (Risk, desired_quotes, clamp_to_book, cooldowns,
                    maker_fee_cents, minutes_to_commence, is_steaming,
                    best_bid)
from dashboard import start_dashboard, STATS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
install_log_scrubber()
log = logging.getLogger("maker")

HEARTBEAT = {"ts": time.time()}


def _watchdog():
    """Item A4: stall + quota alerts, independent of the main loop."""
    last_stall_alert = 0.0
    quota_alerted_day = ""
    while True:
        time.sleep(30)
        now = time.time()
        silent = now - HEARTBEAT["ts"]
        if silent > C.WATCHDOG_STALL_SECS and \
                now - last_stall_alert > C.WATCHDOG_REALERT_SECS:
            last_stall_alert = now
            notify(f"🚨 WATCHDOG: main loop silent for {silent/60:.1f} min "
                   "— check Railway logs/deploy status")
        rem = quota.get("remaining")
        today = datetime.date.today().isoformat()
        if rem is not None and rem < C.QUOTA_ALERT_REMAINING \
                and quota_alerted_day != today:
            quota_alerted_day = today
            notify(f"⚠️ Odds API quota running hot: {rem:.0f} credits left "
                   "this cycle. Raise ODDS_CACHE_SECS or upgrade the plan.")


class Maker:
    def __init__(self):
        # DB must exist before anything touches it — PaperEngine reads the
        # settlements table during construction (crash-loop bug otherwise
        # on fresh volumes / pre-v4 databases).
        store.init_db()
        self.client = KalshiClient()
        self.risk = Risk()
        self.live: dict = {}          # order_id -> {ticker, side, price, count, fair}
        self.last_fill_ts = int(time.time())
        self._settle_log = []
        self.settlements_primed = False
        self.fair_by_ticker: dict = {}   # ticker -> {fair_yes, commence}
        self.day = store.today()
        self._degraded_notified = False
        self._halt_notified = False
        self._unknown_order_pending = False
        self._sim_loss_alerted = False
        self._was_paused = False
        self.paper = None
        if C.PAPER_MODE:
            from paper import PaperEngine
            self.paper = PaperEngine()

    # ── startup safety (item A3) ───────────────────────────────────────
    def prime_settlements(self):
        """Mark pre-existing settlements as seen WITHOUT counting their P&L.
        MANDATORY before any settlement is allowed to hit the books: if
        priming fails, poll_settlements stays disabled and we retry each
        loop — a restart can never pollute today's numbers."""
        try:
            settlements = self.client.get_settlements(limit=200)
        except KalshiError as e:
            log.warning(f"settlement priming failed ({e}) — settlement "
                        "polling DISABLED until priming succeeds")
            return
        n = 0
        for s in settlements:
            sid = s.get("ticker", "") + str(s.get("settled_time", ""))
            if store.record_settlement(
                    sid, s.get("ticker", "?"), s.get("market_result", ""),
                    s.get("revenue", 0),
                    s.get("yes_total_cost", 0) + s.get("no_total_cost", 0),
                    pnl=0, settled_at=str(s.get("settled_time", ""))):
                n += 1
        self.settlements_primed = True
        log.info(f"primed {n} pre-existing settlements (not counted in P&L)")

    def reconcile_from(self, orders: list):
        """Rebuild tracked order state from the exchange's answer. Runs
        every loop (we fetch open orders anyway for exposure). Fair values
        on kept orders are refreshed in sync_quotes, so the re-quote
        trigger is re-armed within one loop of any restart."""
        fresh = {}
        for o in orders:
            oid = o.get("order_id")
            side, px, cnt = norm_order(o)   # handles legacy AND V2 shapes
            fresh[oid] = {
                "ticker": o.get("ticker"), "side": side, "price": px,
                "count": cnt,
                "fair": (self.live.get(oid) or {}).get("fair"),
                "placed_at": (self.live.get(oid) or {}).get("placed_at", 0),
            }
        # Grace window: keep tracking orders WE placed in the last ~2 loops
        # even if the exchange listing hasn't caught up yet — dropping them
        # here would let us double-quote the same market.
        cutoff = time.time() - 2 * C.LOOP_SECS
        for oid, o in self.live.items():
            if oid not in fresh and o.get("placed_at", 0) > cutoff:
                fresh[oid] = o
        self.live = fresh
        self._unknown_order_pending = False

    # ── fills (item B9: real-time alerts with fair + edge) ─────────────
    def side_fair(self, ticker: str, side: str):
        f = self.fair_by_ticker.get(ticker)
        if not f:
            return None
        return f["fair_yes"] if side == "yes" else 100 - f["fair_yes"]

    def poll_fills(self):
        fills = self.client.get_fills(min_ts=max(0, self.last_fill_ts - 120))
        for f in fills:
            fid = f.get("trade_id") or f.get("fill_id") or ""
            side, px, cnt = norm_order(f)   # fills use the same dialects
            tkr = f.get("ticker", "?")
            fair = self.side_fair(tkr, side)
            edge = (fair - px) if fair is not None else None
            fee = maker_fee_cents(px, cnt)
            if not store.record_fill(fid, tkr, side, px, cnt,
                                     fair_at_fill=fair, edge_at_fill=edge,
                                     fee_cents=fee):
                continue
            e_txt = f" | fair {fair:.1f}¢ | edge {edge:+.1f}¢" if fair is not None else ""
            notify(f"🟢 FILL {tkr} — {cnt}x {side.upper()} @ {px}¢{e_txt}")
            cooldowns[(tkr, side)] = time.time() + C.FILL_COOLDOWN_SECS
            oid = f.get("order_id")
            if oid in self.live:
                self.live[oid]["count"] -= cnt
                if self.live[oid]["count"] <= 0:
                    del self.live[oid]
        self.last_fill_ts = int(time.time())

    # ── settlements -> realized P&L + maker CLV (item B7) ──────────────
    def poll_settlements(self):
        if not self.settlements_primed:
            self.prime_settlements()   # keep retrying; counting stays off
            return
        settlements = self.client.get_settlements(limit=50)
        for s in settlements:
            sid = s.get("ticker", "") + str(s.get("settled_time", ""))
            tkr = s.get("ticker", "?")
            rev = s.get("revenue", 0)
            cost = s.get("yes_total_cost", 0) + s.get("no_total_cost", 0)
            pnl = rev - cost
            if not store.record_settlement(sid, tkr, s.get("market_result", ""),
                                           rev, cost, pnl,
                                           str(s.get("settled_time", ""))):
                continue
            store.add_realized(pnl)
            clv_rows = store.compute_clv_for_ticker(tkr)
            clv_txt = ""
            if clv_rows:
                avg = sum(r["edge_vs_close"] * r["count"] for r in clv_rows) \
                    / max(sum(r["count"] for r in clv_rows), 1)
                clv_txt = f" | CLV {avg:+.1f}¢/contract vs close"
            emoji = "✅" if pnl >= 0 else "❌"
            notify(f"{emoji} SETTLED {tkr} — P&L {pnl/100:+.2f}${clv_txt}")
            self._settle_log.append({"ticker": tkr, "pnl_cents": pnl,
                                     "at": datetime.datetime.now().strftime("%m-%d %H:%M")})
            self._settle_log = self._settle_log[-50:]

    # ── quote maintenance ──────────────────────────────────────────────
    def build_want(self, targets, inventory: dict) -> dict:
        want = {}
        for t in targets:
            # record fair for every matched target pre-game (CLV close mark)
            if minutes_to_commence(t["commence"]) > 0:
                self.fair_by_ticker[t["ticker"]] = {
                    "fair_yes": t["fair_prob"] * 100, "commence": t["commence"]}
                store.record_fair_value(t["ticker"], t["fair_prob"],
                                        t.get("n_books", 0), t.get("sharp", False),
                                        t.get("uncertainty", 0.0), t["commence"])
            for q in desired_quotes(t, uncertainty=t.get("uncertainty", 0.0),
                                    net_position=inventory.get(t["ticker"])):
                want[(t["ticker"], q["side"])] = {**q, "ticker": t["ticker"]}
        return want

    def _book(self, ticker, cache: dict):
        if ticker not in cache:
            try:
                cache[ticker] = self.client.get_orderbook(ticker, C.ORDERBOOK_DEPTH)
            except KalshiError:
                cache[ticker] = None
        return cache[ticker]

    def sync_quotes(self, want: dict, exposure: int):
        # Clamp/step EVERY wanted quote against the live book FIRST, so the
        # keep-vs-reprice comparison uses the price we would actually post.
        # (Comparing resting clamped prices to unclamped wishes caused an
        # every-loop cancel/repost churn on any clamped market.) The
        # queue step-up lives inside clamp_to_book, so being outbid
        # surfaces naturally as a reprice.
        books: dict = {}
        eff: dict = {}
        for key, q in want.items():
            q2, ctx = clamp_to_book(q, self._book(q["ticker"], books))
            if q2 is None:
                store.record_quote_event("skip", "", q["ticker"], q["side"],
                                         q["price"], q["count"], fair=q["fair"],
                                         reason=json.dumps(ctx))
                continue
            q2["_ctx"] = ctx
            eff[key] = q2

        # cancel: no longer wanted / fair moved / price no longer right
        for oid, o in list(self.live.items()):
            key = (o["ticker"], o["side"])
            w = eff.get(key)
            stale = w is None
            moved = (w is not None and o.get("fair") is not None
                     and abs(w["fair"] - o["fair"]) >= C.REQUOTE_MOVE)
            mispriced = (w is not None
                         and abs(w["price"] - o["price"]) > C.PRICE_TOLERANCE)
            if stale or moved or mispriced:
                res = self.client.cancel_order(oid)
                if res == "unavailable":
                    # Order may STILL BE LIVE. Keep tracking it — deleting it
                    # here would let us double-quote the market (old bug).
                    store.record_quote_event("cancel_failed", oid, o["ticker"],
                                             o["side"], o["price"], o["count"],
                                             reason="api unavailable")
                    continue
                store.record_quote_event(
                    "cancel", oid, o["ticker"], o["side"], o["price"],
                    o["count"], fair=o.get("fair"),
                    reason="stale" if stale else "moved" if moved else "reprice")
                del self.live[oid]
            elif w is not None:
                # Restart safety (item A3): keeping this order — refresh its
                # fair so the move trigger stays armed (fair is None after
                # restarts until this line runs).
                o["fair"] = w["fair"]

        # post whatever is missing
        have = {(o["ticker"], o["side"]) for o in self.live.values()}
        for key, q2 in eff.items():
            if key in have:
                continue
            ctx = q2.pop("_ctx", {})
            cost = q2["price"] * q2["count"]
            if exposure + cost > C.TOTAL_CAP:
                continue
            res = self.client.place_limit(q2["ticker"], q2["side"],
                                          q2["price"], q2["count"])
            if res.status == "placed":
                self.live[res.order_id] = {
                    "ticker": q2["ticker"], "side": q2["side"],
                    "price": q2["price"], "count": q2["count"],
                    "fair": q2["fair"], "placed_at": time.time()}
                exposure += cost
                store.record_quote_event("post", res.order_id, q2["ticker"],
                                         q2["side"], q2["price"], q2["count"],
                                         fair=q2["fair"], edge=q2["edge"],
                                         reason=json.dumps(ctx))
                log.info(f"posted {q2['ticker']} {q2['side']} {q2['count']}x "
                         f"@ {q2['price']}c (fair {q2['fair']:.1f}, "
                         f"edge {q2['edge']:.1f}c, queue_ahead "
                         f"{ctx.get('queue_ahead')})")
            elif res.status == "unknown":
                # Sent but unconfirmed — it MAY be resting untracked. Stop
                # posting, reconcile from the exchange next loop.
                self._unknown_order_pending = True
                store.record_quote_event("post_unknown", res.client_order_id,
                                         q2["ticker"], q2["side"], q2["price"],
                                         q2["count"], reason=res.reason)
                log.warning("order outcome unknown — pausing posts until reconcile")
                break
            else:
                store.record_quote_event("reject", res.client_order_id,
                                         q2["ticker"], q2["side"], q2["price"],
                                         q2["count"], reason=res.reason)

    def paper_cycle(self, want: dict):
        """Item C15: sim quotes + fills against real prints."""
        books: dict = {}
        clamped = {}
        for key, q in want.items():
            q2, _ctx = clamp_to_book(q, self._book(q["ticker"], books))
            if q2 is not None:
                clamped[key] = q2
        self.paper.sync(clamped)
        for f in self.paper.poll_trades(self.client):
            e_txt = f" | fair {f['fair']:.1f}¢ | edge {f['edge']:+.1f}¢" \
                if f.get("fair") is not None else ""
            notify(f"📝 SIM FILL {f['ticker']} — {f['count']}x "
                   f"{f['side'].upper()} @ {f['price']}¢{e_txt}")
        for s in self.paper.check_settlements(self.client):
            emoji = "✅" if s["pnl_cents"] >= 0 else "❌"
            notify(f"{emoji} SIM SETTLED {s['ticker']} ({s['result'].upper()}) "
                   f"— P&L {s['pnl_cents']/100:+.2f}$")
            self._settle_log.append({"ticker": "SIM " + s["ticker"],
                                     "pnl_cents": s["pnl_cents"],
                                     "at": datetime.datetime.now().strftime("%m-%d %H:%M")})
        sim_today = store.day_realized(is_sim=True)
        if -sim_today >= C.DAILY_LOSS_LIMIT and not self._sim_loss_alerted:
            self._sim_loss_alerted = True
            notify(f"⚠️ PAPER: simulated daily loss ${-sim_today/100:.2f} "
                   "crossed the live loss limit — sim continues for data.")

    # ── daily summary (item B8) ────────────────────────────────────────
    def maybe_daily_summary(self, equity):
        today = store.today()
        if today == self.day:
            return
        prev, self.day = self.day, today
        self._sim_loss_alerted = False
        for is_sim, label in ((False, ""), (True, " (PAPER)")):
            s = store.daily_summary(prev, is_sim=is_sim)
            if not s["fills"] and not s["gross_pnl_cents"]:
                continue
            dd = max(0, C.START_BANKROLL - equity) if equity is not None else None
            lines = [
                f"📊 Daily summary {prev}{label}",
                f"Fills: {s['fills']} ({s['contracts']} contracts, "
                f"${s['volume_cents']/100:.2f} volume)",
                f"Gross P&L: ${s['gross_pnl_cents']/100:+.2f} | "
                f"Fees: ${s['fees_cents']/100:.2f}",
            ]
            if s["avg_edge_at_fill"] is not None:
                lines.append(f"Edge at fill: {s['avg_edge_at_fill']:+.1f}¢ avg")
            if s["avg_edge_vs_close"] is not None:
                lines.append(f"Edge vs close (maker CLV): "
                             f"{s['avg_edge_vs_close']:+.1f}¢ avg")
            if dd is not None and not is_sim:
                lines.append(f"Drawdown from start bankroll: ${dd/100:.2f}")
            notify("\n".join(lines))

    # ── dashboard stats (item B10) ─────────────────────────────────────
    def update_stats(self, balance=None, exposure=0, positions=None,
                     n_games=0, status="running", reason="", targets=None):
        try:
            con = store.db()
            total = con.execute(
                "SELECT COALESCE(SUM(realized),0) FROM pnl_days").fetchone()[0]
            con.close()
            inv = []
            if self.paper:
                inv = self.paper.inventory()
            else:
                for p in positions or []:
                    net = p.get("position", 0)
                    if net or p.get("market_exposure"):
                        inv.append({"ticker": p.get("ticker", "?"), "net": net,
                                    "cost_cents": abs(p.get("market_exposure", 0))})
            is_sim = bool(self.paper)
            open_q = ([{"ticker": o["ticker"], "side": o["side"],
                        "price": o["price"], "count": o["count"],
                        "fair": o.get("fair")}
                       for o in (self.paper.orders if self.paper
                                 else self.live).values()])
            tgt_rows = []
            for t in (targets or []):
                mins = minutes_to_commence(t["commence"])
                if mins < -240:
                    continue
                state = ("pulled" if mins < C.PULL_MIN else
                         "steam" if is_steaming(t["ticker"]) else
                         "skip-unc" if t.get("uncertainty", 0) >= C.UNC_SKIP
                         else "quoting")
                tgt_rows.append({
                    "ticker": t["ticker"], "fair": t["fair_prob"] * 100,
                    "uncertainty": (t.get("uncertainty") or 0) * 100,
                    "mins": int(mins), "n_books": t.get("n_books", 0),
                    "state": state})
            tgt_rows.sort(key=lambda r: r["mins"])
            STATS.update({
                "edge": store.edge_summary(7, is_sim=is_sim),
                "targets": tgt_rows[:30],
                "quota_remaining": quota.get("remaining"),
                "loop_secs": getattr(self, "loop_sleep", C.LOOP_SECS),
                "caps": {"per_market": C.PER_MARKET_CAP,
                         "total": C.TOTAL_CAP,
                         "daily_loss": C.DAILY_LOSS_LIMIT,
                         "drawdown": C.DRAWDOWN_LIMIT,
                         "start_bankroll": C.START_BANKROLL},
                "status": status, "mode": "paper" if self.paper else "live",
                "halted": status == "halted", "halt_reason": reason,
                "balance_cents": balance, "exposure_cents": exposure,
                "equity_cents": (balance + exposure) if balance is not None else None,
                "today_realized_cents": store.day_realized(is_sim=is_sim),
                "total_realized_cents": total,
                "open_quotes": open_q,
                "recent_fills": store.fill_history(20, is_sim=is_sim),
                "recent_settles": self._settle_log[-12:][::-1],
                "inventory": inv,
                "pnl_history": store.pnl_history(14, is_sim=is_sim),
                "n_markets_quoted": len({q["ticker"] for q in open_q}),
                "fair_games": n_games,
                "updated": datetime.datetime.now().strftime("%H:%M:%S"),
            })
        except Exception:
            log.exception("stats update failed")

    # ── main loop ──────────────────────────────────────────────────────
    def run(self):
        store.init_db()
        start_dashboard()
        threading.Thread(target=_watchdog, daemon=True).start()
        self.prime_settlements()
        if self.paper:
            ok, fail = self.client.cancel_all()   # KILL safety: flatten real book
            notify(f"📝 Kalshi maker in PAPER MODE (KILL=1) — simulating "
                   f"quotes & fills, recording to DB. Cancelled {ok} real orders.")
        else:
            notify("🤖 Kalshi maker LIVE — caps: "
                   f"${C.PER_MARKET_CAP/100:.0f}/mkt, ${C.TOTAL_CAP/100:.0f} total, "
                   f"${C.DAILY_LOSS_LIMIT/100:.0f} daily stop, "
                   f"${C.DRAWDOWN_LIMIT/100:.0f} drawdown kill")
        while True:
            HEARTBEAT["ts"] = time.time()
            try:
                self.loop_once()
            except KalshiUnavailable as e:
                # Item A2: cannot verify our own state — PAUSE, don't guess.
                log.warning(f"degraded: {e}")
                if not self._degraded_notified:
                    self._degraded_notified = True
                    notify(f"🟠 DEGRADED: Kalshi API unreachable ({e}). "
                           "Quoting paused — existing orders left untouched "
                           "until state can be verified.")
                self.update_stats(status="degraded", reason=str(e))
            except Exception:
                log.exception("loop error — cancelling all as a precaution")
                try:
                    self.client.cancel_all()
                    self.live.clear()
                except Exception:
                    pass
            time.sleep(getattr(self, "loop_sleep", C.LOOP_SECS))

    def loop_once(self):
        # 1. Verify our own state from the exchange (raises -> degraded)
        balance = self.client.get_balance()
        orders = self.client.get_open_orders()
        positions = self.client.get_positions()
        if self._degraded_notified:
            self._degraded_notified = False
            notify("🟢 RECOVERED: Kalshi API reachable again — resuming.")
        self.reconcile_from(orders)
        exposure = Risk.exposure_cents(orders, positions)
        equity = balance + exposure
        self.maybe_daily_summary(equity)

        # v4.6: dashboard pause switch (meta flag in DB, flipped via
        # /api/pause). Pause = cancel everything, stop quoting, keep
        # recording fills/settlements so nothing goes unaccounted.
        if store.meta_get("paused") == "1":
            if not self._was_paused:
                self._was_paused = True
                if self.paper:
                    self.paper.sync({})
                else:
                    ok, fail = self.client.cancel_all()
                    self.live.clear()
                notify("⏸️ PAUSED from dashboard — quotes cancelled. "
                       "Fills/settlements still tracked.")
            if not self.paper:
                self.poll_fills()
                self.poll_settlements()
            self.update_stats(balance, exposure, positions, status="paused")
            return
        if self._was_paused:
            self._was_paused = False
            notify("▶️ RESUMED from dashboard — quoting restarts this loop.")

        # 2. Risk gates (live mode)
        if not self.paper:
            verdict = self.risk.check(balance, exposure,
                                      store.day_realized())
            if verdict == "halt":
                if not self._halt_notified:
                    self._halt_notified = True
                    notify(f"🛑 HALTED: {self.risk.halt_reason} — "
                           "cancelling all quotes")
                ok, fail = self.client.cancel_all()
                if fail:
                    notify(f"⚠️ HALT: {fail} cancels unconfirmed — orders may "
                           "remain resting. Will retry next loop.")
                self.live.clear()
                self.update_stats(balance, exposure, positions,
                                  status="halted", reason=self.risk.halt_reason)
                time.sleep(30)
                return
            self._halt_notified = False

        # 3. Fair values
        games = fetch_games()
        if cache_age() > C.STALE_FAIR_SECS or not games:
            log.warning("fair values stale/empty — pulling all quotes")
            if self.paper:
                self.paper.sync({})
            else:
                self.client.cancel_all()
                self.live.clear()
            self.update_stats(balance, exposure, positions, n_games=0,
                              status="stale-fair")
            return

        # 4. Match + build desired quotes
        markets = self.client.get_markets(series_ticker=C.KALSHI_SERIES)
        if not markets and not getattr(self, "_warned_series", False):
            self._warned_series = True
            notify(f"⚠️ No open markets found for series '{C.KALSHI_SERIES}'. "
                   "Check KALSHI_SERIES env var against kalshi.com tickers.")
        targets = match_markets(markets, games)

        if self.paper:
            inventory = {i["ticker"]: {"net": i["net"], "cost": i["cost_cents"]}
                         for i in self.paper.inventory()}
        else:
            inventory = {p.get("ticker"): {"net": p.get("position", 0),
                                           "cost": abs(p.get("market_exposure", 0))}
                         for p in positions}
        want = self.build_want(targets, inventory)

        # adaptive cadence: near first pitch, stale quotes are pick-off
        # bait — tighten the loop (Kalshi read budget easily allows it)
        soonest = min((minutes_to_commence(t["commence"]) for t in targets
                       if minutes_to_commence(t["commence"]) > 0),
                      default=1e9)
        self.loop_sleep = C.FAST_LOOP_SECS if soonest <= C.FAST_WINDOW_MIN \
            else C.LOOP_SECS

        # 5. Act
        if self.paper:
            self.paper_cycle(want)
        else:
            if self._unknown_order_pending:
                log.info("unknown-order flag set — reconciled above, resuming")
            self.sync_quotes(want, exposure)
            self.poll_fills()
            self.poll_settlements()
        self.update_stats(balance, exposure, positions, n_games=len(games),
                          targets=targets)


def main():
    maker = Maker()

    def shutdown(signum, frame):
        if not maker.paper:
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
