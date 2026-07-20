"""Odds logger: polls The Odds API and writes immutable snapshots to Postgres.

Run modes:
    python -m kprop.odds_logger once     # single fetch cycle
    python -m kprop.odds_logger loop     # continuous (Railway worker)

Env vars:
    ODDS_API_KEY      required
    DATABASE_URL      required (Railway Postgres)
    POLL_MINUTES      default 30 (drops to POLL_MINUTES_NEAR_START near first pitch)
    PROP_MARKETS      comma-separated override of default prop markets
    BOOKMAKERS        optional comma-separated book filter to save quota
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from .db import get_conn

log = logging.getLogger("kprop.odds_logger")

BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"

# Core prop markets + alternates. Alternate ladders are where the
# distribution-tail edge lives, so we log them from day one.
# Trimmed for speed: every market costs credits per cycle, and cycle speed
# is what makes Dawn Patrol / Fresh Drop useful. Alternates kept only for K
# (that's where the tail edge lives). Re-add markets via PROP_MARKETS env.
DEFAULT_PROP_MARKETS = [
    "pitcher_strikeouts",
    "pitcher_strikeouts_alternate",
    "pitcher_outs",
    "pitcher_hits_allowed",
    "pitcher_walks",
    "pitcher_earned_runs",
]
GAME_MARKETS = ["h2h", "totals", "spreads"]


def _prop_markets() -> list[str]:
    env = os.environ.get("PROP_MARKETS")
    return [m.strip() for m in env.split(",")] if env else DEFAULT_PROP_MARKETS


class OddsAPI:
    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.key = api_key or os.environ["ODDS_API_KEY"]
        self.http = session or requests.Session()
        self.remaining: str | None = None
        self.used_today = 0

    def _get(self, path: str, **params):
        params["apiKey"] = self.key
        r = self.http.get(f"{BASE}{path}", params=params, timeout=30)
        self.remaining = r.headers.get("x-requests-remaining")
        last = r.headers.get("x-requests-last")
        if last:
            try:
                self.used_today += int(float(last))
            except ValueError:
                pass
        r.raise_for_status()
        return r.json()

    def events(self) -> list[dict]:
        """Upcoming/live MLB events. Costs 0 quota."""
        return self._get(f"/sports/{SPORT}/events")

    def event_odds(self, event_id: str, markets: list[str], bookmakers: str | None = None) -> dict:
        params = {
            "markets": ",".join(markets),
            "oddsFormat": "american",
        }
        if bookmakers:
            params["bookmakers"] = bookmakers
        else:
            params["regions"] = os.environ.get("REGIONS", "us,us_dfs")
        return self._get(f"/sports/{SPORT}/events/{event_id}/odds", **params)


# ---------------------------------------------------------------- parsing

def parse_prop_rows(payload: dict, fetched_at: datetime) -> list[tuple]:
    """Flatten an event-odds payload into odds_snapshots rows.

    Player prop outcomes come as name=Over/Under, description=player, point=line.
    Over/Under pairs are joined on (book, market, player, line).
    """
    rows: list[tuple] = []
    ev = payload
    for bk in ev.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt["key"] in GAME_MARKETS:
                continue
            pairs: dict[tuple, dict] = {}
            for oc in mkt.get("outcomes", []):
                player = oc.get("description")
                line = oc.get("point")
                key = (player, line)
                slot = pairs.setdefault(key, {})
                side = (oc.get("name") or "").lower()
                if side in ("over", "under"):
                    slot[side] = oc.get("price")
                else:  # yes/no style markets (e.g. record_a_win)
                    slot[side or "yes"] = oc.get("price")
            for (player, line), prices in pairs.items():
                rows.append((
                    fetched_at, "oddsapi", ev["id"], ev["commence_time"],
                    ev["home_team"], ev["away_team"], bk["key"], mkt["key"],
                    player, line,
                    prices.get("over") or prices.get("yes"),
                    prices.get("under") or prices.get("no"),
                    mkt.get("last_update"),
                ))
    return rows


def parse_game_rows(payload: dict, fetched_at: datetime) -> list[tuple]:
    rows: list[tuple] = []
    ev = payload
    for bk in ev.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt["key"] not in GAME_MARKETS:
                continue
            for oc in mkt.get("outcomes", []):
                rows.append((
                    fetched_at, ev["id"], ev["commence_time"], ev["home_team"],
                    ev["away_team"], bk["key"], mkt["key"], oc.get("name"),
                    oc.get("point"), oc.get("price"),
                ))
    return rows


# ---------------------------------------------------------------- persistence

PROP_INSERT = """
INSERT INTO odds_snapshots
(fetched_at, source, event_id, commence_time, home_team, away_team,
 bookmaker, market, player, line, over_price, under_price, book_last_update)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""

GAME_INSERT = """
INSERT INTO game_odds_snapshots
(fetched_at, event_id, commence_time, home_team, away_team,
 bookmaker, market, outcome_name, line, price)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


def run_cycle(api: OddsAPI) -> int:
    """One full fetch: all events within the horizon, props + game lines."""
    fetched_at = datetime.now(timezone.utc)
    horizon = fetched_at + timedelta(hours=36)
    bookmakers = os.environ.get("BOOKMAKERS")
    total = 0

    events = api.events()
    with get_conn() as conn, conn.cursor() as cur:
        for ev in events:
            start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            if start > horizon:
                continue
            try:
                payload = api.event_odds(ev["id"], _prop_markets() + GAME_MARKETS,
                                         bookmakers)
            except requests.HTTPError as e:
                log.warning("fetch failed for %s: %s", ev["id"], e)
                continue
            prop_rows = parse_prop_rows(payload, fetched_at)
            game_rows = parse_game_rows(payload, fetched_at)
            if prop_rows:
                cur.executemany(PROP_INSERT, prop_rows)
            if game_rows:
                cur.executemany(GAME_INSERT, game_rows)
            total += len(prop_rows) + len(game_rows)
        conn.commit()
    log.info("cycle complete: %d rows, quota remaining=%s", total, api.remaining)
    return total


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"
    api = OddsAPI()
    if mode == "once":
        run_cycle(api)
        return
    poll = int(os.environ.get("POLL_MINUTES", "30"))
    poll_near = int(os.environ.get("POLL_MINUTES_NEAR_START", "12"))
    budget = int(os.environ.get("DAILY_CREDIT_BUDGET", "3000"))
    day = datetime.now(timezone.utc).date()
    while True:
        now = datetime.now(timezone.utc)
        if now.date() != day:
            day = now.date()
            api.used_today = 0
        if api.used_today >= budget:
            log.warning("daily credit budget %d hit (used ~%d); sleeping 1h",
                        budget, api.used_today)
            time.sleep(3600)
            continue
        try:
            run_cycle(api)
        except Exception:
            log.exception("cycle failed")
        if os.environ.get("WATCH_EDGES", "1") == "1":
            try:
                from .watch import scan
                scan()
            except Exception:
                log.exception("edge watcher failed")
            if os.environ.get("STEAM_WEBHOOK_URL"):
                try:
                    from .steam import scan as steam_scan
                    steam_scan()
                except Exception:
                    log.exception("steam scan failed")
            try:
                from .listings import scan as listings_scan
                listings_scan()
            except Exception:
                log.exception("listings scan failed")
        # fast cadence during the opener window (when books hang new lines)
        opener_hours = {int(h) for h in os.environ.get(
            "OPENER_POLL_UTC_HOURS", "13,14,15,16").split(",")}
        # if any game starts within 3h, poll at the faster cadence
        near = datetime.now(timezone.utc).hour in opener_hours
        try:
            for ev in api.events():
                st = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
                if timedelta(0) < st - datetime.now(timezone.utc) < timedelta(hours=3):
                    near = True
                    break
        except Exception:
            pass
        time.sleep((poll_near if near else poll) * 60)


if __name__ == "__main__":
    main()
