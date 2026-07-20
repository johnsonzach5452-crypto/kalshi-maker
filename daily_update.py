"""Daily data refresh: keep Statcast pitches and feature tables current.

THE GAP THIS CLOSES: without it, the projector runs forever on features
from whenever the backfill last ran — a pitcher's velocity jump or slump
in the last two weeks would be invisible to the model.

Pulls the last LOOKBACK_DAYS of Statcast (idempotent: delete+reload the
window), rebuilds current-season features, then grades yesterday's
results. One cron covers all daily data hygiene.

Railway cron (11:00 UTC = after all West Coast games are final):
    python -m kprop.daily_update
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import requests as rq

log = logging.getLogger("kprop.daily_update")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    lookback = int(os.environ.get("LOOKBACK_DAYS", "3"))
    end = date.today()
    start = end - timedelta(days=lookback)

    from .statcast_ingest import ingest_range
    log.info("statcast refresh %s..%s", start, end)
    ingest_range(start, end)

    from .features import build
    log.info("rebuilding features for %d", end.year)
    build(end.year)

    from .fit import run as fit_run
    log.info("refitting model params")
    fit_run()

    from .db import ensure_indexes, get_conn
    ensure_indexes()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM edges WHERE created_at < now() - interval '90 days'")
        conn.commit()

    from .results import main as grade_results
    import sys
    sys.argv = ["results"]  # grade yesterday
    grade_results()

    try:
        from .abs import run as abs_run
        abs_run()
    except Exception:
        log.exception("abs refresh failed")

    try:
        from .bets import close as fill_closers
        fill_closers(None)          # CLV pipeline: closing prices for all bets
    except Exception:
        log.exception("closer fill failed")

    try:
        from .lebron import recap, settle
        settle()
        recap()
    except Exception:
        log.exception("lebron settle/recap failed")

    if date.today().weekday() == 0:  # Monday
        try:
            _weekly_digest()
        except Exception:
            log.exception("weekly digest failed")

    _heartbeat()
    log.info("daily update complete")


def _weekly_digest():
    """Monday report: model vs market over the last 14 days + LeBron week."""
    import json
    from .db import get_conn
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    lines = []
    with get_conn() as conn, conn.cursor() as cur:
        # model calibration vs results, last 14 days (K market)
        cur.execute("""
            SELECT p.distribution, r.strikeouts
            FROM projections p
            JOIN pitcher_game_results r
              ON lower(p.pitcher_name) = lower(r.pitcher_name)
             AND p.created_at::date = r.game_date AND r.is_starter
            WHERE p.market = 'strikeouts'
              AND p.created_at > now() - interval '14 days'
        """)
        pairs = []
        for dist, ks in cur.fetchall():
            d = dist if isinstance(dist, dict) else json.loads(dist)
            d = {int(k): float(v) for k, v in d.items()}
            keys = sorted(d)
            for ln in [k + 0.5 for k in keys[2:-2]]:
                pov = sum(v for k, v in d.items() if k > ln)
                pairs.append((pov, 1.0 if ks > ln else 0.0))
        if pairs:
            brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
            lines.append(f"model Brier (14d, {len(pairs)} probs): **{brier:.4f}**")
        cur.execute("""
            SELECT count(*) FILTER (WHERE result='win'),
                   count(*) FILTER (WHERE result='loss'),
                   avg(CASE WHEN closing_price IS NOT NULL THEN
                     (CASE WHEN closing_price>0 THEN 100.0/(closing_price+100)
                           ELSE -closing_price::float/(-closing_price+100) END) -
                     (CASE WHEN price>0 THEN 100.0/(price+100)
                           ELSE -price::float/(-price+100) END) END)
            FROM bets WHERE trader='lebron'
              AND placed_at > now() - interval '7 days'
        """)
        w, l, clv = cur.fetchone()
        clv_s = f"{clv*100:+.2f}%" if clv is not None else "—"
        lines.append(f"LeBron week: **{w or 0}-{l or 0}** · CLV **{clv_s}**")
        try:
            cur.execute("""SELECT count(*) FROM value_seen
                           WHERE alerted_at > now() - interval '7 days'""")
            vn = cur.fetchone()[0]
            lines.append(f"Value scanner: **{vn}** exchange mispricings flagged")
        except Exception:
            pass
    if url and lines:
        rq.post(url, json={"username": "kprop weekly",
                           "content": "**📊 Weekly digest**\n" + "\n".join(lines)},
                timeout=15)


def _heartbeat():
    """Post system health to Discord so silent failures get caught same-day."""
    import requests as rq
    from .db import get_conn
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    checks = []
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT event_id) FROM odds_snapshots "
                        "WHERE fetched_at > now() - interval '24 hours'")
            n_rows, n_events = cur.fetchone()
            checks.append(f"odds rows 24h: {n_rows} across {n_events} games"
                          + (" ⚠️ LOGGER DOWN?" if n_rows == 0 else " ✅"))
            cur.execute("SELECT max(game_date) FROM statcast_pitches")
            checks.append(f"statcast through: {cur.fetchone()[0]}")
            cur.execute("SELECT max(game_date) FROM pitcher_game_results")
            checks.append(f"results through: {cur.fetchone()[0]}")
            cur.execute("SELECT count(*) FROM edges WHERE created_at > now() - interval '24 hours'")
            checks.append(f"edges 24h: {cur.fetchone()[0]}")
    except Exception as e:
        checks.append(f"⚠️ heartbeat query failed: {e}")
    msg = "**kprop daily health**\n" + "\n".join(checks)
    log.info(msg)
    if url:
        try:
            rq.post(url, json={"content": msg[:1990]}, timeout=15)
        except Exception:
            log.exception("heartbeat post failed")


if __name__ == "__main__":
    main()
