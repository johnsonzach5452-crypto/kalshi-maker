"""Quoting logic + risk manager. All money numbers in cents unless noted."""
import datetime
import logging
import math
import os
import sqlite3
import time

from kalshi_client import kalshi_fee_cents

log = logging.getLogger("quote")

# ── risk config (env-overridable) ──────────────────────────────────────
MARGIN_CENTS      = int(os.environ.get("MARGIN_CENTS", "4"))      # post fair - 4c
MIN_EDGE_CENTS    = int(os.environ.get("MIN_EDGE_CENTS", "3"))    # after fees
PER_MARKET_CAP    = int(os.environ.get("PER_MARKET_CAP", "2000")) # $20
TOTAL_CAP         = int(os.environ.get("TOTAL_CAP", "40000"))     # $400
DAILY_LOSS_LIMIT  = int(os.environ.get("DAILY_LOSS_LIMIT", "6000"))   # $60
DRAWDOWN_LIMIT    = int(os.environ.get("DRAWDOWN_LIMIT", "20000"))    # $200
START_BANKROLL    = int(os.environ.get("START_BANKROLL", "50000"))    # $500
CUTOFF_MIN        = int(os.environ.get("CUTOFF_MIN", "30"))   # stop quoting 30m pre-game
MAX_HOURS_OUT     = int(os.environ.get("MAX_HOURS_OUT", "48"))
REQUOTE_MOVE      = int(os.environ.get("REQUOTE_MOVE", "2"))  # re-quote if fair moves 2c

# Maker fee = 25% of taker formula on the few series that charge makers at all;
# most Kalshi markets charge resting orders nothing. 0.25 is the conservative default.
MAKER_FEE_MULT    = float(os.environ.get("MAKER_FEE_MULT", "0.25"))
# Retail systematically overbets YES (Stanford, 41.6M-trade study), so the
# behavioral surplus concentrates on the NO side. Skew size accordingly.
NO_SIZE_PCT       = int(os.environ.get("NO_SIZE_PCT", "60"))   # % of per-market cap to NO
# After a fill, pause re-quoting that (ticker, side): the fill itself is
# information that fair may be moving through us (adverse selection guard).
FILL_COOLDOWN_SECS = int(os.environ.get("FILL_COOLDOWN_SECS", "600"))

DB_PATH = os.environ.get("MAKER_DB", "/data/maker.db")
if not os.path.isdir(os.path.dirname(DB_PATH) or "."):
    logging.getLogger("quote").warning(
        f"{os.path.dirname(DB_PATH)} not mounted — falling back to ./maker.db "
        "(state will NOT survive redeploys; mount a Railway volume at /data)")
    DB_PATH = "./maker.db"


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS quotes (
        order_id TEXT PRIMARY KEY, ticker TEXT, side TEXT,
        price INTEGER, count INTEGER, fair_at_post REAL,
        posted_at TEXT, status TEXT DEFAULT 'resting')""")
    con.execute("""CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY, ticker TEXT, side TEXT,
        price INTEGER, count INTEGER, filled_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS pnl_days (
        day TEXT PRIMARY KEY, realized INTEGER DEFAULT 0)""")
    con.commit()
    con.close()


def db():
    return sqlite3.connect(DB_PATH)


# ── sizing ─────────────────────────────────────────────────────────────
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
    return kalshi_fee_cents(price_cents, count) * MAKER_FEE_MULT


# (ticker, side) -> unix ts until which we won't re-quote (set on fills)
cooldowns: dict = {}


def desired_quotes(target) -> list:
    """For one matched market, return the quotes we want resting.
    Two-sided: bid YES below fair, bid NO below fair-of-NO.
    NO side gets more size (retail overbets YES; surplus is on NO)."""
    fair_yes = target["fair_prob"] * 100  # cents
    mins = minutes_to_commence(target["commence"])
    if mins < CUTOFF_MIN or mins > MAX_HOURS_OUT * 60:
        return []

    now = time.time()
    quotes = []
    for side in ("yes", "no"):
        if cooldowns.get((target["ticker"], side), 0) > now:
            continue
        fair = fair_yes if side == "yes" else (100 - fair_yes)
        # Solve: highest price with fair - price - maker_fee(price) >= MIN_EDGE
        price = int(math.floor(fair - MIN_EDGE_CENTS - maker_fee_cents(int(fair), 1)))
        for _ in range(3):
            better = int(math.floor(fair - MIN_EDGE_CENTS - maker_fee_cents(max(price, 1), 1)))
            if better == price:
                break
            price = better
        # never post closer than MARGIN_CENTS to fair, even if fees are tiny
        price = min(price, int(math.floor(fair)) - MARGIN_CENTS)
        if price < 2 or price > 97:
            continue
        side_cap = (PER_MARKET_CAP * NO_SIZE_PCT // 100 if side == "no"
                    else PER_MARKET_CAP * (100 - NO_SIZE_PCT) // 100)
        count = contracts_for_cap(price, side_cap)
        if count < 1:
            continue
        fee_per = maker_fee_cents(price, 1)
        edge = fair - price - fee_per
        if edge < MIN_EDGE_CENTS:
            continue
        quotes.append({"side": side, "price": price, "count": count,
                       "fair": fair, "edge": edge})
    return quotes


# ── risk gates ─────────────────────────────────────────────────────────
class Risk:
    def __init__(self, client):
        self.client = client
        self.start_balance = None
        self.halted = False
        self.halt_reason = ""

    def today(self):
        return datetime.date.today().isoformat()

    def record_realized(self, delta_cents: int):
        con = db()
        con.execute("INSERT INTO pnl_days(day, realized) VALUES(?,0) "
                    "ON CONFLICT(day) DO NOTHING", (self.today(),))
        con.execute("UPDATE pnl_days SET realized = realized + ? WHERE day = ?",
                    (delta_cents, self.today()))
        con.commit()
        con.close()

    def today_realized(self) -> int:
        con = db()
        row = con.execute("SELECT realized FROM pnl_days WHERE day=?",
                          (self.today(),)).fetchone()
        con.close()
        return row[0] if row else 0

    def exposure_cents(self) -> int:
        """Cash tied up = resting orders + open position cost basis."""
        total = 0
        for o in self.client.get_open_orders():
            side = o.get("side") or ("yes" if o.get("yes_price") else "no")
            px = (o.get("yes_price") if side == "yes" else o.get("no_price")) or 0
            total += px * (o.get("remaining_count") or o.get("count") or 0)
        for p in self.client.get_positions():
            total += abs(p.get("market_exposure", 0))
        return total

    def check(self) -> bool:
        """Return True if trading allowed. Sets halted+reason otherwise."""
        if os.environ.get("KILL", "") == "1":
            self.halted, self.halt_reason = True, "KILL env var set"
            return False
        bal = self.client.get_balance()
        if bal is None:
            self.halted, self.halt_reason = True, "balance unavailable"
            return False
        if self.start_balance is None:
            self.start_balance = bal
        # total drawdown vs configured start bankroll
        equity = bal + self.exposure_cents()
        if START_BANKROLL - equity >= DRAWDOWN_LIMIT:
            self.halted, self.halt_reason = True, f"drawdown limit hit (equity {equity}c)"
            return False
        if -self.today_realized() >= DAILY_LOSS_LIMIT:
            self.halted, self.halt_reason = True, "daily loss limit hit"
            return False
        self.halted = False
        return True
