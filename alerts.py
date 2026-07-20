"""Discord alerts: tiered, per-market channels, rich embeds.

Routing: each market family can have its own channel webhook. Falls back to
DISCORD_WEBHOOK_URL for anything without a dedicated one.

  DISCORD_WEBHOOK_K       pitcher_strikeouts (+alternates)
  DISCORD_WEBHOOK_OUTS    pitcher_outs
  DISCORD_WEBHOOK_HITS    pitcher_hits_allowed (+alternates)
  DISCORD_WEBHOOK_WALKS   pitcher_walks (+alternates)
  DISCORD_WEBHOOK_ER      pitcher_earned_runs
  DISCORD_WEBHOOK_URL     fallback + daily health heartbeat

Only SOLID and STRONG tiers are pinged (ALERT_MIN_TIER=watch to loosen).
STRONG edges lead the message and get the green embed; the tier system is
shared with the dashboard via kprop.tiers so both always agree.

Other env: BANKROLL (adds ¼-Kelly stake), ALERT_MIN_EV floor still applies.
"""
from __future__ import annotations

import logging
import os
from datetime import date

import requests

from .db import get_conn
from .devig import prob_to_american
from .tiers import COLOR, EMOJI, classify

log = logging.getLogger("kprop.alerts")

TIER_RANK = {"watch": 0, "solid": 1, "strong": 2}

MARKET_WEBHOOK = [
    ("pitcher_strikeouts", "DISCORD_WEBHOOK_K"),
    ("pitcher_outs", "DISCORD_WEBHOOK_OUTS"),
    ("pitcher_hits_allowed", "DISCORD_WEBHOOK_HITS"),
    ("pitcher_walks", "DISCORD_WEBHOOK_WALKS"),
    ("pitcher_earned_runs", "DISCORD_WEBHOOK_ER"),
]
MARKET_LABEL = {
    "pitcher_strikeouts": "Strikeouts", "pitcher_strikeouts_alternate": "K alt",
    "pitcher_outs": "Outs", "pitcher_hits_allowed": "Hits allowed",
    "pitcher_hits_allowed_alternate": "Hits alt", "pitcher_walks": "Walks",
    "pitcher_walks_alternate": "Walks alt", "pitcher_earned_runs": "Earned runs",
}


def _webhook_for(market: str) -> str | None:
    for prefix, env in MARKET_WEBHOOK:
        if market.startswith(prefix):
            url = os.environ.get(env)
            if url:
                return url
    return os.environ.get("DISCORD_WEBHOOK_URL")


def _blend_weight(market: str = "") -> float:
    from .tiers import blend_weight
    return blend_weight(market)


def _cal(p: float) -> float:
    from .tiers import calibrated_prob
    return calibrated_prob(p)


def _embed(row, bankroll: float) -> dict:
    (eid, name, market, side, line, price, book, p_model, p_fair, ev,
     tier, opener) = row
    p_model = float(p_model)
    wm = _blend_weight(market)
    p_cal = _cal(p_model)
    # THE fair price: calibrated model blended with de-vigged consensus.
    if p_fair is not None:
        p_bet = wm * p_cal + (1 - wm) * float(p_fair)
        gap = f"{(p_cal - float(p_fair)) * 100:+.1f} pts vs market"
    else:
        p_bet = p_cal
        gap = "no market consensus"
    fair_px = prob_to_american(min(max(p_bet, 0.01), 0.99))
    raw_px = prob_to_american(min(max(p_model, 0.01), 0.99))
    stake = ""
    if bankroll > 0:
        pr = int(price)
        b = pr / 100 if pr > 0 else 100 / -pr
        kelly = max(0.0, (p_bet * (b + 1) - 1) / b)
        stake = f"\n**Stake (¼-Kelly):** ${bankroll * kelly / 4:.0f}"
    mlabel = MARKET_LABEL.get(market, market)
    fresh = " · 🆕 opener" if opener else ""
    return {
        "title": f"{EMOJI[tier]} {tier.upper()} · {name} — {mlabel} "
                 f"{side} {line}{fresh}",
        "color": COLOR[tier],
        "description": (
            f"**Bet:** {side} {line} at **{int(price):+d}** ({book})\n"
            f"**Fair price:** {fair_px:+d} (market-weighted · raw model "
            f"{raw_px:+d}, {p_model:.0%})\n"
            f"**Edge:** {gap}  ·  **EV {float(ev):+.1f}%**{stake}"
        ),
    }


def _line_age_min(cur, name: str, market: str, line, book: str):
    cur.execute("""SELECT min(fetched_at) FROM odds_snapshots
                   WHERE player=%s AND market=%s AND line=%s AND bookmaker=%s
                     AND commence_time > now()""", (name, market, line, book))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    from datetime import datetime, timezone
    return int((datetime.now(timezone.utc) - row[0]).total_seconds() // 60)


def _bettable() -> set[str] | None:
    env = os.environ.get("BETTABLE_BOOKS", "").strip()
    return {b.strip() for b in env.split(",") if b.strip()} or None if env else None


def send_top_edges(d: date, limit: int = 5):
    min_ev = float(os.environ.get("ALERT_MIN_EV", "4.0"))
    bettable = _bettable()
    min_gap = float(os.environ.get("ALERT_MIN_FAIR_GAP", "0.02"))
    min_tier = TIER_RANK.get(os.environ.get("ALERT_MIN_TIER", "solid"), 1)
    bankroll = float(os.environ.get("BANKROLL", "0"))

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, pitcher_name, market, side, line, price, bookmaker,
                   model_prob, market_fair_prob, ev_pct
            FROM edges
            WHERE game_date = %s AND alerted = false AND ev_pct >= %s
              AND (market_fair_prob IS NULL
                   OR model_prob - market_fair_prob >= %s)
            ORDER BY ev_pct DESC
            LIMIT %s
        """, (d, min_ev, min_gap, limit * 3))
        rows = cur.fetchall()
        if not rows:
            log.info("no edges above floor")
            return

        # classify, filter to tier threshold, group by destination webhook
        by_hook: dict[str, list] = {}
        alert_ids, seen_ids = [], []
        for r in rows:
            (eid, name, market, side, line, price, book, p_model, p_fair, ev) = r
            tier = classify(float(ev), float(p_model),
                            float(p_fair) if p_fair is not None else None,
                            book, market)
            seen_ids.append(eid)
            if TIER_RANK[tier] < min_tier:
                continue
            url = _webhook_for(market)
            if not url:
                continue
            if bettable is not None and book not in bettable:
                continue           # never alert a book you can't bet
            age = _line_age_min(cur, name, market, line, book)
            opener = age is not None and age <= int(
                os.environ.get("OPENER_MAX_AGE_MIN", "120"))
            by_hook.setdefault(url, []).append((*r, tier, opener))

        ping_strong = os.environ.get("ALERT_PING_STRONG", "1") == "1"
        sent = 0
        for url, items in by_hook.items():
            items.sort(key=lambda x: (-TIER_RANK[x[-2]], -float(x[9])))
            embeds = [_embed(i, bankroll) for i in items[:limit]]
            payload = {"embeds": embeds}
            if ping_strong and any(i[-2] == "strong" for i in items[:limit]):
                payload["content"] = "@here 🔥 strong edge"
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code < 300:
                alert_ids += [i[0] for i in items[:limit]]
                sent += len(embeds)
            else:
                log.warning("discord post failed: %s %s",
                            resp.status_code, resp.text[:200])

        # Early Board 🌅: young lines with real tiers -> the early channel
        dawn_url = (os.environ.get("EARLY_WEBHOOK_URL")
                    or os.environ.get("OPENERS_WEBHOOK_URL")
                    or os.environ.get("LISTINGS_WEBHOOK_URL"))
        if dawn_url:
            dawn = [i for items in by_hook.values() for i in items
                    if i[-1] and TIER_RANK[i[-2]] >= 1]
            dawn.sort(key=lambda x: (-TIER_RANK[x[-2]], -float(x[9])))
            if dawn:
                payload = {"username": "Early Board 🌅",
                           "embeds": [_embed(i, bankroll) for i in dawn[:5]]}
                if ping_strong and any(i[-2] == "strong" for i in dawn[:5]):
                    payload["content"] = "@here 🌅 opener edge"
                try:
                    requests.post(dawn_url, json=payload, timeout=15)
                except Exception:
                    log.exception("dawn patrol post failed")

        # mark everything we evaluated so WATCH-tier rows don't re-trigger
        # every cycle; they remain visible on the dashboard
        if seen_ids:
            cur.execute("UPDATE edges SET alerted = true WHERE id = ANY(%s)",
                        (seen_ids,))
            conn.commit()
        log.info("alerted %d edges across %d channels", sent, len(by_hook))
