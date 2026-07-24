"""Inventory skew in LOGIT space (Avellaneda-Stoikov for prediction markets).

Why: our original skew shaded quotes a flat N cents regardless of price.
But a cent means very different things at different prices --
  50c -> 51c is a ~2% change in odds
   8c ->  9c is a ~12% change in odds
so a flat cent-skew over-shades cheap rungs and under-shades mid ones.

The prediction-market adaptation of Avellaneda-Stoikov (arXiv 2510.15205)
works in log-odds, where the geometry is uniform:

    x        = logit(p)                       (price -> log-odds)
    r_x      = x - q * gamma * sigma^2 * tau  (reservation, inventory-shifted)
    p_r      = sigmoid(r_x)                   (back to price)

  q      = signed inventory in "risk units" (position / cap), + = long YES
  gamma  = risk aversion (higher = shade harder)
  sigma  = belief volatility per sqrt(time) in logit units
  tau    = time to settlement, normalized

Two properties the old cent-skew lacked:
  1. Scale invariance -- equal *relative* shading at any price.
  2. Time decay -- skew shrinks as settlement approaches, because there
     is less time left for the price to move against the inventory.

Everything is opt-in via config.LOGIT_SKEW so the proven cent-based path
stays the default until CLV data says this is better.
"""
import math

# Clamp probabilities away from 0/1 so logit stays finite.
_EPS = 1e-6


def logit(p: float) -> float:
    p = min(max(p, _EPS), 1 - _EPS)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def reservation_price_cents(fair_cents: float, q_units: float,
                            gamma: float, sigma: float,
                            tau: float) -> float:
    """Inventory-adjusted fair value, in cents.

    fair_cents : unskewed fair for THIS side (0-100)
    q_units    : signed inventory / per-market cap. +1.0 means "fully long
                 this side"; negative means long the opposite side.
    gamma      : risk aversion (0 = no skew)
    sigma      : belief vol in logit units per sqrt(tau)
    tau        : normalized time to settlement (0 at settle, 1 far out)

    Long inventory pushes the reservation price DOWN (we want to buy less
    / sell more of what we already hold), and vice versa.
    """
    if gamma <= 0 or q_units == 0 or tau <= 0:
        return fair_cents
    x = logit(fair_cents / 100.0)
    r = x - q_units * gamma * (sigma ** 2) * tau
    return sigmoid(r) * 100.0


def skew_cents(fair_cents: float, q_units: float, gamma: float,
               sigma: float, tau: float, max_cents: float) -> float:
    """Convenience: how many cents to shade, derived from the logit
    reservation price and clamped to a hard cent ceiling so a wild
    parameter can never produce an absurd quote."""
    r = reservation_price_cents(fair_cents, q_units, gamma, sigma, tau)
    delta = fair_cents - r          # positive => shade down (we're long)
    if delta > max_cents:
        delta = max_cents
    elif delta < -max_cents:
        delta = -max_cents
    return delta


def tau_from_minutes(mins_to_start: float, horizon_min: float = 2880.0) -> float:
    """Normalize time-to-event into (0, 1]. Default horizon 48h matches
    our quoting window. Never returns 0 (a settled market isn't quoted)."""
    if mins_to_start <= 0:
        return 0.01
    return min(1.0, max(0.01, mins_to_start / horizon_min))
