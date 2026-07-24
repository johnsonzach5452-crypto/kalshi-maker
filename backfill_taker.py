"""Backfill is_taker on historical fills from Kalshi's records.

Existing rows were saved before we tracked maker vs taker, so manual
trades are currently blended into bot stats. This pulls the authoritative
flag from Kalshi and tags them.

    python3 backfill_taker.py          # dry run
    python3 backfill_taker.py --apply
"""
import argparse
import sys

import store
from kalshi_client import KalshiClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    store.init_db()

    client = KalshiClient()
    fills = client.get_fills()
    flags = {}
    for f in fills:
        fid = f.get("trade_id") or f.get("fill_id")
        if fid:
            flags[fid] = 1 if f.get("is_taker") else 0

    con = store.db()
    known = {r[0] for r in con.execute(
        "SELECT fill_id FROM fills WHERE side IN ('yes','no')")}
    hits = {k: v for k, v in flags.items() if k in known}
    takers = sum(1 for v in hits.values() if v)
    print(f"Kalshi returned {len(flags)} fills; {len(hits)} match our DB")
    print(f"  maker (bot):    {len(hits) - takers}")
    print(f"  taker (manual): {takers}")

    if not args.apply:
        con.close()
        print("\ndry run — rerun with --apply to tag them")
        return 0

    for fid, is_tk in hits.items():
        con.execute("UPDATE fills SET is_taker=? WHERE fill_id=?", (is_tk, fid))
        con.execute("UPDATE maker_clv SET is_taker=? WHERE fill_id=?",
                    (is_tk, fid))
    con.commit()
    con.close()
    print(f"\ntagged {len(hits)} fills. Bot stats now exclude manual trades.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
