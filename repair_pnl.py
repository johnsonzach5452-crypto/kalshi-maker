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
    args = ap.parse_args()

    store.init_db()
    client = KalshiClient()
    try:
        settlements = client.get_settlements(limit=200)
    except Exception as e:
        print("could not fetch settlements:", e)
        return 1

    by_day = collections.defaultdict(int)
    rows = []
    for s in settlements:
        rev, cost, fee = norm_settlement(s)
        pnl = rev - cost - fee
        day = str(s.get("settled_time", ""))[:10]
        if not day:
            continue
        by_day[day] += pnl
        rows.append((day, s.get("ticker", "?"), rev, cost, fee, pnl))

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
    print(f"\nTRUE realized total: ${total/100:+.2f}")
    print(f"(settlements covered: {len(rows)})")

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
