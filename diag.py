"""No-fills diagnostic. Answers, in order, exactly where the pipeline stopped:

  price -> quote -> get hit

1. Did it PRICE games?         (fair_values in the window)
2. Did it try to QUOTE?        (quote_events: post vs skip, with skip reasons)
3. Did anything FILL?          (fills in the window, sim + real)

Then it reads the result: a wall of 'skip' with a dominant reason is a config
problem we fix; lots of 'post' with zero fills is just no takers (expected at
probe size in a tight window on a slow night) -- not a bug.

USAGE (Railway console):
    python3 diag.py            # last 36h
    python3 diag.py 24         # last N hours
"""
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 36
    try:
        from config import DB_PATH
    except Exception:
        DB_PATH = "/data/maker.db"
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    def q(sql, *a):
        try:
            return con.execute(sql, a).fetchall()
        except sqlite3.OperationalError as e:
            print(f"  (query failed: {e})")
            return []

    print(f"=== no-fills diagnostic: last {hours}h (since {since[:16]}Z) ===\n")

    # 1. PRICING
    fv = q("SELECT COUNT(*) c, COUNT(DISTINCT ticker) t FROM fair_values "
           "WHERE ts >= ?", since)
    n_fv = fv[0]["c"] if fv else 0
    n_tk = fv[0]["t"] if fv else 0
    print(f"1. PRICING: {n_fv} fair-value snapshots across {n_tk} distinct "
          f"tickers")
    if n_fv == 0:
        print("   -> priced NOTHING. The feed isn't giving WNBA data, or games")
        print("      never came within MAX_HOURS_OUT. This is the whole problem;")
        print("      fix pricing before anything else. Check the odds feed.\n")
    else:
        sample = q("SELECT ticker, fair_yes FROM fair_values WHERE ts >= ? "
                   "ORDER BY ts DESC LIMIT 5", since)
        for r in sample:
            print(f"     e.g. {r['ticker']}  fair_yes={r['fair_yes']:.3f}")
        print()

    # 2. QUOTING
    ev = q("SELECT event, reason, COUNT(*) n FROM quote_events WHERE ts >= ? "
           "GROUP BY event, reason ORDER BY n DESC", since)
    posts = sum(r["n"] for r in ev if r["event"] == "post")
    skips = sum(r["n"] for r in ev if r["event"] == "skip")
    print(f"2. QUOTING: {posts} posts, {skips} skips")
    if not ev:
        print("   -> no quote activity logged at all. It priced games but never")
        print("      entered the quoting loop -- likely a risk gate (KILL, caps,")
        print("      or DEGRADED state). Check the boot/loop logs.\n")
    else:
        for r in ev:
            tag = r["reason"] or "-"
            print(f"     {r['event']:<14} {tag:<22} {r['n']}")
        if skips > posts * 3 and skips > 0:
            top = Counter()
            for r in ev:
                if r["event"] == "skip":
                    top[r["reason"] or "-"] += r["n"]
            reason, cnt = top.most_common(1)[0]
            print(f"\n   -> mostly SKIPS. Dominant reason: '{reason}' ({cnt}x).")
            print("      That's the lever to fix. See mapping below.")
        print()

    # 3. FILLS
    fl = q("SELECT COALESCE(is_sim,0) s, COUNT(*) n FROM fills WHERE "
           "filled_at >= ? GROUP BY is_sim", since)
    real = sum(r["n"] for r in fl if r["s"] == 0)
    sim = sum(r["n"] for r in fl if r["s"] == 1)
    print(f"3. FILLS: {real} real, {sim} sim")
    print()

    # READ
    print("=== read ===")
    if n_fv == 0:
        print("Stopped at PRICING. Nothing else matters until the feed gives")
        print("WNBA games inside the quote window. That's the fix.")
    elif posts == 0 and skips == 0:
        print("Priced but never quoted -> a risk gate or state issue, not edge.")
    elif posts == 0 and skips > 0:
        print("Priced and TRIED to quote but skipped everything. Not a 'no")
        print("takers' night -- a threshold is rejecting every quote. Fixable.")
    elif posts > 0 and real == 0:
        print(f"Posted {posts} quotes, zero real fills. The machine WORKS -- it")
        print("priced, quoted in-window, and rested orders. No one hit them.")
        print("At probe size in a 2h window that's a normal slow night, not a")
        print("bug. Levers to get more fills: quote closer to the touch (tighter")
        print("MARGIN_CENTS), lower MIN_EDGE_CENTS, or widen the window a little")
        print("(MAX_HOURS_OUT 2->3). Decide with the skip reasons above.")
    else:
        print("Fills present. If you expected more, tune margin/edge/window.")

    print("\nskip-reason cheat sheet:")
    print("  edge / min-edge   -> MIN_EDGE_CENTS too high for current spreads")
    print("  clamp / book      -> our price outside the live book; MARGIN_CENTS")
    print("  cap / exposure    -> hit a rung/event/total cap (probe size is low)")
    print("  cooldown          -> FILL_COOLDOWN_SECS after a fill/attempt")
    print("  unc / uncertainty -> books disagree; UNC_SKIP gate")
    print("  pulled / window   -> inside PULL_MIN or outside MAX_HOURS_OUT")
    con.close()


if __name__ == "__main__":
    main()
