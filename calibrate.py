"""Model calibration harness — the validation layer that was missing.

Reads the SAME maker.db the bot writes (fair_values + settlements) and asks
the one question CLV can't answer: WAS THE MODEL RIGHT? It compares the final
pre-game fair (the model's P(YES)) to what actually happened at settlement.

Two things it reports:
  1. Reliability / calibration: bucket fairs into deciles; in each, compare
     the average predicted P(YES) to the realized YES rate. A well-calibrated
     model tracks the diagonal. Systematic gaps = bias.
  2. Directional bias by market type: split TOTAL (YES=over) from SPREAD
     (YES=cover). If overs settle YES MORE often than the model predicted,
     the model UNDERPRICES overs -- the documented bug. Same for covers.

Also prints the Brier score (lower is better; 0.25 = coin-flip baseline) and
a binomial-style read on whether any bias is beyond noise.

USAGE (no external deps; runs in the Railway console or locally):
    python3 calibrate.py                  # uses config.DB_PATH
    python3 calibrate.py /path/to/maker.db
    python3 calibrate.py --sim            # analyze paper-mode (is_sim=1) rows

Run it now for a baseline on the live (Normal) model, then again after a
paper-mode stretch with LADDER_DIST=t to confirm the tail bias is gone.
"""
import math
import sqlite3
import sys


def _yes_outcome(result: str):
    """Map a settlement result string to 1 (YES) / 0 (NO) / None (unknown)."""
    if result is None:
        return None
    r = str(result).strip().lower()
    if r in ("yes", "y", "1", "true", "t", "win", "won"):
        return 1
    if r in ("no", "n", "0", "false", "f", "loss", "lost"):
        return 0
    return None


def _market_type(ticker: str) -> str:
    t = (ticker or "").upper()
    if "TOTAL" in t:
        return "total"     # YES = over
    if "SPREAD" in t:
        return "spread"    # YES = cover
    return "other"


def load_pairs(db_path: str, is_sim: int, since: str = None):
    """Return [(ticker, type, predicted_p_yes, realized_yes)] for settled
    markets that have a recorded pre-settlement fair.

    since: optional 'YYYY-MM-DD'. Only settlements on/after this date count —
    use the day you switched LADDER_DIST=t so the old Normal-model results
    don't dilute the read on the new model.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    if since:
        settled = con.execute(
            "SELECT ticker, result FROM settlements WHERE is_sim=? "
            "AND substr(COALESCE(settled_at,recorded_at),1,10) >= ?",
            (is_sim, since)).fetchall()
    else:
        settled = con.execute(
            "SELECT ticker, result FROM settlements WHERE is_sim=?",
            (is_sim,)).fetchall()
    pairs = []
    for s in settled:
        y = _yes_outcome(s["result"])
        if y is None:
            continue
        row = con.execute(
            "SELECT fair_yes FROM fair_values WHERE ticker=? "
            "ORDER BY ts DESC LIMIT 1", (s["ticker"],)).fetchone()
        if not row or row["fair_yes"] is None:
            continue
        p = float(row["fair_yes"])
        if p > 1.5:            # stored as cents somewhere? normalize
            p /= 100.0
        pairs.append((s["ticker"], _market_type(s["ticker"]), p, y))
    con.close()
    return pairs


def reliability(pairs, nbuckets=10):
    """Decile reliability table."""
    buckets = [[] for _ in range(nbuckets)]
    for _, _, p, y in pairs:
        b = min(int(p * nbuckets), nbuckets - 1)
        buckets[b].append((p, y))
    rows = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        avg_p = sum(p for p, _ in b) / len(b)
        rate = sum(y for _, y in b) / len(b)
        rows.append((f"{i/nbuckets:.1f}-{(i+1)/nbuckets:.1f}", len(b),
                     avg_p, rate, rate - avg_p))
    return rows


def brier(pairs):
    if not pairs:
        return None
    return sum((p - y) ** 2 for _, _, p, y in pairs) / len(pairs)


def directional(pairs, mtype):
    sub = [(p, y) for _, t, p, y in pairs if t == mtype]
    if not sub:
        return None
    n = len(sub)
    avg_pred = sum(p for p, _ in sub) / n
    realized = sum(y for _, y in sub) / n
    bias = realized - avg_pred          # + => YES happens more than predicted
    # standard error of the mean realized rate
    se = math.sqrt(max(realized * (1 - realized), 1e-9) / n)
    z = bias / se if se else 0.0
    return dict(n=n, avg_pred=avg_pred, realized=realized, bias=bias, z=z)


def main():
    args = [a for a in sys.argv[1:]]
    is_sim = 1 if "--sim" in args else 0
    args = [a for a in args if a != "--sim"]
    since = None
    if "--since" in args:
        i = args.index("--since")
        since = args[i + 1]
        del args[i:i + 2]
    if args:
        db_path = args[0]
    else:
        try:
            from config import DB_PATH
            db_path = DB_PATH
        except Exception:
            db_path = "/data/maker.db"

    tag = f"  (is_sim={is_sim}" + (f", since {since}" if since else "") + ")"
    print(f"reading {db_path}{tag}")
    try:
        pairs = load_pairs(db_path, is_sim, since)
    except sqlite3.OperationalError as e:
        print(f"could not read db: {e}")
        return

    if not pairs:
        print("no settled markets with a recorded pre-game fair yet.")
        print("(need rows in both fair_values and settlements; in paper mode "
              "run with --sim)")
        return

    print(f"\n{len(pairs)} settled markets with a recorded fair\n")

    print("RELIABILITY (predicted P(YES) vs realized YES rate, by decile)")
    print(f"{'bucket':>10} {'n':>5} {'pred':>7} {'real':>7} {'gap':>8}")
    for name, n, ap, rate, gap in reliability(pairs):
        flag = "  <== underpriced" if gap > 0.05 else (
               "  <== overpriced" if gap < -0.05 else "")
        print(f"{name:>10} {n:>5} {ap:>7.3f} {rate:>7.3f} {gap:>+8.3f}{flag}")

    b = brier(pairs)
    print(f"\nBrier score: {b:.4f}  (0.25 = coin-flip; lower is better)")

    print("\nDIRECTIONAL BIAS BY MARKET TYPE")
    print("(bias > 0 means the outcome happens MORE than the model predicted")
    print(" = the model UNDERPRICES that side)")
    for mtype, label in [("total", "TOTAL  (YES = over) "),
                         ("spread", "SPREAD (YES = cover)")]:
        d = directional(pairs, mtype)
        if not d:
            print(f"  {label}: no data")
            continue
        verdict = ""
        if abs(d["z"]) >= 2:
            verdict = ("  UNDERPRICES overs" if d["bias"] > 0 and mtype == "total"
                       else "  UNDERPRICES covers" if d["bias"] > 0
                       else "  OVERPRICES")
        print(f"  {label}: n={d['n']:>4}  pred={d['avg_pred']:.3f}  "
              f"real={d['realized']:.3f}  bias={d['bias']:+.3f}  "
              f"z={d['z']:+.2f}{verdict}")

    print("\nRead: a large positive bias on TOTAL confirms the thin-tail bug on")
    print("YOUR data. After a paper stretch with LADDER_DIST=t, re-run with")
    print("--sim; the bias should shrink toward zero.")


if __name__ == "__main__":
    main()
