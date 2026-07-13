"""Fair value engine — pulls sharp odds from The Odds API and devigs."""
import logging
import os
import time

import requests

log = logging.getLogger("fair")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
SPORT = os.environ.get("ODDS_SPORT", "baseball_mlb")
# Prefer sharp books; fall back to consensus of whatever is available
SHARP_BOOKS = ["pinnacle", "circasports", "betonlineag"]
# NOTE: The Odds API charges (markets x regions) credits per call.
# us,eu = 2 credits/call. Monthly usage ~= 2 * 86400/CACHE_SECS * 30.
#   CACHE_SECS=90  -> ~57K/mo  (needs 100K tier, ~$59/mo)  <- recommended
#   CACHE_SECS=300 -> ~17K/mo  (fits 20K tier, ~$30/mo, slower reactions)
REGIONS = os.environ.get("ODDS_REGIONS", "us,eu")

_cache = {"ts": 0, "games": []}
CACHE_SECS = int(os.environ.get("ODDS_CACHE_SECS", "90"))


def american_to_prob(odds: float) -> float:
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def devig_two_way(p_a: float, p_b: float):
    total = p_a + p_b
    if total <= 0:
        return None, None
    return p_a / total, p_b / total


def fetch_games():
    """Return list of {home, away, commence, home_prob, away_prob, books_used}."""
    now = time.time()
    if now - _cache["ts"] < CACHE_SECS and _cache["games"]:
        return _cache["games"]

    if not ODDS_API_KEY:
        log.error("ODDS_API_KEY missing")
        return []

    url = f"https://api.the-odds-api.com/v4/sports/{SPORT}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": "h2h",
        "oddsFormat": "american",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            log.warning(f"odds api {r.status_code}: {r.text[:200]}")
            return _cache["games"]  # stale ok briefly; staleness gate is in main
        data = r.json()
    except requests.RequestException as e:
        log.warning(f"odds api network error: {e}")
        return _cache["games"]

    games = []
    for g in data:
        home, away = g.get("home_team"), g.get("away_team")
        commence = g.get("commence_time")
        # Collect devigged probs per book, prefer sharps
        sharp_probs, all_probs = [], []
        for bk in g.get("bookmakers", []):
            key = bk.get("key", "")
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                ph = pa = None
                for oc in mkt.get("outcomes", []):
                    if oc.get("name") == home:
                        ph = american_to_prob(oc.get("price", 0))
                    elif oc.get("name") == away:
                        pa = american_to_prob(oc.get("price", 0))
                if ph and pa:
                    dh, da = devig_two_way(ph, pa)
                    if dh:
                        all_probs.append((dh, da))
                        if key in SHARP_BOOKS:
                            sharp_probs.append((dh, da))
        probs = sharp_probs or all_probs
        if not probs:
            continue
        home_prob = sum(p[0] for p in probs) / len(probs)
        away_prob = sum(p[1] for p in probs) / len(probs)
        games.append({
            "home": home, "away": away, "commence": commence,
            "home_prob": home_prob, "away_prob": away_prob,
            "n_books": len(probs), "sharp": bool(sharp_probs),
        })

    _cache["ts"] = now
    _cache["games"] = games
    log.info(f"fair values refreshed: {len(games)} games")
    return games


def cache_age() -> float:
    return time.time() - _cache["ts"]
