# Kalshi Passive Maker — v5.4 (WNBA ladders + MLB toggle)

Rests limit orders on Kalshi MLB game markets at devigged sharp consensus
(Pinnacle/Circa/BetOnline via The Odds API) minus an edge that must clear
fees. Records every odds snapshot, fair value, quote event, fill, and
settlement to SQLite — the dataset is the product.

## Modes
- **LIVE** (`KILL` unset/0): real orders.
- **PAPER** (`KILL=1`): full pipeline, simulated quotes filled against real
  market prints, everything recorded with `is_sim=1`. Cancels any real
  resting orders at startup. Dry-run days generate real strategy data.

## Files
- `config.py` — every tunable, env-overridable, commented
- `kalshi_client.py` — signed client; paginated; rejected-vs-unavailable
  error taxonomy; rate pacing (8 read/s, 4 write/s vs basic-tier 10/5)
- `fair_value.py` — devigged consensus + book-disagreement uncertainty +
  Odds API quota tracking
- `matcher.py` — Kalshi ticker ↔ Odds API game matching
- `quoter.py` — pricing pipeline (fees → time widening → uncertainty →
  inventory skew → orderbook clamp) + risk gates
- `paper.py` — paper-trading engine (KILL=1)
- `store.py` — SQLite layer (snapshots, fairs, quote events, fills,
  settlements, maker CLV, daily P&L real+sim)
- `notify.py` — Discord alerts; secrets scrubbed from all logs/messages
- `main.py` — loop, degraded-state handling, watchdog, daily summary
- `dashboard.py` — live dashboard (mode badge, P&L history, inventory,
  fill history with edge + CLV columns)

## Required env (Railway)
- `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY` (PEM; base64 single-line ok)
- `ODDS_API_KEY`, `DISCORD_WEBHOOK`
- `MAKER_DB=/data/maker.db` (volume at /data), `START_BANKROLL` (cents)
- `KILL=1` for paper mode

## Odds API budget
Calls cost 2 credits (us,eu). ODDS_CACHE_SECS=60 (default) ≈ 86.4K/mo —
fits the 100K tier with room for the bet tracker. 30s would be ~172K: does
NOT fit. Watchdog alerts when remaining credits drop below
QUOTA_ALERT_REMAINING (8000).

## Safety model
- API outage ≠ flat: if orders/positions can't be verified, the bot goes
  DEGRADED — no quoting, orders untouched, Discord alert, auto-recover.
- Settlement polling is gated until priming succeeds (restarts can never
  pollute daily P&L).
- Unconfirmed cancels keep the order tracked; unconfirmed placements pause
  posting until reconciled. Watchdog alerts on loop stalls >5 min.
- Risk: $20/market, $400 total, $60 daily stop, $200 drawdown kill.

## Tests
`python3 -m pytest tests/ -v` (25 tests: devig, fees, quoting pipeline,
book clamp, restart safety, sim fills, CLV math, secret scrubbing).

## v4.1 edge add-ons (research-backed)
- **Longshot-bias filter**: Kalshi longshot prices are systematically rich
  (documented across 300K+ contracts). Bids landing below LONGSHOT_CENTS
  (25c) require LONGSHOT_EXTRA_EDGE (+2c) on top of the normal floor.
- **Steam guard**: if our fair moves ≥3c within 3 min (lineup posts,
  pitcher scratches), the market pauses for 4 min — resting quotes during
  repricing are pick-off bait.

## v4.2 fair-value quality
- **Power devig** (default): corrects multiplicative devig's overstatement
  of longshot probabilities on lopsided lines. `DEVIG_METHOD=multiplicative`
  to revert.
- **Median consensus** (default): cross-book fair uses the median devigged
  prob — one stale/outlier book can no longer drag the fair. `CONSENSUS=mean`
  to revert.

## v4.3 practitioner fixes (final)
- **Exact maker fee math**: fee = ceil(0.0175 x C x P x (1-P)) per Kalshi's
  published schedule — the ceil applies to the maker product, not 0.25x the
  ceil'd taker fee. Small fills were previously under-costed.
- **Adaptive loop cadence**: when any matched game is inside FAST_WINDOW_MIN
  (120) of first pitch, the loop runs every FAST_LOOP_SECS (12s) instead of
  30s — REST-polling staleness near game time is the documented pick-off
  vector for Kalshi bots.

## v4.4 dashboard upgrade
Edge-health scorecards (fills today, avg edge @ fill 7d, avg CLV vs close
7d), risk-limit meters (daily loss vs stop, drawdown vs kill), Odds API
quota + loop cadence in the header, priced-markets table (fair,
uncertainty, books, time to start, quoting/steam/pulled/unc-skip state),
cumulative P&L line over the daily bars, edge column on resting quotes,
% of cap on inventory. Auto-refresh 10s.

## v4.5–v4.7
- Queue-position step-up at post time (JOIN_BEST/JOIN_MIN_EDGE)
- Hold-top-of-queue: resting orders requote above rivals while edge holds
- Dashboard Pause/Resume button (DASH_TOKEN-protected), pause flag in DB
- Matcher passes uncertainty/n_books through (uncertainty filter now live)
- Exact maker fee formula; adaptive 12s loop inside 2h of first pitch
