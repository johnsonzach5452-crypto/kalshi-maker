"""Pond finder: rank Kalshi series by how makeable they are.

Run on the server any time:   python3 scan.py
                              python3 scan.py --wide      (only good ones)
                              python3 scan.py --series KXNFLSPREAD,KXNFLTOTAL

Why a script instead of ad-hoc probes: a blind /markets crawl drowns in
the 13k auto-generated KXMVE parlay shells, so we query candidate series
BY NAME and sample real order books. What we're hunting is the shape that
made WNBA work -- wide spread AND a thin touch:

    spread >= 3c   room for our edge floor after fees
    touch  <  500  our quote is real liquidity, not behind a wall

A wide spread with a 200k-contract wall (MLB) is not an opportunity; a
thin touch with a 1c spread (tennis) has no room. We need both.
"""
import argparse
import statistics
import sys

from kalshi_client import KalshiClient

# Candidates worth checking. Add freely -- unknown series just report 0.
CANDIDATES = [
    # proven / active
    "KXWNBASPREAD", "KXWNBATOTAL", "KXWNBAGAME",
    # football (preseason opens early Aug; regular season Sept)
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLPRESEASON",
    "KXNFLPRESEASONSPREAD", "KXNFLPRESEASONTOTAL",
    "KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL",
    # baseball
    "KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL",
    # basketball (NBA preseason Oct)
    "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL",
    # hockey (Oct)
    "KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL",
    # soccer
    "KXMLSGAME", "KXMLSSPREAD", "KXMLSTOTAL",
    "KXNWSLGAME", "KXNWSLSPREAD", "KXNWSLTOTAL", "KXNWSLBTTS",
    "KXEPLGAME", "KXEPLSPREAD", "KXEPLTOTAL",
    # individual sports
    "KXATPMATCH", "KXWTAMATCH", "KXUFCFIGHT", "KXPGATOURN", "KXNASCARRACE",
    # non-sports
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHMIA", "KXHIGHAUS",
    "KXCPI", "KXFEDDECISION", "KXPAYROLLS",
]

SPREAD_FLOOR = 3      # cents
TOUCH_CEILING = 500   # contracts at the touch


def sample(client, ticker, depth=3):
    """-> (spread_cents, touch_depth) or None if the book is unusable."""
    try:
        ob = client.get_orderbook(ticker, depth)
    except Exception:
        return None
    yes, no = ob.get("yes") or [], ob.get("no") or []
    yb = max((lv[0] for lv in yes if lv), default=0)
    nb = max((lv[0] for lv in no if lv), default=0)
    if not yb or not nb:
        return None                     # empty / one-sided
    spread = 100 - yb - nb
    d_yes = sum(lv[1] for lv in yes if lv and lv[0] == yb)
    d_no = sum(lv[1] for lv in no if lv and lv[0] == nb)
    return spread, min(d_yes, d_no)


def scan(series_list, per_series=5):
    client = KalshiClient()
    rows = []
    for s in series_list:
        try:
            ms = client._req("GET", "/markets",
                             params={"series_ticker": s, "status": "open",
                                     "limit": 40}).get("markets", [])
        except Exception as e:
            rows.append((s, 0, None, None, f"err {str(e)[:20]}"))
            continue
        if not ms:
            continue
        spreads, depths = [], []
        for m in ms[:per_series]:
            got = sample(client, m["ticker"])
            if got:
                spreads.append(got[0])
                depths.append(got[1])
        if not spreads:
            rows.append((s, len(ms), None, None, "empty books"))
            continue
        sp = statistics.median(spreads)
        dp = int(statistics.median(depths))
        if sp >= SPREAD_FLOOR and dp < TOUCH_CEILING:
            verdict = "** MAKEABLE **"
        elif sp < SPREAD_FLOOR:
            verdict = "too tight"
        else:
            verdict = "walled"
        rows.append((s, len(ms), sp, dp, verdict))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="comma-separated series to check")
    ap.add_argument("--wide", action="store_true",
                    help="only show makeable candidates")
    args = ap.parse_args()

    targets = args.series.split(",") if args.series else CANDIDATES
    rows = scan(targets)
    rows.sort(key=lambda r: (r[4] != "** MAKEABLE **",
                             -(r[2] or 0), r[3] or 1e9))

    print(f"{'SERIES':24}{'MKTS':>5}{'SPREAD':>8}{'TOUCH':>8}  VERDICT")
    print("-" * 62)
    for s, n, sp, dp, verdict in rows:
        if args.wide and verdict != "** MAKEABLE **":
            continue
        sp_s = f"{sp}c" if sp is not None else "--"
        dp_s = str(dp) if dp is not None else "--"
        print(f"{s:24}{n:>5}{sp_s:>8}{dp_s:>8}  {verdict}")
    print(f"\ncriteria: spread >= {SPREAD_FLOOR}c AND touch < {TOUCH_CEILING}")
    print("run daily-ish; new series (NFL preseason etc) appear as listed")


if __name__ == "__main__":
    sys.exit(main())
