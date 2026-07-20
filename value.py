"""Value scanner: find exchange lines that disagree with the sharp market —
no model required.

The thesis (validated by research): Novig and ProphetX are peer-to-peer
exchanges. Their prices are set by bettors, not a trading desk, so they drift
from the sharp consensus (Pinnacle + top US books). ANY exchange line whose
de-vigged price is meaningfully better than the sharp de-vigged fair is a
positive-EV bet — across EVERY market we log (props we don't model, plus
moneyline / totals / run lines), not just the four the sim covers.

This complements the sim: the pitcher model finds edges the market hasn't
priced; the scanner finds edges where the EXCHANGE is simply mispriced vs
everyone else. Both feed the same alert + LeBron pipeline.

Runs each logger cycle. Alerts to VALUE_WEBHOOK_URL (own channel) or the
main edges webhook.

Env:
  SHARP_BOOKS       de-vigged consensus source
                    (default "pinnacle,betonlineag,circasports,lowvig")
  EXCHANGE_BOOKS    where we can actually bet (default "novig,prophetx")
  VALUE_MIN_EV      min EV% to surface (default 3.0)
  VALUE_MIN_SHARP   min sharp books needed for a trustworthy consensus (2)
  VALUE_WEBHOOK_URL alert channel (falls back to DISCORD_WEBHOOK_URL)
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date

import requests

from .db import get_conn
from .devig import american_to_prob, book_fees, ev_per_dollar, fair_probs, prob_to_american

log = logging.getLogger("kprop.value")

DDL = """
CREATE TABLE IF NOT EXISTS value_seen (
    sig TEXT PRIMARY KEY, alerted_at TIMESTAMPTZ DEFAULT now()
)
"""

# latest two-sided quote per (event, market, line, side-pair, book) today
SNAP_SQL = """
SELECT DISTINCT ON (event_id, market, line, bookmaker, player)
       event_id, market, line, bookmaker, player, over_price, under_price,
       commence_time
FROM odds_snapshots
WHERE commence_time > now() AND commence_time < now() + interval '30 hours'
  AND fetched_at > now() - interval '90 minutes'
  AND over_price IS NOT NULL AND under_price IS NOT NULL
ORDER BY event_id, market, line, bookmaker, player, fetched_at DESC
"""

PRETTY = {
    "h2h": "Moneyline", "totals": "Total", "spreads": "Run line",
    "batter_home_runs": "HR", "batter_hits": "Hits", "batter_total_bases": "TB",
    "batter_rbis": "RBIs", "batter_runs_scored": "Runs",
    "batter_hits_runs_rbis": "H+R+RBI", "batter_stolen_bases": "SB",
    "pitcher_strikeouts": "K", "pitcher_outs": "Outs",
    "pitcher_hits_allowed": "Hits allowed", "pitcher_walks": "Walks",
    "pitcher_earned_runs": "ER",
}


def _hook():
    return os.environ.get("VALUE_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL")


def scan() -> int:
    sharp = {b.strip() for b in os.environ.get(
        "SHARP_BOOKS", "pinnacle,betonlineag,circasports,lowvig").split(",")}
    exch = {b.strip() for b in os.environ.get(
        "EXCHANGE_BOOKS", "novig,prophetx").split(",")}
    min_ev = float(os.environ.get("VALUE_MIN_EV", "3.0"))
    min_sharp = int(os.environ.get("VALUE_MIN_SHARP", "2"))
    fees = book_fees()
    url = _hook()
    if not url:
        return 0

    fired = 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute(SNAP_SQL)
        rows = cur.fetchall()

        # group quotes by proposition (event, market, line, player)
        props = defaultdict(dict)
        meta = {}
        for (eid, market, line, book, player, op, up, ct) in rows:
            key = (eid, market, line, player)
            props[key][book] = (int(op), int(up))
            meta[key] = ct

        alerts = []
        for key, books in props.items():
            eid, market, line, player = key
            # sharp de-vigged consensus (over probability)
            sharp_ov = []
            for b in sharp & books.keys():
                op, up = books[b]
                sharp_ov.append(fair_probs(op, up)[0])
            if len(sharp_ov) < min_sharp:
                continue
            sharp_ov.sort()
            mid = sharp_ov[len(sharp_ov) // 2]      # median over-prob

            # check each exchange side for value vs that consensus
            for b in exch & books.keys():
                op, up = books[b]
                for side, price, fair_p in (("over", op, mid),
                                            ("under", up, 1 - mid)):
                    ev = ev_per_dollar(fair_p, price, fees.get(b, 0.0)) * 100
                    if ev < min_ev:
                        continue
                    sig = f"{eid}|{market}|{line}|{player}|{side}|{b}"
                    cur.execute("SELECT 1 FROM value_seen WHERE sig=%s AND "
                                "alerted_at > now() - interval '4 hours'", (sig,))
                    if cur.fetchone():
                        continue
                    alerts.append((ev, side, price, fair_p, b, market, line,
                                   player, sig))

        alerts.sort(reverse=True)
        embeds = []
        for (ev, side, price, fair_p, book, market, line, player, sig) in alerts[:8]:
            fair_px = prob_to_american(min(max(fair_p, .01), .99))
            label = PRETTY.get(market, market)
            who = player if player else ""
            pt = f" {line}" if line is not None else ""
            embeds.append({
                "title": f"💎 VALUE · {who or label} {label if who else ''} "
                         f"{side}{pt}",
                "color": 0x8CE0FF,
                "description": (
                    f"**{book}** {int(price):+d} vs sharp fair **{fair_px:+d}**\n"
                    f"**EV {ev:+.1f}%** — exchange mispriced vs "
                    f"{label} sharp consensus.\n"
                    f"_No model, pure line value. Verify it's live._"),
            })
            cur.execute("INSERT INTO value_seen (sig) VALUES (%s) "
                        "ON CONFLICT DO NOTHING", (sig,))
            fired += 1
        if embeds:
            try:
                requests.post(url, json={"username": "Value 💎", "embeds": embeds},
                              timeout=15)
            except Exception:
                log.exception("value post failed")
        conn.commit()
    if fired:
        log.info("value scanner: %d exchange mispricings", fired)
    return fired


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    scan()


if __name__ == "__main__":
    main()
