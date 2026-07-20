"""kprop dashboard v3 — a pricing sheet, not a feed.

Layout, top to bottom:
  1. TOP EDGES — the biggest plays on the slate, front and center
  2. Game-by-game sections, sorted by each game's best edge. Every pitcher
     shows: what the model projects, the model's own line, and per market —
     the book's line/price next to the model's fair price for that line.

Start command:  sh -c "uvicorn kprop.dashboard:app --host 0.0.0.0 --port $PORT"
Env: DASH_PASSWORD, BETTABLE_BOOKS (?all=1 to show reference books)
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .db import get_conn
from .devig import american_to_prob, prob_to_american
from .tiers import EMOJI, blend_weight, calibrated_prob, classify

app = FastAPI(title="kprop")

TIER_ORDER = {"strong": 0, "solid": 1, "watch": 2}
MARKET_LABEL = {
    "pitcher_strikeouts": "K", "pitcher_strikeouts_alternate": "K·alt",
    "pitcher_outs": "Outs", "pitcher_hits_allowed": "Hits",
    "pitcher_hits_allowed_alternate": "Hits·alt", "pitcher_walks": "BB",
    "pitcher_walks_alternate": "BB·alt", "pitcher_earned_runs": "ER",
}
SIM_KEY = {"pitcher_strikeouts": "strikeouts",
           "pitcher_strikeouts_alternate": "strikeouts",
           "pitcher_outs": "outs", "pitcher_hits_allowed": "hits_allowed",
           "pitcher_hits_allowed_alternate": "hits_allowed",
           "pitcher_walks": "walks", "pitcher_walks_alternate": "walks",
           "pitcher_earned_runs": "earned_runs"}
MKT_RANK = ["pitcher_strikeouts", "pitcher_strikeouts_alternate",
            "pitcher_outs", "pitcher_hits_allowed",
            "pitcher_hits_allowed_alternate", "pitcher_walks",
            "pitcher_walks_alternate", "pitcher_earned_runs"]


def _gate(request: Request):
    pw = os.environ.get("DASH_PASSWORD")
    if pw and request.query_params.get("key") != pw:
        raise HTTPException(403, "missing/incorrect ?key=")


def _bettable():
    env = os.environ.get("BETTABLE_BOOKS", "").strip()
    return {b.strip() for b in env.split(",") if b.strip()} or None if env else None


# ------------------------------------------------------------ data

def fetch_all(d: date, min_ev: float, market: str):
    with get_conn() as conn, conn.cursor() as cur:
        q = """
        SELECT DISTINCT ON (pitcher_name, market, line, side)
               pitcher_name, market, side, line, price, bookmaker,
               model_prob, market_fair_prob, ev_pct, created_at, event_id
        FROM edges WHERE game_date = %s AND ev_pct >= %s
        """
        params: list = [d, min_ev]
        if market != "all":
            q += " AND market LIKE %s"
            params.append(market + "%")
        q += " ORDER BY pitcher_name, market, line, side, created_at DESC"
        cur.execute(q, params)
        cols = ["pitcher", "market", "side", "line", "price", "book",
                "model_prob", "market_fair_prob", "ev_pct", "as_of", "event_id"]
        edges = [dict(zip(cols, r)) for r in cur.fetchall()]

        ev_ids = list({e["event_id"] for e in edges if e["event_id"]})
        games = {}
        if ev_ids:
            cur.execute("""
                SELECT DISTINCT ON (event_id) event_id, home_team, away_team,
                       commence_time
                FROM odds_snapshots WHERE event_id = ANY(%s)
                ORDER BY event_id, fetched_at DESC
            """, (ev_ids,))
            for eid, home, away, ct in cur.fetchall():
                games[eid] = {"home": home, "away": away, "start": ct}

        openers = {}
        names = list({e["pitcher"] for e in edges})
        if names:
            cur.execute("""
                SELECT DISTINCT ON (player, market, line, bookmaker)
                       player, market, line, bookmaker, over_price, under_price
                FROM odds_snapshots
                WHERE player = ANY(%s) AND fetched_at::date = %s
                ORDER BY player, market, line, bookmaker, fetched_at ASC
            """, (names, d))
            for pl, mk, ln, bk, op, up in cur.fetchall():
                openers[(pl, mk, float(ln) if ln is not None else None, bk)] = (op, up)

        dists = {}
        if names:
            cur.execute("""
                SELECT DISTINCT ON (pitcher_name, market)
                       pitcher_name, market, distribution
                FROM projections
                WHERE pitcher_name = ANY(%s)
                ORDER BY pitcher_name, market, created_at DESC
            """, (names,))
            for name, mkt, dist in cur.fetchall():
                dd = dist if isinstance(dist, dict) else json.loads(dist)
                dists.setdefault(name, {})[mkt] = {int(k): float(v)
                                                   for k, v in dd.items()}

        cur.execute("""
            SELECT count(*),
                   avg(CASE WHEN closing_price IS NOT NULL THEN
                       (CASE WHEN closing_price > 0 THEN 100.0/(closing_price+100)
                             ELSE -closing_price::float/(-closing_price+100) END) -
                       (CASE WHEN price > 0 THEN 100.0/(price+100)
                             ELSE -price::float/(-price+100) END) END),
                   count(*) FILTER (WHERE result = 'win'),
                   count(*) FILTER (WHERE result = 'loss')
            FROM bets
        """)
        nbets, clv, wins, losses = cur.fetchone()
    return edges, games, openers, dists, (nbets or 0, clv, wins or 0, losses or 0)


BASE_MARKETS = ["pitcher_strikeouts", "pitcher_outs", "pitcher_hits_allowed",
                "pitcher_walks", "pitcher_earned_runs"]


def fetch_sheet(d: date):
    """Every pitcher with a projection + the main (non-alt) line per market.
    This is the reference sheet: no edge filter, no alt lines."""
    import statistics
    from .devig import fair_probs
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (pitcher_name, market)
                   pitcher_name, market, distribution
            FROM projections
            WHERE created_at > now() - interval '30 hours'
            ORDER BY pitcher_name, market, created_at DESC
        """)
        dists: dict = {}
        for name, mkt, dist in cur.fetchall():
            dd = dist if isinstance(dist, dict) else json.loads(dist)
            dists.setdefault(name, {})[mkt] = {int(k): float(v)
                                               for k, v in dd.items()}
        names = list(dists.keys())
        quotes: dict = {}
        games: dict = {}
        pit_game: dict = {}
        if names:
            cur.execute("""
                SELECT DISTINCT ON (player, market, bookmaker)
                       player, market, line, bookmaker, over_price,
                       under_price, event_id
                FROM odds_snapshots
                WHERE player = ANY(%s) AND market = ANY(%s)
                  AND commence_time > now()
                  AND commence_time < now() + interval '30 hours'
                  AND fetched_at > now() - interval '12 hours'
                ORDER BY player, market, bookmaker, fetched_at DESC
            """, (names, BASE_MARKETS))
            per: dict = {}
            for pl, mk, ln, bk, op, up, eid in cur.fetchall():
                if ln is None:
                    continue
                per.setdefault((pl, mk), []).append(
                    (float(ln), op, up, bk, eid))
                pit_game.setdefault(pl, eid)
            for (pl, mk), qs in per.items():
                lines = [q[0] for q in qs]
                main = statistics.mode(lines)
                at = [q for q in qs if q[0] == main]
                fos = [fair_probs(int(q[1]), int(q[2]))[0]
                       for q in at if q[1] is not None and q[2] is not None]
                cons = statistics.median(fos) if fos else None
                quotes[(pl, mk)] = (main, cons, len(at))
            ev_ids = list({e for e in pit_game.values() if e})
            if ev_ids:
                cur.execute("""
                    SELECT DISTINCT ON (event_id) event_id, home_team,
                           away_team, commence_time
                    FROM odds_snapshots WHERE event_id = ANY(%s)
                    ORDER BY event_id, fetched_at DESC
                """, (ev_ids,))
                for eid, home, away, ct in cur.fetchall():
                    games[eid] = {"home": home, "away": away, "start": ct}
    return dists, quotes, games, pit_game


# ------------------------------------------------------------ math helpers

def _mean(dist):
    return sum(k * v for k, v in dist.items()) if dist else None


def _p_over(dist, ln: float):
    p = sum(v for k, v in dist.items() if k > ln)
    if ln == int(ln):
        push = dist.get(int(ln), 0.0)
        if push < 1:
            p = p / (1 - push)
    return p


def _model_line(dist):
    """The half-integer line where the model is closest to a coin flip —
    i.e., what the model thinks the line SHOULD be."""
    if not dist:
        return None, None
    ks = sorted(dist)
    best = None
    for k in ks[:-1]:
        ln = k + 0.5
        p = _p_over(dist, ln)
        if best is None or abs(p - 0.5) < abs(best[1] - 0.5):
            best = (ln, p)
    return best if best else (None, None)


def _fair_and_ev(e):
    """Calibrated + blended fair price for the book's line."""
    p = float(e["model_prob"])
    f = float(e["market_fair_prob"]) if e["market_fair_prob"] is not None else None
    wm = blend_weight(e["market"])
    pc = calibrated_prob(p)
    p_bet = wm * pc + (1 - wm) * f if f is not None else pc
    fair = prob_to_american(min(max(p_bet, .01), .99))
    return fair, p_bet


def fmt_px(n) -> str:
    n = int(n)
    return f"+{n}" if n > 0 else str(n)


def countdown(start) -> str:
    if not start:
        return ""
    mins = int((start - datetime.now(timezone.utc)).total_seconds() // 60)
    if mins < -200:
        return "final?"
    if mins < 0:
        return "LIVE"
    if mins < 60:
        return f"in {mins}m"
    return f"in {mins // 60}h {mins % 60:02d}m"


def movement(e, openers) -> str:
    key = (e["pitcher"], e["market"],
           float(e["line"]) if e["line"] is not None else None, e["book"])
    op = openers.get(key)
    if not op:
        return ""
    open_px = op[0] if e["side"] == "over" else op[1]
    if open_px is None or int(open_px) == int(e["price"]):
        return ""
    now_p = american_to_prob(int(e["price"]))
    was_p = american_to_prob(int(open_px))
    if now_p < was_p - 0.002:
        return f'<span class="mv up" title="opened {fmt_px(open_px)}">▲</span>'
    if now_p > was_p + 0.002:
        return f'<span class="mv dn" title="opened {fmt_px(open_px)}">▼</span>'
    return ""


def dist_svg(dist, line):
    if not dist:
        return ""
    ks = sorted(dist)[:13]
    peak = max(dist[k] for k in ks) or 1
    W, H, bw = 150, 38, 11
    bars, cut = [], ""
    for i, k in enumerate(ks):
        h = max(2, dist[k] / peak * (H - 8))
        x = 3 + i * bw
        cls = "over" if (line is not None and k > line) else "under"
        bars.append(f'<rect class="{cls}" x="{x}" y="{H - 6 - h}" '
                    f'width="{bw - 2}" height="{h:.1f}" rx="1.5"/>')
    if line is not None and ks and ks[0] <= line <= ks[-1]:
        pos = 3 + (line - ks[0] + 0.5) * bw - 1
        cut = f'<line class="cut" x1="{pos:.1f}" y1="2" x2="{pos:.1f}" y2="{H - 5}"/>'
    return f'<svg class="dist" viewBox="0 0 {W} {H}">{"".join(bars)}{cut}</svg>'


# ------------------------------------------------------------ api

@app.get("/api/edges")
def api_edges(request: Request, day: str | None = None, min_ev: float = 0.0,
              market: str = "all"):
    _gate(request)
    d = date.fromisoformat(day) if day else date.today()
    edges, games, openers, dists, _ = fetch_all(d, min_ev, market)
    out = []
    for e in edges:
        fair, p_bet = _fair_and_ev(e)
        f = float(e["market_fair_prob"]) if e["market_fair_prob"] is not None else None
        out.append({**{k: e[k] for k in ("pitcher", "market", "side", "book")},
                    "line": float(e["line"]) if e["line"] is not None else None,
                    "book_price": e["price"], "fair_price": fair,
                    "model_prob": float(e["model_prob"]),
                    "consensus_prob": f, "ev_pct": float(e["ev_pct"]),
                    "tier": classify(float(e["ev_pct"]), float(e["model_prob"]),
                                     f, e["book"], e["market"]),
                    "as_of": e["as_of"].isoformat()})
    return JSONResponse(out)


# ------------------------------------------------------------ page

SHEET_LABEL = {"pitcher_strikeouts": "K", "pitcher_outs": "Outs",
               "pitcher_hits_allowed": "Hits allowed",
               "pitcher_walks": "Walks", "pitcher_earned_runs": "ER"}


def render_sheet(d: date, key: str) -> str:
    dists, quotes, games, pit_game = fetch_sheet(d)
    # group pitchers by game, ordered by start time
    by_game: dict = {}
    for name in dists:
        by_game.setdefault(pit_game.get(name), []).append(name)
    def gkey(eid):
        g = games.get(eid)
        return (g is None, g["start"] if g else None)
    blocks = []
    for eid in sorted(by_game, key=gkey):
        meta = games.get(eid, {})
        matchup = (f'{meta.get("away","")} @ {meta.get("home","")}'
                   if meta else "No lines posted yet")
        when = countdown(meta.get("start"))
        pcards = []
        for name in sorted(by_game[eid]):
            pd = dists[name]
            rows = []
            for mk in BASE_MARKETS:
                sim = SIM_KEY[mk]
                dist = pd.get(sim)
                if not dist:
                    continue
                proj = _mean(dist)
                q = quotes.get((name, mk))
                if q:
                    line, cons, nb = q
                    p_over = _p_over(dist, line)
                    wm = blend_weight(mk)
                    pc = calibrated_prob(p_over)
                    pb = wm * pc + (1 - wm) * cons if cons is not None else pc
                    pb = min(max(pb, .02), .98)
                    fo = prob_to_american(pb)
                    fu = prob_to_american(1 - pb)
                    lean = ("over" if proj > line + 0.15 else
                            "under" if proj < line - 0.15 else "")
                    rows.append(f"""<div class="srow">
<span class="smkt">{SHEET_LABEL[mk]}</span>
<span class="sline">{line}</span>
<span class="sproj{' lo' if lean=='under' else ' hi' if lean=='over' else ''}">{proj:.1f}</span>
<span class="spct">{pb:.0%} o</span>
<span class="sfair">o {fmt_px(fo)} · u {fmt_px(fu)}</span></div>""")
                else:
                    rows.append(f"""<div class="srow dim">
<span class="smkt">{SHEET_LABEL[mk]}</span>
<span class="sline">—</span>
<span class="sproj">{proj:.1f}</span>
<span class="spct"></span>
<span class="sfair">no line</span></div>""")
            if rows:
                pcards.append(f"""<div class="scard"><h3>{name}</h3>
<div class="shead"><span>Mkt</span><span>Line</span><span>Proj</span>
<span>Model</span><span>Fair o/u</span></div>{''.join(rows)}</div>""")
        if pcards:
            blocks.append(f"""<section class="sgame">
<div class="ghead"><h2>{matchup}</h2><p>{when}</p></div>
<div class="sgrid">{''.join(pcards)}</div></section>""")
    return "".join(blocks) or '<div class="none">No projections yet today — projector runs 12/14/20/22 UTC.</div>'


@app.get("/", response_class=HTMLResponse)
def board(request: Request, day: str | None = None, min_ev: float = 2.0,
          market: str = "all", tier: str = "all", view: str = "edges"):
    _gate(request)
    d = date.fromisoformat(day) if day else date.today()
    key = request.query_params.get("key", "")
    edges, games, openers, dists, bets = fetch_all(d, min_ev, market)

    bettable = _bettable()
    if bettable is not None and request.query_params.get("all") != "1":
        edges = [e for e in edges if e["book"] in bettable]
    for e in edges:
        f = float(e["market_fair_prob"]) if e["market_fair_prob"] is not None else None
        e["tier"] = classify(float(e["ev_pct"]), float(e["model_prob"]), f,
                             e["book"], e["market"])
        e["fair"], e["p_bet"] = _fair_and_ev(e)
    if tier != "all":
        edges = [e for e in edges if e["tier"] == tier]

    def href(**kw):
        p = {"key": key, "min_ev": min_ev, "market": market, "tier": tier,
             "day": d.isoformat(), "view": view, **kw}
        return "/?" + "&".join(f"{k}={v}" for k, v in p.items())

    view_pills = (
        f'<a class="chip{" on" if view == "edges" else ""}" '
        f'href="{href(view="edges")}">🔥 Edges</a>'
        f'<a class="chip{" on" if view == "sheet" else ""}" '
        f'href="{href(view="sheet")}">📋 Sheet</a>')

    ranked = sorted(edges, key=lambda e: (TIER_ORDER[e["tier"]],
                                          -float(e["ev_pct"])))
    # ---------- 1. TOP EDGES hero strip
    heroes = [e for e in ranked if e["tier"] != "watch"][:4] or ranked[:3]
    hero_html = "".join(f"""<a class="hero {e['tier']}" href="#g{e['event_id']}">
<span class="ht">{EMOJI[e['tier']]} {e['pitcher']}</span>
<span class="hb">{MARKET_LABEL.get(e['market'], e['market'])} {e['side']} {e['line']}</span>
<span class="hp">{fmt_px(e['price'])} <i>{e['book']}</i> · fair {fmt_px(e['fair'])}</span>
<span class="he">{float(e['ev_pct']):+.1f}%</span></a>""" for e in heroes)

    # ---------- 2. game-by-game, sorted by best edge in the game
    by_game: dict = {}
    for e in edges:
        by_game.setdefault(e["event_id"] or "?", []).append(e)

    def gsort(item):
        es = item[1]
        return (min(TIER_ORDER[e["tier"]] for e in es),
                -max(float(e["ev_pct"]) for e in es))

    sections = []
    for eid, ges in sorted(by_game.items(), key=gsort):
        meta = games.get(eid, {})
        matchup = (f'{meta.get("away", "")} @ {meta.get("home", "")}'
                   if meta else "Game")
        when = countdown(meta.get("start"))
        best_ev = max(float(e["ev_pct"]) for e in ges)

        by_pitcher: dict = {}
        for e in ges:
            by_pitcher.setdefault(e["pitcher"], []).append(e)
        pit_blocks = []
        for name, pes in sorted(by_pitcher.items(),
                                key=lambda kv: -max(float(x["ev_pct"])
                                                    for x in kv[1])):
            pd = dists.get(name, {})
            kmean = _mean(pd.get("strikeouts"))
            mline, _mp = _model_line(pd.get("strikeouts"))
            kline = next((float(e["line"]) for e in pes
                          if e["market"].startswith("pitcher_strikeouts")), mline)
            head_bits = []
            if kmean is not None:
                head_bits.append(f'projects <b>{kmean:.1f} K</b>')
            if mline is not None:
                head_bits.append(f'model line <b>{mline}</b>')
            # one row per market+side: keep the best-EV quote
            best_rows: dict = {}
            for e in pes:
                kk = (e["market"], e["side"])
                if (kk not in best_rows
                        or float(e["ev_pct"]) > float(best_rows[kk]["ev_pct"])):
                    best_rows[kk] = e
            rows = []
            for e in sorted(best_rows.values(),
                            key=lambda x: (MKT_RANK.index(x["market"])
                                           if x["market"] in MKT_RANK else 99,
                                           -float(x["ev_pct"]))):
                sim = SIM_KEY.get(e["market"])
                pm = _mean(pd.get(sim)) if sim else None
                proj = f"{pm:.1f}" if pm is not None else "—"
                ev = float(e["ev_pct"])
                meter = min(abs(ev), 12) / 12 * 100
                rows.append(f"""<div class="row {e['tier']}">
<span class="badge">{EMOJI[e['tier']]}</span>
<span class="mkt">{MARKET_LABEL.get(e['market'], e['market'])}</span>
<span class="bet">{e['side']} {e['line']}</span>
<span class="quote">{fmt_px(e['price'])} <i>{e['book']}</i>{movement(e, openers)}</span>
<span class="fair">{fmt_px(e['fair'])}</span>
<span class="proj">{proj}</span>
<span class="ev"><b class="{'pos' if ev >= 0 else 'neg'}">{ev:+.1f}%</b>
<em><i style="width:{meter:.0f}%"></i></em></span></div>""")
            pit_blocks.append(f"""<div class="pitcher">
<div class="phead"><h3>{name}</h3><p>{' · '.join(head_bits)}</p>
{dist_svg(pd.get('strikeouts', {}), kline)}</div>
<div class="cols"><span></span><span>Mkt</span><span>Bet</span>
<span>Book</span><span>Fair</span><span>Proj</span><span>EV</span></div>
{''.join(rows)}</div>""")
        sections.append(f"""<section class="game" id="g{eid}">
<div class="ghead"><h2>{matchup}</h2>
<p>{when}{' · best ' if when else 'best '}<b>{best_ev:+.1f}%</b></p></div>
{''.join(pit_blocks)}</section>""")

    n_strong = sum(1 for e in edges if e["tier"] == "strong")
    n_solid = sum(1 for e in edges if e["tier"] == "solid")
    tier_chips = "".join(
        f'<a class="chip{" on" if tier == t else ""}" href="{href(tier=t)}">{lbl}</a>'
        for t, lbl in [("all", "All"), ("strong", "🔥"), ("solid", "🎯"),
                       ("watch", "👀")])
    ev_chips = "".join(
        f'<a class="chip{" on" if min_ev == v else ""}" href="{href(min_ev=v)}">{v:g}%+</a>'
        for v in (2.0, 3.0, 5.0))
    nbets, clv, wins, losses = bets
    clv_s = f"{clv * 100:+.2f}%" if clv is not None else "—"
    empty = ("" if sections else '<div class="none">No edges above this filter '
             "yet — projector runs 12/14/20/22 UTC, watcher re-prices every "
             "cycle.</div>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300"><title>kprop · {d}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0A0D13;--card:#10151F;--edge:#1C2433;--ink:#EDEAE3;--dim:#7E8899;
--green:#3FD68C;--red:#E5484D;--amber:#F0B54A;
--mono:'JetBrains Mono',ui-monospace,Menlo,monospace;
--disp:'Space Grotesk',system-ui,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font:13.5px/1.45 var(--mono);-webkit-font-smoothing:antialiased}}
.wrap{{max-width:980px;margin:0 auto;padding:18px 14px 70px}}
.top{{display:flex;justify-content:space-between;align-items:flex-end;
flex-wrap:wrap;gap:10px}}
.brand{{font-family:var(--disp);font-weight:700;font-size:21px}}
.brand i{{color:var(--green);font-style:normal}}
.brand small{{display:block;color:var(--dim);font:11.5px var(--mono);margin-top:2px}}
.dnav{{color:var(--ink);text-decoration:none;border:1px solid var(--edge);
border-radius:5px;padding:0 7px;margin:0 3px}}
.filters{{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 4px}}
.chip{{border:1px solid var(--edge);border-radius:999px;color:var(--dim);
padding:3px 11px;font-size:12px;text-decoration:none}}
.chip.on{{color:#0A0D13;background:var(--ink);border-color:var(--ink);font-weight:600}}
h1.sec{{font:600 11px var(--mono);letter-spacing:.18em;color:var(--dim);
text-transform:uppercase;margin:18px 0 8px}}
.heroes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}}
.hero{{background:var(--card);border:1px solid var(--edge);border-radius:12px;
padding:12px 14px;text-decoration:none;color:var(--ink);display:flex;
flex-direction:column;gap:3px}}
.hero.strong{{border-color:var(--green);box-shadow:0 0 0 1px var(--green) inset}}
.hero.solid{{border-color:var(--amber)}}
.ht{{font:700 14px var(--disp)}}
.hb{{text-transform:capitalize}}
.hp{{color:var(--dim);font-size:12px}}
.hp i{{font-style:normal}}
.he{{font:700 20px var(--disp);color:var(--green);margin-top:2px}}
.game{{background:var(--card);border:1px solid var(--edge);border-radius:12px;
margin:12px 0;overflow:hidden}}
.ghead{{display:flex;justify-content:space-between;align-items:baseline;
padding:11px 15px;border-bottom:1px solid var(--edge)}}
.ghead h2{{margin:0;font:700 15px var(--disp)}}
.ghead p{{margin:0;color:var(--dim);font-size:12px}}
.ghead b{{color:var(--green)}}
.pitcher{{padding:4px 15px 10px;border-bottom:1px solid var(--edge)}}
.pitcher:last-child{{border-bottom:0}}
.phead{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:8px 0 4px}}
.phead h3{{margin:0;font:700 14.5px var(--disp)}}
.phead p{{margin:0;color:var(--dim);font-size:12px}}
.phead b{{color:var(--amber)}}
.dist{{width:150px;height:38px;margin-left:auto}}
.dist rect.under{{fill:#2A3550}}
.dist rect.over{{fill:var(--green);opacity:.9}}
.dist line.cut{{stroke:var(--amber);stroke-width:1.5;stroke-dasharray:3 2}}
.cols,.row{{display:grid;grid-template-columns:22px 52px 1fr 150px 62px 52px 116px;
gap:8px;align-items:center}}
.cols{{color:var(--dim);font-size:9.5px;letter-spacing:.1em;
text-transform:uppercase;padding:4px 0}}
.cols span:nth-child(n+4){{text-align:right}}
.row{{padding:6px 0;border-top:1px solid var(--edge);font-size:13px}}
.row.strong{{background:rgba(63,214,140,.05);box-shadow:inset 3px 0 0 var(--green)}}
.row.solid{{box-shadow:inset 3px 0 0 var(--amber)}}
.row.watch{{opacity:.55}}
.bet{{text-transform:capitalize;font-weight:600}}
.quote,.fair,.proj{{text-align:right;font-variant-numeric:tabular-nums}}
.quote i{{color:var(--dim);font-style:normal;font-size:11px}}
.fair{{color:var(--amber);font-weight:600}}
.mv{{margin-left:4px;font-size:10px}}.mv.up{{color:var(--green)}}.mv.dn{{color:var(--red)}}
.ev{{text-align:right}}
.ev b.pos{{color:var(--green)}}.ev b.neg{{color:var(--red)}}
.ev em{{display:block;height:3px;background:var(--edge);border-radius:2px;
margin-top:3px;overflow:hidden}}
.ev em i{{display:block;height:100%;background:var(--green)}}
.sgame{{background:var(--card);border:1px solid var(--edge);border-radius:12px;
margin:12px 0;overflow:hidden}}
.sgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
gap:0 18px;padding:4px 15px 12px}}
.scard h3{{margin:10px 0 4px;font:700 14px var(--disp)}}
.shead,.srow{{display:grid;grid-template-columns:78px 44px 46px 56px 1fr;
gap:6px;align-items:center;font-size:12.5px}}
.shead{{color:var(--dim);font-size:9.5px;letter-spacing:.1em;
text-transform:uppercase;padding:2px 0}}
.srow{{padding:4px 0;border-top:1px solid var(--edge)}}
.srow.dim{{opacity:.5}}
.smkt{{color:var(--dim)}}
.sline{{font-weight:600}}
.sproj{{color:var(--amber);font-weight:700}}
.sproj.hi{{color:var(--green)}}
.sproj.lo{{color:var(--red)}}
.spct{{color:var(--dim)}}
.sfair{{text-align:right;font-variant-numeric:tabular-nums}}
.none{{color:var(--dim);text-align:center;padding:50px 0;
border:1px dashed var(--edge);border-radius:12px}}
.paper{{position:fixed;left:0;right:0;bottom:0;background:#0D1119EE;
backdrop-filter:blur(6px);border-top:1px solid var(--edge);display:flex;
gap:22px;justify-content:center;padding:8px;font-size:12px;color:var(--dim)}}
.paper b{{color:var(--ink)}}
.legend{{color:var(--dim);font-size:11.5px;margin-top:16px;line-height:1.7}}
.legend b{{color:var(--amber)}}
@media(max-width:700px){{
.cols{{display:none}}
.row{{grid-template-columns:20px 44px 1fr 78px 76px;grid-auto-flow:row}}
.proj{{display:none}}
.dist{{display:none}}}}
</style></head><body><div class="wrap">
<div class="top">
  <div class="brand">k<i>prop</i><small>
    <a class="dnav" href="{href(day=(d - timedelta(days=1)).isoformat())}">‹</a>
    {d}{' · today' if d == date.today() else ''}
    <a class="dnav" href="{href(day=(d + timedelta(days=1)).isoformat())}">›</a>
    · 🔥{n_strong} 🎯{n_solid}</small></div>
  <div class="filters">{view_pills}<span style="width:12px"></span>{tier_chips if view == "edges" else ''}{('<span style="width:8px"></span>' + ev_chips) if view == "edges" else ''}</div>
</div>
{('<h1 class="sec">Top edges</h1>'
   f'<div class="heroes">{hero_html or chr(60)+"div class="+chr(34)+"none"+chr(34)+chr(62)+"quiet board</div>"}</div>'
   '<h1 class="sec">Slate · sorted by edge</h1>'
   + ''.join(sections) + empty) if view == "edges"
  else ('<h1 class="sec">Pitcher sheet · every base line · model priced</h1>'
        + render_sheet(d, key))}
<div class="legend"><b>Fair</b> = the price the model says each book line is
worth (calibrated, market-weighted) — value when the book pays more.
<b>Model line</b> = where the model thinks the line itself belongs.
<b>Proj</b> = projected stat total. ▲ improved since open · ▼ steamed.
Bettable books only (?all=1 for the rest) · refreshes every 5 min.</div>
</div>
<div class="paper">📒 paper: <b>{nbets}</b> bets · CLV <b>{clv_s}</b> ·
W-L <b>{wins}-{losses}</b></div>
</body></html>"""
