"""Fair value engine — pulls sharp odds from The Odds API, devigs, and
measures book DISAGREEMENT (uncertainty) so the quoter can widen or skip
markets where the consensus is shaky (item C11).

Also captures the x-requests-remaining quota header (item A4) and records
every snapshot to SQLite (item B6).
"""
import logging
import statistics
import time

import requests

import store
from config import (ODDS_API_KEY, ODDS_SPORT, ODDS_REGIONS, ODDS_CACHE_SECS,
                    MIN_BOOKS, DEVIG_METHOD, CONSENSUS)
from notify import scrub

log = logging.getLogger("fair")

# Prefer sharp books; fall back to consensus of whatever is available
SHARP_BOOKS = ["pinnacle", "circasports", "betonlineag"]

_cache = {"ts": 0, "games": []}
# Odds API quota, read from response headers; watchdog alerts on this.
quota = {"remaining": None, "used": None, "checked": 0}


def american_to_prob(odds: float) -> float:
    if odds < 0:
        return -odds / (-odds + 100)
    return 100 / (odds + 100)


def devig_two_way(p_a: float, p_b: float):
    """Multiplicative devig: spread vig proportionally."""
    total = p_a + p_b
    if total <= 0:
        return None, None
    return p_a / total, p_b / total


def devig_power(p_a: float, p_b: float):
    """Power devig: solve k so p_a^k + p_b^k = 1 (bisection).
    Corrects the multiplicative method's known overstatement of longshot
    probabilities on lopsided lines — favorites get a touch more fair,
    longshots a touch less. Equal-odds lines are unchanged."""
    if p_a <= 0 or p_b <= 0 or p_a + p_b <= 1.0:
        return devig_two_way(p_a, p_b)
    lo, hi = 1.0, 10.0
    for _ in range(60):
        k = (lo + hi) / 2
        s = p_a ** k + p_b ** k
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return p_a ** k, p_b ** k


def devig(p_a: float, p_b: float):
    if DEVIG_METHOD == "power":
        return devig_power(p_a, p_b)
    return devig_two_way(p_a, p_b)


def devig_three_way(p_h: float, p_d: float, p_a: float):
    """Soccer: home / draw / away. Multiplicative devig across three
    outcomes (power method is ill-posed on 3-way; multiplicative is the
    standard and the vig is spread proportionally).
    Returns (home, draw, away) summing to 1, or (None, None, None)."""
    total = (p_h or 0) + (p_d or 0) + (p_a or 0)
    if total <= 0:
        return None, None, None
    return p_h / total, p_d / total, p_a / total


def fetch_games():
    """Return list of {home, away, commence, home_prob, away_prob,
    n_books, sharp, uncertainty, books}.

    uncertainty = stddev of devigged home-prob across the books used for
    consensus (prob points, 0-1). High = books disagree = shaky fair.
    """
    now = time.time()
    if now - _cache["ts"] < ODDS_CACHE_SECS and _cache["games"]:
        return _cache["games"]

    if not ODDS_API_KEY:
        log.error("ODDS_API_KEY missing")
        return []

    url = f"https://api.the-odds-api.com/v4/sports/{ODDS_SPORT}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_REGIONS,
        "markets": "h2h",
        "oddsFormat": "american",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        rem = r.headers.get("x-requests-remaining")
        used = r.headers.get("x-requests-used")
        if rem is not None:
            quota.update({"remaining": float(rem),
                          "used": float(used) if used else None,
                          "checked": now})
        if r.status_code != 200:
            log.warning(f"odds api {r.status_code}: {scrub(r.text[:200])}")
            return _cache["games"]  # stale ok briefly; staleness gate in main
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"odds api error: {scrub(str(e))}")
        return _cache["games"]

    games = []
    for g in data:
        home, away = g.get("home_team"), g.get("away_team")
        commence = g.get("commence_time")
        sharp_probs, all_probs = [], []
        books = []
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
                    dh, da = devig(ph, pa)
                    if dh:
                        all_probs.append((dh, da))
                        books.append({"book": key, "home": round(dh, 4)})
                        if key in SHARP_BOOKS:
                            sharp_probs.append((dh, da))
        probs = sharp_probs or all_probs
        if len(probs) < MIN_BOOKS:
            continue
        if CONSENSUS == "median":
            # robust to a single stale/outlier book dragging the fair
            home_prob = statistics.median(p[0] for p in probs)
            away_prob = 1.0 - home_prob
        else:
            home_prob = sum(p[0] for p in probs) / len(probs)
            away_prob = sum(p[1] for p in probs) / len(probs)
        # Disagreement across ALL books (not just sharps) — a sharp/soft
        # split is itself a signal the number is in play.
        pool = [p[0] for p in all_probs] if len(all_probs) >= 2 else [p[0] for p in probs]
        uncertainty = statistics.pstdev(pool) if len(pool) >= 2 else 0.0
        games.append({
            "home": home, "away": away, "commence": commence,
            "home_prob": home_prob, "away_prob": away_prob,
            "n_books": len(probs), "sharp": bool(sharp_probs),
            "uncertainty": uncertainty, "books": books,
        })

    _cache["ts"] = now
    _cache["games"] = games
    log.info(f"fair values refreshed: {len(games)} games "
             f"(quota remaining: {quota['remaining']})")
    try:
        store.record_odds_snapshot(games)
    except Exception:
        log.exception("failed to record odds snapshot")
    return games


def cache_age() -> float:
    return time.time() - _cache["ts"]


# ── multi-sport, multi-market fetch (spreads/totals ladders) ──────────
_sport_cache: dict = {}   # sport -> {"ts": t, "games": [...]}


def fetch_sport(sport: str, markets: str = "h2h,spreads,totals",
                regions: str = "us", cache_secs: int = 180):
    """Odds for another sport with per-book spread/total lines attached:
    game dicts gain spread_lines [(home_point, p_home_cover)] and
    total_lines [(line, p_over)] for distribution fitting."""
    now = time.time()
    ent = _sport_cache.get(sport)
    if ent and now - ent["ts"] < cache_secs and ent["games"]:
        return ent["games"]
    if not ODDS_API_KEY:
        return []
    url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": regions,
              "markets": markets, "oddsFormat": "american"}
    try:
        r = requests.get(url, params=params, timeout=15)
        rem = r.headers.get("x-requests-remaining")
        if rem is not None:
            quota.update({"remaining": float(rem), "checked": now})
        if r.status_code != 200:
            log.warning(f"odds api [{sport}] {r.status_code}: "
                        f"{scrub(r.text[:150])}")
            return (ent or {}).get("games", [])
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning(f"odds api [{sport}] error: {scrub(str(e))}")
        if ent and now - ent["ts"] < 2 * cache_secs:
            return ent["games"]      # briefly stale is fine
        return []                    # too stale: quote nothing for sport

    games = []
    for g in data:
        home, away = g.get("home_team"), g.get("away_team")
        commence = g.get("commence_time")
        ml, spreads, totals = [], [], []
        for bk in g.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                key = mkt.get("key")
                ocs = mkt.get("outcomes", [])
                if key == "h2h":
                    ph = pa = None
                    for oc in ocs:
                        if oc.get("name") == home:
                            ph = american_to_prob(oc.get("price", 0))
                        elif oc.get("name") == away:
                            pa = american_to_prob(oc.get("price", 0))
                    if ph and pa:
                        dh, da = devig(ph, pa)
                        if dh:
                            ml.append((dh, da))
                elif key == "spreads":
                    ph = pa = pt = None
                    for oc in ocs:
                        if oc.get("name") == home:
                            ph, pt = american_to_prob(oc.get("price", 0)), oc.get("point")
                        elif oc.get("name") == away:
                            pa = american_to_prob(oc.get("price", 0))
                    if ph and pa and pt is not None:
                        dh, _ = devig(ph, pa)
                        if dh:
                            spreads.append((float(pt), dh))
                elif key == "totals":
                    po = pu = pt = None
                    for oc in ocs:
                        nm = (oc.get("name") or "").lower()
                        if nm == "over":
                            po, pt = american_to_prob(oc.get("price", 0)), oc.get("point")
                        elif nm == "under":
                            pu = american_to_prob(oc.get("price", 0))
                    if po and pu and pt is not None:
                        do, _ = devig(po, pu)
                        if do:
                            totals.append((float(pt), do))
        if len(ml) < MIN_BOOKS:
            continue
        if CONSENSUS == "median":
            hp = statistics.median(p[0] for p in ml)
            ap = 1.0 - hp
        else:
            hp = sum(p[0] for p in ml) / len(ml)
            ap = sum(p[1] for p in ml) / len(ml)
        unc = statistics.pstdev([p[0] for p in ml]) if len(ml) >= 2 else 0.0
        games.append({"home": home, "away": away, "commence": commence,
                      "home_prob": hp, "away_prob": ap, "n_books": len(ml),
                      "sharp": True, "uncertainty": unc,
                      "spread_lines": spreads, "total_lines": totals})
    _sport_cache[sport] = {"ts": now, "games": games}
    log.info(f"[{sport}] {len(games)} games priced "
             f"({sum(len(g['spread_lines']) for g in games)} spread lines, "
             f"{sum(len(g['total_lines']) for g in games)} total lines)")
    return games
