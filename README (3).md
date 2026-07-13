# Kalshi Passive Maker — v3 (final)

Rests limit orders on Kalshi MLB game markets at devigged sharp consensus
(Pinnacle et al via The Odds API) minus an edge that must clear fees.
Skews size to the NO side (documented retail YES-bias), pauses a side after
every fill (adverse-selection guard), re-quotes on 2c fair moves, cancels
everything 30 min before first pitch, and self-heals order state from the
exchange every ~10 minutes. Serves its own live dashboard.

## Required env (Railway)
- KALSHI_KEY_ID / KALSHI_PRIVATE_KEY (full PEM contents)
- ODDS_API_KEY
- DISCORD_WEBHOOK
- MAKER_DB=/data/maker.db   (mount a volume at /data)
- START_BANKROLL=50000      (cents)

## Odds API budget (important)
Calls cost (markets x regions) credits. us,eu = 2 credits/call.
- ODDS_CACHE_SECS=90  (default) ~57K credits/mo -> needs 100K tier (~$59/mo). Recommended.
- ODDS_CACHE_SECS=300 ~17K/mo -> fits 20K tier (~$30/mo). Slower reactions = more pick-off risk.
STALE_FAIR_SECS (default 240) cancels all quotes if the feed goes quiet.

## Risk defaults (override via env)
PER_MARKET_CAP=2000 ($20) | TOTAL_CAP=40000 ($400) | DAILY_LOSS_LIMIT=6000 ($60)
DRAWDOWN_LIMIT=20000 ($200) | MIN_EDGE_CENTS=3 | MARGIN_CENTS=4 | REQUOTE_MOVE=2
NO_SIZE_PCT=60 | FILL_COOLDOWN_SECS=600 | CUTOFF_MIN=30 | MAX_HOURS_OUT=48
KILL=1 -> halt + cancel everything (Railway env change auto-restarts)

## Dashboard
Railway -> Settings -> Networking -> Generate Domain. That URL is the live
dashboard (balance, working capital, today/total P&L, quotes, fills, settles).

## Scale-up plan (after 1-2 profitable weeks)
1. Raise TOTAL_CAP / PER_MARKET_CAP proportionally with new deposits.
2. Add NBA/NFL when in season (KALSHI_SERIES is env-driven).
3. Phase 2: Kalshi RFQ combo making — where the top solo operators
   (Risk Takers podcast guests) report the largest edges.
