"""Steam detection: catch coordinated multi-book line moves in real time.

Your snapshot log already contains this signal — when several books move the
same prop in the same direction within a short window, sharp money is hitting
it, and the books that HAVEN'T moved yet are briefly stale. That stale price
is often the cleanest bet of the day, model or no model.

After each logger cycle, for every prop on today's slate:
  - compare each book's current implied prob vs its own price ~WINDOW ago
  - if >= STEAM_MIN_BOOKS moved the same direction by >= STEAM_MIN_MOVE
    (in implied-prob points), fire a steam alert listing which books moved,
    which are lagging, and what the model thinks
  - dedupe: one alert per prop per direction per 3 hours (steam_seen table)

Env:
  STEAM_WEBHOOK_URL   channel for steam (falls back to DISCORD_WEBHOOK_URL)
  STEAM_MIN_BOOKS     default 3
  STEAM_MIN_MOVE      default 0.02 (2 prob points, ~= -110 -> -122)
  STEAM_WINDOW_MIN    default 50 minutes
  LEAD_BOOKS          books whose moves count as signal
                      (default "bovada,betonlineag,mybookieag,circasports")
  STEAM_BIG_MOVE      single-book drastic move threshold in prob points
                      (default 0.035 ~= -110 -> -128) -> its own ⚡ alert
  BETTABLE_BOOKS      your books — lagging ones get flagged as YOUR SHOT
  STEAM_SHARP_ONLY    "1" (default) = only alert sharp-led steam;
                      soft-book-only shuffles are logged, not alerted
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

import requests

from .db import get_conn
from .devig import american_to_prob

log = logging.getLogger("kprop.steam")

DDL = """
CREATE TABLE IF NOT EXISTS steam_seen (
    prop_key TEXT, direction TEXT, alerted_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_steam_seen ON steam_seen (prop_key, alerted_at)
"""

MOVES_SQL = """
WITH windowed AS (
    SELECT player, market, line, bookmaker, over_price, under_price,
           fetched_at,
           row_number() OVER (PARTITION BY player, market, line, bookmaker
                              ORDER BY fetched_at DESC) AS rn_new,
           row_number() OVER (PARTITION BY player, market, line, bookmaker
                              ORDER BY fetched_at ASC) AS rn_old
    FROM odds_snapshots
    WHERE commence_time > now() AND commence_time < now() + interval '24 hours'
      AND fetched_at > now() - make_interval(mins => %s)
      AND player IS NOT NULL AND over_price IS NOT NULL
)
SELECT n.player, n.market, n.line, n.bookmaker,
       o.over_price AS over_then, n.over_price AS over_now,
       o.under_price AS under_then, n.under_price AS under_now
FROM windowed n
JOIN windowed o USING (player, market, line, bookmaker)
WHERE n.rn_new = 1 AND o.rn_old = 1 AND n.fetched_at > o.fetched_at
"""


def _post(msg: dict):
    # Opt-in only: without STEAM_WEBHOOK_URL the module stays silent.
    url = os.environ.get("STEAM_WEBHOOK_URL")
    if not url:
        return
    msg.setdefault("username", "Steam 🚂")
    try:
        requests.post(url, json=msg, timeout=15)
    except Exception:
        log.exception("steam post failed")


def scan() -> int:
    min_books = int(os.environ.get("STEAM_MIN_BOOKS", "3"))
    min_move = float(os.environ.get("STEAM_MIN_MOVE", "0.02"))
    window = int(os.environ.get("STEAM_WINDOW_MIN", "50"))
    fired = 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(MOVES_SQL, (window,))
        props = defaultdict(list)
        for (player, market, line, book,
             over_then, over_now, under_then, under_now) in cur.fetchall():
            if over_then is None or over_now is None:
                continue
            delta = (american_to_prob(int(over_now))
                     - american_to_prob(int(over_then)))
            props[(player, market, float(line) if line is not None else None)
                  ].append((book, delta, int(over_now)))

        lead = {b.strip() for b in os.environ.get(
            "LEAD_BOOKS", "bovada,betonlineag,mybookieag,circasports").split(",")}
        big_move = float(os.environ.get("STEAM_BIG_MOVE", "0.035"))
        bettable = {b.strip() for b in os.environ.get(
            "BETTABLE_BOOKS", "").split(",") if b.strip()}
        sharp_only = os.environ.get("STEAM_SHARP_ONLY", "1") == "1"

        for (player, market, line), moves in props.items():
            # ⚡ single-book drastic move on a lead book: its own alert
            for book, delta, px_now in moves:
                if book not in lead or abs(delta) < big_move:
                    continue
                dkey = f"big|{book}|{player}|{market}|{line}"
                cur.execute("""SELECT 1 FROM steam_seen
                               WHERE prop_key=%s AND direction=%s
                                 AND alerted_at > now() - interval '3 hours'""",
                            (dkey, "over" if delta > 0 else "under"))
                if cur.fetchone():
                    continue
                mk2 = market.replace("pitcher_", "").replace("_", " ")
                _post({"embeds": [{
                    "title": f"⚡ BIG MOVE · {book} · {player} — {mk2} {line}",
                    "color": 0xF0B54A,
                    "description": (
                        f"{book} just moved **{delta*100:+.1f} pts** "
                        f"({'toward OVER' if delta > 0 else 'toward UNDER'}) "
                        f"in <{window}m — now {px_now:+d}.\n"
                        f"_Drastic single-book reprice: either they took a "
                        f"position or they're correcting a mistake. Check "
                        f"other books' stale prices now._"),
                }]})
                cur.execute("INSERT INTO steam_seen (prop_key, direction) "
                            "VALUES (%s, %s)",
                            (dkey, "over" if delta > 0 else "under"))
                fired += 1

            up = [m for m in moves if m[1] >= min_move]     # toward the over
            dn = [m for m in moves if m[1] <= -min_move]    # toward the under
            for direction, movers in (("over", up), ("under", dn)):
                if len(movers) < min_books:
                    continue
                sharp_movers = [m for m in movers if m[0] in lead]
                if sharp_only and not sharp_movers:
                    log.info("soft-only shuffle ignored: %s %s %s",
                             player, market, direction)
                    continue
                prop_key = f"{player}|{market}|{line}"
                cur.execute("""SELECT 1 FROM steam_seen
                               WHERE prop_key=%s AND direction=%s
                                 AND alerted_at > now() - interval '3 hours'""",
                            (prop_key, direction))
                if cur.fetchone():
                    continue
                laggards = [m for m in moves if abs(m[1]) < min_move / 2]
                your_shots = [m for m in laggards if m[0] in bettable]
                other_lag = [m for m in laggards if m[0] not in bettable]
                avg = sum(m[1] for m in movers) / len(movers) * 100

                # does the model agree with the steam direction?
                cur.execute("""SELECT model_prob FROM edges
                               WHERE pitcher_name=%s AND market=%s AND line=%s
                                 AND side=%s
                               ORDER BY created_at DESC LIMIT 1""",
                            (player, market, line, direction))
                mrow = cur.fetchone()
                if mrow is not None:
                    mp = float(mrow[0])
                    verdict = ("✅ model agrees" if mp > 0.52 else
                               "❌ model disagrees" if mp < 0.48 else
                               "➖ model neutral")
                    model_s = f"{verdict} ({mp:.0%} {direction})"
                else:
                    model_s = "model: no read on this line"

                mk = market.replace("pitcher_", "").replace("_", " ")
                heat = "🚂🚂🚂" if (len(sharp_movers) >= 2 and abs(avg) >= 3)                     else "🚂🚂" if sharp_movers else "🚂"
                shot_s = (", ".join(f"**{b} {p:+d}**" for b, _d, p in your_shots[:4])
                          if your_shots else "none of your books lagging")
                other_s = (", ".join(f"{b} {p:+d}" for b, _d, p in other_lag[:3])
                           if other_lag else "—")
                payload = {"embeds": [{
                    "title": f"{heat} STEAM · {player} — {mk} {line} "
                             f"({'toward OVER' if direction == 'over' else 'toward UNDER'})",
                    "color": 0x5AA7FF if sharp_movers else 0x8A93A6,
                    "description": (
                        f"**Moved {avg:+.1f} pts** in {window}m — "
                        f"sharp: {', '.join(b for b,_d,_p in sharp_movers) or '—'}"
                        f" · others: "
                        f"{', '.join(b for b,_d,_p in movers if b not in lead) or '—'}\n"
                        f"🎯 **YOUR SHOT (not moved):** {shot_s}\n"
                        f"Other laggards: {other_s}\n"
                        f"{model_s}"),
                }]}
                if your_shots and sharp_movers and mrow is not None and mp > 0.52:
                    payload["content"] = "@here 🚂 sharp steam + your book lagging + model agrees"
                _post(payload)
                cur.execute("INSERT INTO steam_seen (prop_key, direction) "
                            "VALUES (%s, %s)", (prop_key, direction))
                fired += 1
        conn.commit()
    if fired:
        log.info("steam: %d alerts", fired)
    return fired


def main():
    logging.basicConfig(level=logging.INFO)
    scan()


if __name__ == "__main__":
    main()
