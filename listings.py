"""Fresh listings: alert the moment a prop appears for the FIRST time on
your target books (default: the exchanges, novig + prophetx) — if the model
sees an edge on it.

Why this window matters: exchange lines at listing are set thin, before
liquidity arrives to correct them. First-look + model edge is the purest
"early" bet in the whole system.

Detection: a (player, market, book) whose earliest-ever snapshot is within
LISTING_MAX_AGE_MIN (default 45) of now — i.e., it did not exist last cycle.
Joined against today's freshest edge rows for that exact book; alerts when
any side clears LISTING_MIN_EV (default 2%). One alert per prop per book per
day (listing_seen table).

Env:
  LISTINGS_WEBHOOK_URL       default channel (required to post)
  LISTINGS_WEBHOOK_<BOOK>    optional per-book channel override, e.g.
                             LISTINGS_WEBHOOK_FLIFF, LISTINGS_WEBHOOK_NOVIG
  LISTING_BOOKS              default "novig,prophetx,fliff,mybookieag"
  LISTING_MIN_EV             default 3.0
  LISTING_MAX_AGE_MIN        default 45
"""
from __future__ import annotations

import logging
import os
from datetime import date

import requests

from .db import get_conn
from .devig import prob_to_american
from .tiers import EMOJI, classify

log = logging.getLogger("kprop.listings")

DDL = """
CREATE TABLE IF NOT EXISTS listing_seen (
    player TEXT, market TEXT, bookmaker TEXT, seen_on DATE,
    PRIMARY KEY (player, market, bookmaker, seen_on)
)
"""

NEW_LISTINGS_SQL = """
SELECT player, market, bookmaker, min(fetched_at) AS first_seen
FROM odds_snapshots
WHERE bookmaker = ANY(%s) AND player IS NOT NULL
  AND commence_time > now()
GROUP BY player, market, bookmaker
HAVING min(fetched_at) > now() - make_interval(mins => %s)
"""

EDGE_SQL = """
SELECT DISTINCT ON (side, line)
       side, line, price, model_prob, market_fair_prob, ev_pct
FROM edges
WHERE game_date = %s AND pitcher_name = %s AND market = %s AND bookmaker = %s
ORDER BY side, line, created_at DESC
"""

MARKET_LABEL = {
    "pitcher_strikeouts": "Strikeouts", "pitcher_strikeouts_alternate": "K alt",
    "pitcher_outs": "Outs", "pitcher_hits_allowed": "Hits allowed",
    "pitcher_hits_allowed_alternate": "Hits alt", "pitcher_walks": "Walks",
    "pitcher_walks_alternate": "Walks alt", "pitcher_earned_runs": "Earned runs",
}


def scan(d: date | None = None) -> int:
    url = (os.environ.get("EARLY_WEBHOOK_URL")
           or os.environ.get("LISTINGS_WEBHOOK_URL"))
    if not url:
        return 0
    d = d or date.today()
    books = [b.strip() for b in os.environ.get(
        "LISTING_BOOKS", "novig,prophetx,fliff,mybookieag").split(",")
        if b.strip()]
    min_ev = float(os.environ.get("LISTING_MIN_EV", "3.0"))
    max_age = int(os.environ.get("LISTING_MAX_AGE_MIN", "45"))

    fired = 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(NEW_LISTINGS_SQL, (books, max_age))
        fresh = cur.fetchall()
        for player, market, book, first_seen in fresh:
            cur.execute("""SELECT 1 FROM listing_seen
                           WHERE player=%s AND market=%s AND bookmaker=%s
                             AND seen_on=%s""", (player, market, book, d))
            if cur.fetchone():
                continue
            cur.execute(EDGE_SQL, (d, player, market, book))
            quotes = cur.fetchall()
            good = []
            for side, line, price, mp, fp, ev in quotes:
                if ev is None or float(ev) < min_ev:
                    continue
                tier = classify(float(ev), float(mp),
                                float(fp) if fp is not None else None,
                                book, market)
                good.append((side, line, price, float(mp),
                             float(fp) if fp is not None else None,
                             float(ev), tier))
            if not good:
                continue
            good = [g for g in good if g[6] in ("solid", "strong")]
            if not good:
                continue          # model doesn't agree -> stay silent
            good.sort(key=lambda g: -g[5])
            side, line, price, mp, fp, ev, tier = good[0]
            fair_px = prob_to_american(min(max(mp, .01), .99))
            gap = f"{(mp - fp) * 100:+.1f} pts vs mkt" if fp is not None else "no consensus yet"
            mk = MARKET_LABEL.get(market, market)
            embed = {
                "title": f"🛎️ NEW on {book} · {player} — {mk}",
                "color": 0xB88CFF,
                "description": (
                    f"Just listed ({first_seen:%H:%M} UTC). Best side:\n"
                    f"{EMOJI[tier]} **{side} {line}** at **{int(price):+d}** — "
                    f"model fair {fair_px:+d} ({mp:.0%}), {gap}, "
                    f"**EV {ev:+.1f}%**\n"
                    f"_Exchange opener — thin until liquidity arrives; "
                    f"verify the line is live._"),
            }
            dest = os.environ.get(
                f"LISTINGS_WEBHOOK_{book.upper()}", url)
            try:
                requests.post(dest, json={"username": "Fresh Drop 🛎️",
                                          "embeds": [embed]}, timeout=15)
                fired += 1
            except Exception:
                log.exception("listing post failed")
            cur.execute("""INSERT INTO listing_seen VALUES (%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING""", (player, market, book, d))
        conn.commit()
    if fired:
        log.info("listings: %d fresh-drop alerts", fired)
    return fired


def main():
    logging.basicConfig(level=logging.INFO)
    scan()


if __name__ == "__main__":
    main()
