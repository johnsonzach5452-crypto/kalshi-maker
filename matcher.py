"""Match Kalshi MLB game-winner markets to Odds API games by team + date."""
import datetime
import logging
import re

log = logging.getLogger("match")

# Full name pieces -> canonical key. Covers The Odds API full names and
# the team names Kalshi uses in market titles/subtitles.
TEAM_KEYS = {
    "diamondbacks": "ARI", "braves": "ATL", "orioles": "BAL", "red sox": "BOS",
    "cubs": "CHC", "white sox": "CWS", "reds": "CIN", "guardians": "CLE",
    "rockies": "COL", "tigers": "DET", "astros": "HOU", "royals": "KC",
    "angels": "LAA", "dodgers": "LAD", "marlins": "MIA", "brewers": "MIL",
    "twins": "MIN", "mets": "NYM", "yankees": "NYY", "athletics": "OAK",
    "phillies": "PHI", "pirates": "PIT", "padres": "SD", "mariners": "SEA",
    "giants": "SF", "cardinals": "STL", "rays": "TB", "rangers": "TEX",
    "blue jays": "TOR", "nationals": "WSH",
}


# Alternate codes seen in tickers -> our canonical keys
TICKER_ALIASES = {
    "CHW": "CWS", "WAS": "WSH", "SDP": "SD", "SFG": "SF", "TBR": "TB",
    "KCR": "KC", "ANA": "LAA", "ATH": "OAK", "AZ": "ARI",
}
ALL_CODES = set(TEAM_KEYS.values()) | set(TICKER_ALIASES.keys())


def norm_code(code: str):
    return TICKER_ALIASES.get(code, code)


def team_key(name: str):
    if not name:
        return None
    n = name.lower()
    for frag, key in TEAM_KEYS.items():
        if frag in n:
            return key
    return None


def game_date(iso_ts: str):
    """Return the US/Eastern-ish calendar date for a commence timestamp."""
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        # shift 5h back so late-night UTC still maps to the US game date
        return (dt - datetime.timedelta(hours=5)).date()
    except Exception:
        return None


def match_markets(kalshi_markets, fair_games):
    """Return list of quote targets:
    {ticker, side_team, fair_prob, commence, title}
    One Kalshi market = 'will TEAM win' -> YES prob = that team's fair prob.
    """
    # Index fair games by (date, frozenset of team keys)
    fair_idx = {}
    for g in fair_games:
        hk, ak = team_key(g["home"]), team_key(g["away"])
        d = game_date(g["commence"])
        if hk and ak and d:
            fair_idx[(d, frozenset((hk, ak)))] = g

    out = []
    for m in kalshi_markets:
        title = f"{m.get('title','')} {m.get('subtitle','')} {m.get('yes_sub_title','')}"
        ticker = m.get("ticker", "")
        close_iso = m.get("close_time") or m.get("expected_expiration_time") or ""
        d = game_date(close_iso)
        if not d:
            continue

        # Find the two teams referenced in the market
        keys_found = []
        tl = title.lower()
        for frag, key in TEAM_KEYS.items():
            if frag in tl and key not in keys_found:
                keys_found.append(key)
        if len(keys_found) < 2:
            # ticker often encodes teams e.g. KXMLBGAME-25JUL08NYYMIN-NYY
            tk = re.findall(r"[A-Z]{2,3}", ticker)
            keys_found = []
            for k in tk:
                if k in ALL_CODES:
                    nk = norm_code(k)
                    if nk not in keys_found:
                        keys_found.append(nk)
            keys_found = keys_found[:3]
        if len(keys_found) < 2:
            continue

        pair = frozenset(keys_found[:2])
        game = fair_idx.get((d, pair)) or fair_idx.get(
            (d + datetime.timedelta(days=1), pair)) or fair_idx.get(
            (d - datetime.timedelta(days=1), pair))
        if not game:
            continue

        # Which team does YES refer to? Prefer explicit yes_sub_title, else
        # the last team code in the ticker (Kalshi convention: -TEAM suffix)
        yes_team = None
        yst = (m.get("yes_sub_title") or "").lower()
        for frag, key in TEAM_KEYS.items():
            if frag in yst:
                yes_team = key
                break
        if not yes_team:
            mm = re.search(r"-([A-Z]{2,3})$", ticker)
            if mm and mm.group(1) in ALL_CODES:
                yes_team = norm_code(mm.group(1))
        if not yes_team:
            continue

        hk, ak = team_key(game["home"]), team_key(game["away"])
        if yes_team == hk:
            fair = game["home_prob"]
        elif yes_team == ak:
            fair = game["away_prob"]
        else:
            continue

        out.append({
            "ticker": ticker,
            "yes_team": yes_team,
            "fair_prob": fair,
            "commence": game["commence"],
            "title": m.get("title", ticker),
            "sharp": game.get("sharp", False),
            "uncertainty": game.get("uncertainty", 0.0),
            "n_books": game.get("n_books", 0),
        })
    return out
