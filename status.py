"""kprop mission control — the whole operation on one screen.

    python -m kprop.status

Read-only. Shows: model brain, data pulse, today's board by tier with
sparkline distributions, LeBron's ledger, and what runs next.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

from .db import get_conn
from .devig import prob_to_american
from .tiers import EMOJI, blend_weight, calibrated_prob, classify

BLOCKS = "▁▂▃▄▅▆▇█"


def spark(dist: dict[int, float], line: float | None = None) -> str:
    if not dist:
        return ""
    ks = sorted(dist)[:12]
    peak = max(dist[k] for k in ks) or 1
    out = []
    for k in ks:
        ch = BLOCKS[min(7, int(dist[k] / peak * 7.999))]
        out.append(f"\033[92m{ch}\033[0m" if line is not None and k > line else ch)
    return "".join(out) + f"  ({ks[0]}–{ks[-1]})"


def bar(v: float, vmax: float, width: int = 12) -> str:
    n = max(0, min(width, int(v / vmax * width)))
    return "█" * n + "·" * (width - n)


def main():
    now = datetime.now(timezone.utc)
    with get_conn() as conn, conn.cursor() as cur:
        # model brain
        cur.execute("SELECT key, value, updated_at FROM model_params")
        params = {k: (v, t) for k, v, t in cur.fetchall()}
        sim = params.get("sim_params", ({}, None))[0]
        sl = params.get("starter_league", ({}, None))[0]

        # data pulse
        cur.execute("""SELECT count(*), max(fetched_at) FROM odds_snapshots
                       WHERE fetched_at > now() - interval '2 hours'""")
        n2h, last_fetch = cur.fetchone()
        cur.execute("SELECT count(*) FROM odds_snapshots")
        n_all = cur.fetchone()[0]
        age = int((now - last_fetch).total_seconds() // 60) if last_fetch else None

        # today's board
        cur.execute("""
            SELECT DISTINCT ON (pitcher_name, market, line, side)
                   pitcher_name, market, side, line, price, bookmaker,
                   model_prob, market_fair_prob, ev_pct
            FROM edges WHERE game_date = %s
            ORDER BY pitcher_name, market, line, side, created_at DESC
        """, (date.today(),))
        board = []
        for r in cur.fetchall():
            name, market, side, line, price, book, mp, fp, ev = r
            t = classify(float(ev), float(mp),
                         float(fp) if fp is not None else None, book, market)
            board.append((t, name, market, side, line, int(price), book,
                          float(mp), float(fp) if fp is not None else None,
                          float(ev)))
        strong = [b for b in board if b[0] == "strong"]
        solid = [b for b in board if b[0] == "solid"]

        # top edge's K distribution
        top = (strong + solid)[:1]
        top_spark = ""
        if top:
            cur.execute("""SELECT distribution FROM projections
                           WHERE pitcher_name=%s AND market='strikeouts'
                           ORDER BY created_at DESC LIMIT 1""", (top[0][1],))
            row = cur.fetchone()
            if row:
                d = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                kline = float(top[0][4]) if top[0][2].startswith("pitcher_strikeouts") else None
                top_spark = spark({int(k): float(v) for k, v in d.items()}, kline)

        # lebron ledger
        cur.execute("""SELECT count(*),
                              count(*) FILTER (WHERE result='win'),
                              count(*) FILTER (WHERE result='loss'),
                              avg(CASE WHEN closing_price IS NOT NULL THEN
                                (CASE WHEN closing_price>0 THEN 100.0/(closing_price+100)
                                      ELSE -closing_price::float/(-closing_price+100) END) -
                                (CASE WHEN price>0 THEN 100.0/(price+100)
                                      ELSE -price::float/(-price+100) END) END)
                       FROM bets WHERE trader='lebron'""")
        nb, w, l, clv = cur.fetchone()
        cur.execute("""SELECT count(*), avg(CASE WHEN closing_price IS NOT NULL THEN
                                (CASE WHEN closing_price>0 THEN 100.0/(closing_price+100)
                                      ELSE -closing_price::float/(-closing_price+100) END) -
                                (CASE WHEN price>0 THEN 100.0/(price+100)
                                      ELSE -price::float/(-price+100) END) END)
                       FROM bets WHERE trader='lebron'
                         AND placed_at > now() - interval '3 days'""")
        nb3, clv3 = cur.fetchone()

    G, Y, D, R, X = "\033[92m", "\033[93m", "\033[2m", "\033[91m", "\033[0m"
    W = 64
    print()
    print(f"{Y}  ██╗  ██╗{X}██████╗ ██████╗  ██████╗ ██████╗ ")
    print(f"{Y}  ██║ ██╔╝{X}██╔══██╗██╔══██╗██╔═══██╗██╔══██╗")
    print(f"{Y}  █████╔╝ {X}██████╔╝██████╔╝██║   ██║██████╔╝")
    print(f"{Y}  ██╔═██╗ {X}██╔═══╝ ██╔══██╗██║   ██║██╔═══╝ ")
    print(f"{Y}  ██║  ██╗{X}██║     ██║  ██║╚██████╔╝██║     ")
    print(f"{Y}  ╚═╝  ╚═╝{X}╚═╝     ╚═╝  ╚═╝ ╚═════╝ ╚═╝  {D}mission control{X}")
    print(f"{D}{'─'*W}{X}")
    print(f"  🕐 {now:%a %b %d · %H:%M} UTC")

    print(f"\n  {Y}MODEL BRAIN{X}")
    if sim:
        print(f"   k_scale {G}{sim.get('k_scale','—')}{X} · workload "
              f"{G}{sim.get('workload_scale','—')}{X} · volatility "
              f"{sim.get('stuff_day_sd','—')} · shrink→starters "
              f"K {sl.get('k','—')} BB {sl.get('bb','—')}")
        print(f"   trust: K {blend_weight('pitcher_strikeouts'):.0%} · "
              f"outs {blend_weight('pitcher_outs'):.0%} · "
              f"ER {blend_weight('pitcher_earned_runs'):.0%} · "
              f"calibration shrink {calibrated_prob(1.0)*2-1:.0%}")
    else:
        print(f"   {R}no tuned params — run: python -m kprop.tune{X}")

    print(f"\n  {Y}DATA PULSE{X}")
    ok = age is not None and age < 45
    dot = f"{G}●{X}" if ok else f"{R}●{X}"
    print(f"   {dot} last odds fetch {age if age is not None else '—'}m ago · "
          f"{n2h or 0} rows/2h · {n_all:,} lifetime")

    print(f"\n  {Y}TODAY'S BOARD{X}  🔥{len(strong)} strong · 🎯{len(solid)} solid "
          f"· 👀{len(board)-len(strong)-len(solid)} watch")
    shown = (strong + solid)[:5]
    for t, name, market, side, line, price, book, mp, fp, ev in shown:
        mk = market.replace("pitcher_", "").replace("_alternate", "·alt").replace("_", " ")
        wm = blend_weight(market)
        pc = calibrated_prob(mp)
        pb = wm * pc + (1 - wm) * fp if fp is not None else pc
        fair = prob_to_american(min(max(pb, .01), .99))
        print(f"   {EMOJI[t]} {name:<22.22s} {mk:<9.9s} {side:>5s} {line:<4} "
              f"{price:+d}@{book:<10.10s} fair {fair:+d}  "
              f"{G if ev>=0 else R}{bar(abs(ev),10)}{X} {ev:+.1f}%")
    if not shown:
        print(f"   {D}nothing above the bar — the filters are doing their job{X}")
    if top_spark:
        print(f"   {D}top edge K dist:{X} {top_spark}")

    print(f"\n  {Y}LEBRON'S LEDGER{X} 👑")
    clv_s = f"{clv*100:+.2f}%" if clv is not None else "building"
    clv3_s = f"{clv3*100:+.2f}%" if clv3 is not None else "building"
    verdict = ("" if clv3 is None else
               f"  {G}← real-money territory{X}" if clv3 >= 0.015 else
               f"  {D}(bar: +1.50%){X}")
    print(f"   {nb or 0} bets · {w or 0}-{l or 0} · CLV all-time {clv_s} · "
          f"last 3d {G}{clv3_s}{X}{verdict}")

    print(f"\n  {D}projector: 12/14/20/22 UTC · daily update: 11 UTC · "
          f"dashboard ?key=… · full audit: python -m kprop.audit{X}")
    print(f"{D}{'─'*W}{X}\n")


if __name__ == "__main__":
    main()
