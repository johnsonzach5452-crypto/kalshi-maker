"""SQLite recording layer. Every odds snapshot, fair value, quote event,
fill, and settlement lands here, timestamped. This dataset is the product.

Real and simulated (paper-mode) activity share tables, separated by an
is_sim flag, so the same analysis queries work on both.
"""
import datetime
import json
import logging
import sqlite3
import time

from config import DB_PATH

log = logging.getLogger("store")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.date.today().isoformat()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _columns(con, table: str) -> set:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def init_db():
    con = db()
    # legacy tables (keep — existing volume data + settlement dedupe live here)
    con.execute("""CREATE TABLE IF NOT EXISTS quotes (
        order_id TEXT PRIMARY KEY, ticker TEXT, side TEXT,
        price INTEGER, count INTEGER, fair_at_post REAL,
        posted_at TEXT, status TEXT DEFAULT 'resting')""")
    con.execute("""CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY, ticker TEXT, side TEXT,
        price INTEGER, count INTEGER, filled_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS pnl_days (
        day TEXT PRIMARY KEY, realized INTEGER DEFAULT 0)""")

    # migrations on fills: fee/edge/sim columns
    cols = _columns(con, "fills")
    for name, ddl in [("fair_at_fill", "REAL"), ("edge_at_fill", "REAL"),
                      ("fee_cents", "REAL"), ("is_sim", "INTEGER DEFAULT 0"),
                      ("is_taker", "INTEGER DEFAULT 0")]:
        if name not in cols:
            con.execute(f"ALTER TABLE fills ADD COLUMN {name} {ddl}")

    # measurement tables
    con.execute("""CREATE TABLE IF NOT EXISTS odds_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, home TEXT, away TEXT,
        commence TEXT, home_prob REAL, away_prob REAL, n_books INTEGER,
        sharp INTEGER, uncertainty REAL, books_json TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS fair_values (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ticker TEXT,
        fair_yes REAL, n_books INTEGER, sharp INTEGER, uncertainty REAL,
        commence TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fv_ticker ON fair_values(ticker, ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS quote_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, event TEXT,
        order_id TEXT, ticker TEXT, side TEXT, price INTEGER, count INTEGER,
        fair REAL, edge REAL, reason TEXT, is_sim INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS settlements (
        sid TEXT PRIMARY KEY, ticker TEXT, result TEXT,
        revenue_cents INTEGER, cost_cents INTEGER, pnl_cents INTEGER,
        settled_at TEXT, recorded_at TEXT, is_sim INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS maker_clv (
        fill_id TEXT PRIMARY KEY, ticker TEXT, side TEXT, price INTEGER,
        count INTEGER, fair_at_fill REAL, fair_at_close REAL,
        edge_at_fill REAL, edge_vs_close REAL, computed_at TEXT,
        is_sim INTEGER DEFAULT 0)""")
    if "is_taker" not in _columns(con, "maker_clv"):
        con.execute("ALTER TABLE maker_clv ADD COLUMN is_taker INTEGER DEFAULT 0")
    con.execute("""CREATE TABLE IF NOT EXISTS pnl_days_sim (
        day TEXT PRIMARY KEY, realized INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT)""")
    con.commit()
    con.close()
    log.info(f"db ready at {DB_PATH}")


# ── writers ────────────────────────────────────────────────────────────
_last_fv: dict = {}  # ticker -> (ts, fair) throttle


def record_odds_snapshot(games: list):
    if not games:
        return
    con = db()
    ts = now_iso()
    for g in games:
        con.execute(
            "INSERT INTO odds_snapshots(ts,home,away,commence,home_prob,"
            "away_prob,n_books,sharp,uncertainty,books_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, g["home"], g["away"], g["commence"], g["home_prob"],
             g["away_prob"], g["n_books"], int(g.get("sharp", False)),
             g.get("uncertainty"), json.dumps(g.get("books", []))))
    con.commit()
    con.close()


def record_fair_value(ticker, fair_yes, n_books, sharp, uncertainty, commence):
    """Throttled: write when fair moved >=0.25c or >5 min since last write.
    The LAST row per ticker doubles as 'final pre-game fair' for maker CLV."""
    prev = _last_fv.get(ticker)
    now = time.time()
    if prev and abs(prev[1] - fair_yes) < 0.25 and now - prev[0] < 300:
        return
    _last_fv[ticker] = (now, fair_yes)
    con = db()
    con.execute("INSERT INTO fair_values(ts,ticker,fair_yes,n_books,sharp,"
                "uncertainty,commence) VALUES(?,?,?,?,?,?,?)",
                (now_iso(), ticker, fair_yes, n_books, int(sharp),
                 uncertainty, commence))
    con.commit()
    con.close()


def record_quote_event(event, order_id, ticker, side, price, count,
                       fair=None, edge=None, reason="", is_sim=False):
    con = db()
    con.execute("INSERT INTO quote_events(ts,event,order_id,ticker,side,"
                "price,count,fair,edge,reason,is_sim) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (now_iso(), event, order_id, ticker, side, price, count,
                 fair, edge, reason, int(is_sim)))
    con.commit()
    con.close()


def record_fill(fill_id, ticker, side, price, count, fair_at_fill=None,
                edge_at_fill=None, fee_cents=None, is_sim=False,
                is_taker=False) -> bool:
    """Insert a fill if new. Returns True if it was new.

    is_taker separates MANUAL trades (you clicking buy = taker, crossing
    the spread) from BOT trades (resting orders = maker). Mixing them
    corrupts every strategy metric, so they're tagged at the source.
    """
    con = db()
    cur = con.execute(
        "INSERT OR IGNORE INTO fills(fill_id,ticker,side,price,count,"
        "filled_at,fair_at_fill,edge_at_fill,fee_cents,is_sim,is_taker) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (fill_id, ticker, side, price, count, now_iso(),
         fair_at_fill, edge_at_fill, fee_cents, int(is_sim), int(is_taker)))
    new = cur.rowcount > 0
    con.commit()
    con.close()
    return new


def record_settlement(sid, ticker, result, revenue, cost, pnl,
                      settled_at, is_sim=False) -> bool:
    """Record a settlement. Also inserts the legacy fills-table sentinel so
    dedupe stays compatible with pre-upgrade databases. Returns True if new."""
    con = db()
    seen = con.execute("SELECT 1 FROM fills WHERE fill_id=?", (sid,)).fetchone() \
        or con.execute("SELECT 1 FROM settlements WHERE sid=?", (sid,)).fetchone()
    if seen:
        con.close()
        return False
    con.execute("INSERT OR IGNORE INTO fills(fill_id,ticker,side,price,count,"
                "filled_at,is_sim) VALUES(?,?,?,?,?,?,?)",
                (sid, ticker, "settle", 0, 0, now_iso(), int(is_sim)))
    con.execute("INSERT OR IGNORE INTO settlements VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, ticker, result, revenue, cost, pnl, settled_at,
                 now_iso(), int(is_sim)))
    con.commit()
    con.close()
    return True


def add_realized(delta_cents: int, is_sim=False, day=None):
    table = "pnl_days_sim" if is_sim else "pnl_days"
    d = day or today()
    con = db()
    con.execute(f"INSERT INTO {table}(day, realized) VALUES(?,0) "
                "ON CONFLICT(day) DO NOTHING", (d,))
    con.execute(f"UPDATE {table} SET realized = realized + ? WHERE day = ?",
                (delta_cents, d))
    con.commit()
    con.close()


def day_realized(is_sim=False, day=None) -> int:
    table = "pnl_days_sim" if is_sim else "pnl_days"
    con = db()
    row = con.execute(f"SELECT realized FROM {table} WHERE day=?",
                      (day or today(),)).fetchone()
    con.close()
    return row[0] if row else 0


# ── maker CLV ──────────────────────────────────────────────────────────
def last_fair_before(ticker: str, commence_iso: str = None):
    """Final pre-game fair (YES cents-prob 0-1) = latest recorded fair
    value for the ticker, optionally bounded by commence time."""
    con = db()
    if commence_iso:
        row = con.execute(
            "SELECT fair_yes FROM fair_values WHERE ticker=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1", (ticker, commence_iso)).fetchone()
        if row:
            con.close()
            return row[0]
    row = con.execute("SELECT fair_yes FROM fair_values WHERE ticker=? "
                      "ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
    con.close()
    return row[0] if row else None


def compute_clv_for_ticker(ticker: str, is_sim=False) -> list:
    """At settlement: for every un-scored fill on this ticker, compute
    edge vs final pre-game fair. Positive = bought below closing fair
    (collecting spread); negative = picked off. Returns computed rows."""
    fair_close_yes = last_fair_before(ticker)
    if fair_close_yes is None:
        return []
    con = db()
    fills = con.execute(
        "SELECT fill_id, side, price, count, fair_at_fill, "
        "COALESCE(is_taker,0) FROM fills "
        "WHERE ticker=? AND side IN ('yes','no') AND is_sim=? "
        "AND fill_id NOT IN (SELECT fill_id FROM maker_clv)",
        (ticker, int(is_sim))).fetchall()
    out = []
    for fid, side, price, count, fair_at_fill, is_tkr in fills:
        fair_close_side = fair_close_yes * 100 if side == "yes" \
            else 100 - fair_close_yes * 100
        edge_vs_close = fair_close_side - price
        edge_at_fill = (fair_at_fill - price) if fair_at_fill is not None else None
        con.execute("INSERT OR IGNORE INTO maker_clv VALUES"
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fid, ticker, side, price, count, fair_at_fill,
                     fair_close_side, edge_at_fill, edge_vs_close,
                     now_iso(), int(is_sim), int(is_tkr)))
        out.append({"fill_id": fid, "side": side, "price": price,
                    "count": count, "edge_vs_close": edge_vs_close,
                    "is_taker": bool(is_tkr)})
    con.commit()
    con.close()
    return out


# ── daily summary / dashboard queries ──────────────────────────────────
def daily_summary(day: str, is_sim=False, maker_only=True) -> dict:
    """maker_only=True excludes your manual (taker) trades so bot
    performance isn't blended with hand-placed bets."""
    con = db()
    sim = int(is_sim)
    tk = " AND COALESCE(is_taker,0)=0" if maker_only else ""
    f = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(count),0), COALESCE(SUM(price*count),0), "
        "COALESCE(SUM(fee_cents),0), AVG(edge_at_fill) FROM fills "
        "WHERE side IN ('yes','no') AND is_sim=?" + tk +
        " AND substr(filled_at,1,10)=?", (sim, day)).fetchone()
    clv = con.execute(
        "SELECT AVG(edge_vs_close), SUM(edge_vs_close*count) FROM maker_clv "
        "WHERE is_sim=?" + tk + " AND substr(computed_at,1,10)=?",
        (sim, day)).fetchone()
    con.close()
    return {
        "fills": f[0], "contracts": f[1], "volume_cents": f[2],
        "fees_cents": f[3] or 0, "avg_edge_at_fill": f[4],
        "avg_edge_vs_close": clv[0], "clv_cents_total": clv[1],
        "gross_pnl_cents": day_realized(is_sim=is_sim, day=day),
    }


def fill_history(limit=25, is_sim=False, maker_only=True) -> list:
    con = db()
    tk = " AND COALESCE(f.is_taker,0)=0" if maker_only else ""
    rows = con.execute(
        "SELECT f.filled_at, f.ticker, f.side, f.price, f.count, "
        "f.fair_at_fill, f.edge_at_fill, c.edge_vs_close, "
        "COALESCE(f.is_taker,0) FROM fills f "
        "LEFT JOIN maker_clv c ON c.fill_id=f.fill_id "
        "WHERE f.side IN ('yes','no') AND f.is_sim=?" + tk +
        " ORDER BY f.filled_at DESC LIMIT ?", (int(is_sim), limit)).fetchall()
    con.close()
    return [{"at": (r[0] or "")[:16].replace("T", " "), "ticker": r[1],
             "side": r[2], "price": r[3], "count": r[4], "fair": r[5],
             "edge": r[6], "clv": r[7], "manual": bool(r[8])} for r in rows]


def strategy_stats(maker_only=True) -> dict:
    """Headline numbers, maker-only by default. Size-weighted CLV is the
    honest one: per-fill average hides the case where the big fills are
    the bad ones."""
    con = db()
    tk = " AND COALESCE(is_taker,0)=0" if maker_only else ""
    r = con.execute("SELECT AVG(edge_vs_close), COUNT(*), "
                    "SUM(edge_vs_close*count), SUM(count) FROM maker_clv "
                    "WHERE is_sim=0" + tk).fetchone()
    f = con.execute("SELECT COUNT(*), SUM(count) FROM fills "
                    "WHERE side IN ('yes','no') AND is_sim=0" + tk).fetchone()
    con.close()
    per_fill = r[0]
    weighted = (r[2] / r[3]) if r[2] is not None and r[3] else None
    return {"clv_per_fill": per_fill, "clv_size_weighted": weighted,
            "scored_fills": r[1] or 0, "scored_contracts": r[3] or 0,
            "total_fills": f[0] or 0, "total_contracts": f[1] or 0}


def pnl_history(days=14, is_sim=False) -> list:
    table = "pnl_days_sim" if is_sim else "pnl_days"
    con = db()
    rows = con.execute(f"SELECT day, realized FROM {table} "
                       "ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    con.close()
    return [{"day": r[0], "realized_cents": r[1]} for r in rows[::-1]]


def edge_summary(days=7, is_sim=False) -> dict:
    """Rolling edge-health numbers for the dashboard."""
    con = db()
    sim = int(is_sim)
    cutoff = (datetime.date.today() -
              datetime.timedelta(days=days)).isoformat()
    f = con.execute(
        "SELECT COUNT(*), AVG(edge_at_fill) FROM fills WHERE side IN "
        "('yes','no') AND is_sim=? AND substr(filled_at,1,10)>=?",
        (sim, cutoff)).fetchone()
    c = con.execute(
        "SELECT COUNT(*), AVG(edge_vs_close) FROM maker_clv WHERE is_sim=? "
        "AND substr(computed_at,1,10)>=?", (sim, cutoff)).fetchone()
    t = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(fee_cents),0) FROM fills WHERE side "
        "IN ('yes','no') AND is_sim=? AND substr(filled_at,1,10)=?",
        (sim, today())).fetchone()
    con.close()
    return {"window_days": days, "fills": f[0], "avg_edge_at_fill": f[1],
            "clv_scored": c[0], "avg_clv": c[1],
            "fills_today": t[0], "fees_today_cents": t[1]}


def meta_get(key: str):
    con = db()
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    con.close()
    return row[0] if row else None


def meta_set(key: str, value: str):
    con = db()
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()
    con.close()
