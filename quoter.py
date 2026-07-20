"""Quoting logic + risk manager. All money numbers in cents unless noted.

Pricing pipeline per (market, side):
  1. fee-aware price solver: highest price clearing the edge floor
  2. time widening      — extra margin inside WIDEN_MIN; pull inside PULL_MIN
  3. uncertainty filter — widen when books disagree; skip when they really do
  4. inventory skew     — shade the long side away, tighten the flattening
                          side (relaxed edge floor) to attract the offset
  5. orderbook clamp    — never cross the spread; record queue context
All constants live in config.py.
"""
import datetime
import logging
import math
import time

import config as C
from kalshi_client import (kalshi_fee_cents, kalshi_maker_fee_cents,
                           norm_order, position_exposure_cents)

log = logging.getLogger("quote")

# (ticker, side) -> unix ts until which we won't re-quote (set on fills)
cooldowns: dict = {}

# ── penny-war damper: don't escalate against faster bots ──────────────
_step_hist: dict = {}    # (ticker, side) -> [ts of step-up posts]
_war_until: dict = {}    # (ticker, side) -> unix ts


def note_step(ticker: str, side: str):
    """Called when a step-up quote actually posts. Repeated step-ups on
    one market/side in a short window = someone re-pennying us; a war of
    ticks against a faster bot only bleeds edge and queue position."""
    now = time.time()
    key = (ticker, side)
    hist = [t for t in _step_hist.get(key, []) if now - t < C.PENNY_WAR_WINDOW]
    hist.append(now)
    _step_hist[key] = hist
    if len(hist) >= C.PENNY_WAR_MAX:
        _war_until[key] = now + C.PENNY_WAR_PAUSE
        log.info(f"penny war on {ticker} {side} — standing down "
                 f"{C.PENNY_WAR_PAUSE}s (resting deep)")


def in_penny_war(ticker: str, side: str) -> bool:
    return _war_until.get((ticker, side), 0) > time.time()


# ── steam guard: pause a market while fair is moving fast ──────────────
_fair_hist: dict = {}   # ticker -> list[(ts, fair_yes_cents)]
_steam_until: dict = {} # ticker -> unix ts


def is_steaming(ticker: str) -> bool:
    return _steam_until.get(ticker, 0) > time.time()


def steam_check(ticker: str, fair_yes_cents: float) -> bool:
    """Record fair, return True if the market is 'steaming' (fair moved
    >= STEAM_MOVE_CENTS within STEAM_WINDOW_SECS) — quotes should pause.
    Lineup posts and pitcher scratches move MLB lines 20-30c in minutes;
    a resting quote during that window is pure pick-off bait."""
    now = time.time()
    hist = _fair_hist.setdefault(ticker, [])
    hist.append((now, fair_yes_cents))
    cutoff = now - C.STEAM_WINDOW_SECS
    _fair_hist[ticker] = hist = [(t, f) for t, f in hist if t >= cutoff]
    if _steam_until.get(ticker, 0) > now:
        return True
    lo = min(f for _, f in hist)
    hi = max(f for _, f in hist)
    if hi - lo >= C.STEAM_MOVE_CENTS:
        _steam_until[ticker] = now + C.STEAM_PAUSE_SECS
        log.info(f"steam guard: {ticker} fair moved {hi-lo:.1f}c in "
                 f"{C.STEAM_WINDOW_SECS}s — pausing {C.STEAM_PAUSE_SECS}s")
        return True
    return False


# ── helpers ────────────────────────────────────────────────────────────
def contracts_for_cap(price_cents: int, cap_cents: int) -> int:
    if price_cents <= 0:
        return 0
    return max(0, cap_cents // price_cents)


def minutes_to_commence(commence_iso: str) -> float:
    try:
        dt = datetime.datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
        return (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
    except Exception:
        return -1


def maker_fee_cents(price_cents: int, count: int) -> float:
    return kalshi_maker_fee_cents(price_cents, count, C.MAKER_FEE_MULT)


def inventory_skew_cents(net_cost_cents: int) -> int:
    """Shading, scaled by how much of the per-market cap the position uses."""
    if not net_cost_cents:
        return 0
    frac = min(1.0, abs(net_cost_cents) / max(C.PER_MARKET_CAP, 1))
    return int(round(frac * C.MAX_INV_SKEW_CENTS))


def _solve_price(fair: float, margin: int, min_edge: int):
    """Highest price with fair - price - maker_fee(price) >= min_edge,
    also respecting the hard margin floor. Returns (price, edge) or None."""
    price = int(math.floor(fair - min_edge - maker_fee_cents(int(max(fair, 1)), 1)))
    for _ in range(3):
        better = int(math.floor(fair - min_edge - maker_fee_cents(max(price, 1), 1)))
        if better == price:
            break
        price = better
    price = min(price, int(math.floor(fair)) - margin)
    if price < 2 or price > 97:
        return None
    edge = fair - price - maker_fee_cents(price, 1)
    if edge < min_edge:
        return None
    return price, edge


# ── quote construction (items C11, C12, C13) ───────────────────────────
def desired_quotes(target, uncertainty: float = 0.0,
                   net_position: dict = None) -> list:
    _is_ladder = bool(target.get("ladder"))
    """For one matched market, return the quotes we want resting.

    target: {ticker, fair_prob, commence, ...}
    uncertainty: stddev of book disagreement (prob points 0-1)
    net_position: {"net": contracts (+long YES / -long NO), "cost": cents}
    """
    fair_yes = target["fair_prob"] * 100  # cents
    mins = minutes_to_commence(target["commence"])

    # C13: pull entirely inside PULL_MIN; never quote too far out
    if mins < C.PULL_MIN or mins > C.MAX_HOURS_OUT * 60:
        return []

    # steam guard: fair moving fast = information flow = pick-off risk
    if steam_check(target["ticker"], fair_yes):
        return []

    # C11: skip when books genuinely disagree — our fair is a guess there
    if uncertainty >= C.UNC_SKIP:
        return []

    extra = 0
    if mins < C.WIDEN_MIN:                  # C13: widen approaching first pitch
        extra += C.TIME_WIDEN_CENTS
    if uncertainty >= C.UNC_WIDEN:          # C11: widen on shaky consensus
        extra += C.UNC_WIDEN_CENTS

    net = (net_position or {}).get("net", 0)
    skew = inventory_skew_cents((net_position or {}).get("cost", 0))

    now = time.time()
    quotes = []
    for side in ("yes", "no"):
        if cooldowns.get((target["ticker"], side), 0) > now:
            continue
        fair = fair_yes if side == "yes" else (100 - fair_yes)

        # C12: inventory skew — long YES? shade YES away, tighten NO (and
        # vice versa). The flattening side also gets a relaxed edge floor:
        # cutting inventory risk is worth accepting less edge.
        if _is_ladder:
            margin, min_edge = C.LADDER_MARGIN + extra, C.LADDER_MIN_EDGE
        else:
            margin, min_edge = C.MARGIN_CENTS + extra, C.MIN_EDGE_CENTS
        if net > 0:      # long YES
            if side == "yes":
                margin += skew
            else:
                margin = max(1, margin - skew)
                min_edge = C.OFFSET_MIN_EDGE
        elif net < 0:    # long NO
            if side == "no":
                margin += skew
            else:
                margin = max(1, margin - skew)
                min_edge = C.OFFSET_MIN_EDGE

        # Favorite-longshot bias: Kalshi longshot prices are systematically
        # rich (documented on 300K+ contracts). If our bid lands in longshot
        # territory, demand extra edge — the "cheap" side is the trap side.
        if fair <= C.LONGSHOT_CENTS + C.MARGIN_CENTS:
            min_edge += C.LONGSHOT_EXTRA_EDGE

        solved = _solve_price(fair, margin, min_edge)
        if not solved:
            continue
        price, edge = solved

        side_cap = (C.PER_MARKET_CAP * C.NO_SIZE_PCT // 100 if side == "no"
                    else C.PER_MARKET_CAP * (100 - C.NO_SIZE_PCT) // 100)
        count = contracts_for_cap(price, side_cap)
        if count < 1:
            continue
        quotes.append({"side": side, "price": price, "count": count,
                       "fair": fair, "edge": edge})
    return quotes


# ── orderbook awareness (item C14) ─────────────────────────────────────
def best_bid(levels) -> int:
    """levels: [[price, count], ...] resting bids on one side."""
    return max((lv[0] for lv in (levels or []) if lv), default=0)


def clamp_to_book(quote: dict, orderbook: dict):
    """Adjust a quote against the live book. Returns (quote|None, context).

    - Never cross: our bid must stay below the implied ask
      (implied YES ask = 100 - best NO bid, and vice versa).
    - If clamping kills the edge, drop the quote.
    - context records queue position: contracts already resting at our
      price level (ahead of us in FIFO) and the current best bid.
    """
    if not orderbook:
        return quote, {"book": "unavailable"}
    yes_levels = orderbook.get("yes") or []
    no_levels = orderbook.get("no") or []
    side = quote["side"]
    own_levels = yes_levels if side == "yes" else no_levels
    opp_best = best_bid(no_levels if side == "yes" else yes_levels)
    implied_ask = 100 - opp_best if opp_best else 100

    q = dict(quote)
    clamped = False
    if q["price"] >= implied_ask:           # would cross / take instantly
        q["price"] = implied_ask - 1
        clamped = True
        if q["price"] < 2:
            return None, {"dropped": "no room under implied ask"}
        q["edge"] = q["fair"] - q["price"] - maker_fee_cents(q["price"], 1)
        if q["edge"] < C.OFFSET_MIN_EDGE:
            return None, {"dropped": "clamp killed edge"}

    # v5.0: profitable pennying. If someone bids at/above us:
    #   JUMP one tick over them only when the depth ahead is worth a full
    #   tick of edge (long queue = expensive wait). Otherwise JOIN their
    #   level and keep the tick (short queues clear fast). Never do
    #   either while a penny war is on — rest deep and let it rage.
    stepped = joined = False
    ob = best_bid(own_levels)
    if (C.JOIN_BEST and ob >= q["price"]
            and not in_penny_war(quote.get("ticker", ""), side)):
        ahead_at_best = sum(lv[1] for lv in own_levels if lv and lv[0] == ob)
        jump_px = min(ob + 1, implied_ask - 1)
        jump_edge = q["fair"] - jump_px - maker_fee_cents(jump_px, 1)
        join_edge = q["fair"] - ob - maker_fee_cents(ob, 1)
        if (ahead_at_best >= C.JUMP_QUEUE_MIN and jump_px > q["price"]
                and jump_edge >= C.JOIN_MIN_EDGE):
            q["price"], q["edge"], stepped = jump_px, jump_edge, True
        elif (ob > q["price"] and ob < implied_ask
                and join_edge >= C.JOIN_MIN_EDGE):
            q["price"], q["edge"], joined = ob, join_edge, True

    ahead = sum(lv[1] for lv in own_levels if lv and lv[0] == q["price"])
    ctx = {"best_bid": best_bid(own_levels), "implied_ask": implied_ask,
           "queue_ahead": ahead, "clamped": clamped, "stepped": stepped,
           "joined": joined}
    return q, ctx


# ── risk gates (item A2: tri-state — never confuse outage with flat) ───
class Risk:
    def __init__(self):
        self.halted = False
        self.halt_reason = ""

    @staticmethod
    def exposure_cents(open_orders: list, positions: list) -> int:
        """Cash tied up = resting orders + open position cost basis.
        Caller supplies exchange-verified lists (raises upstream if the
        API is down — an outage must never read as zero exposure)."""
        total = 0
        for o in open_orders:
            _side, px, cnt = norm_order(o)
            total += px * cnt
        for p in positions:
            total += position_exposure_cents(p)
        return total

    def check(self, balance: int, exposure: int, today_realized: int) -> str:
        """Returns 'ok' or 'halt' (sets halt_reason). Inputs are verified
        numbers fetched by the caller this loop."""
        equity = balance + exposure
        if C.START_BANKROLL - equity >= C.DRAWDOWN_LIMIT:
            self.halted = True
            self.halt_reason = f"DRAWDOWN KILL: equity ${equity/100:.2f} vs start ${C.START_BANKROLL/100:.2f}"
            return "halt"
        if -today_realized >= C.DAILY_LOSS_LIMIT:
            self.halted = True
            self.halt_reason = f"DAILY LOSS STOP: ${today_realized/100:.2f} today"
            return "halt"
        self.halted = False
        self.halt_reason = ""
        return "ok"
