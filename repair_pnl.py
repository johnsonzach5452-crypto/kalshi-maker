"""Rebuild realized P&L from Kalshi's own settlement records.

Why this exists: the V2 API renamed settlement cost fields to
*_total_cost_dollars (dollar strings). The old integer keys read as 0, so
every win was booked at full payout with cost ignored, and every loss was
booked as $0. The pnl_days table is therefore garbage.

This pulls the authoritative settlement history from Kalshi, recomputes
each row correctly, and rewrites pnl_days. Read-only against Kalshi.

    python3 repair_pnl.py            # show what it WOULD do
    python3 repair_pnl.py --apply    # actually rewrite pnl_days
"""
import argparse
import collections
import sys

import store
from kalshi_client import KalshiClient, norm_settlement


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the corrected values to pnl_days")
    ap.add_argument("--since", default="2026-07-19",
                    help="only rewrite days on/after this (bot era). "
                         "Earlier settlements are your manual history and "
                         "must not be written into bot P&L.")
    args = ap.parse_args()

    store.init_db()
    client = KalshiClient()
    try:
        settlements = client.get_settlements(limit=200)
    except Exception as e:
        print("could not fetch settlements:", e)
        return 1

    # Attribute each settlement to BOT or MANUAL using the fill flags we
    # recorded. Settlements are account-level, so without this the bot's
    # P&L would absorb your hand-placed trades.
    con0 = store.db()
    taker_tickers = {r[0] for r in con0.execute(
        "SELECT DISTINCT ticker FROM fills WHERE COALESCE(is_taker,0)=1")}
    maker_tickers = {r[0] for r in con0.execute(
        "SELECT DISTINCT ticker FROM fills WHERE COALESCE(is_taker,0)=0 "
        "AND side IN ('yes','no')")}
    con0.close()

    by_day = collections.defaultdict(int)
    rows = []
    skipped_manual = 0
    for s in settlements:
        rev, cost, fee = norm_settlement(s)
        pnl = rev - cost - fee
        day = str(s.get("settled_time", ""))[:10]
        tkr = s.get("ticker", "?")
        if not day or day < args.since:
            continue
        if tkr in taker_tickers and tkr not in maker_tickers:
            skipped_manual += 1          # purely your own trade
            continue
        by_day[day] += pnl
        rows.append((day, tkr, rev, cost, fee, pnl))

    rows.sort(key=lambda r: r[0])
    print(f"{'DAY':12}{'TICKER':30}{'REV':>8}{'COST':>8}{'FEE':>6}{'P&L':>9}")
    print("-" * 74)
    for day, tkr, rev, cost, fee, pnl in rows[-40:]:
        print(f"{day:12}{tkr[-29:]:30}{rev:>8}{cost:>8}{fee:>6}{pnl:>9}")

    print("\n=== corrected daily totals ===")
    con = store.db()
    total = 0
    for day in sorted(by_day):
        old = con.execute("SELECT realized FROM pnl_days WHERE day=?",
                          (day,)).fetchone()
        old_v = old[0] if old else 0
        total += by_day[day]
        flag = "  <-- was wrong" if old_v != by_day[day] else ""
        print(f"  {day}  old ${old_v/100:>9.2f}   true ${by_day[day]/100:>9.2f}{flag}")
    print(f"\nBOT realized total (since {args.since}): ${total/100:+.2f}")
    print(f"(bot settlements: {len(rows)}; manual excluded: {skipped_manual})")

    if not args.apply:
        con.close()
        print("\ndry run — rerun with --apply to rewrite pnl_days")
        return 0

    for day, pnl in by_day.items():
        con.execute("INSERT INTO pnl_days(day, realized) VALUES(?,?) "
                    "ON CONFLICT(day) DO UPDATE SET realized=excluded.realized",
                    (day, pnl))
    # keep the settlements table consistent too
    for s in settlements:
        rev, cost, fee = norm_settlement(s)
        sid = s.get("ticker", "") + str(s.get("settled_time", ""))
        con.execute("UPDATE settlements SET revenue_cents=?, cost_cents=?, "
                    "pnl_cents=? WHERE sid=?",
                    (rev, cost, rev - cost - fee, sid))
    con.commit()
    con.close()
    print("\npnl_days rewritten with corrected values.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
