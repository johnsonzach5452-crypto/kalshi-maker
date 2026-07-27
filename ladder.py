"""Strike-ladder pricing (WNBA spreads/totals, extensible).

A ladder market like "Washington wins by over 6.5" is a point on the margin
distribution's survival curve. We fit a location-scale distribution to the
sharp consensus — every devigged (line, probability) pair from every book is
one constraint — then read fair value for ANY strike off the fitted curve.
The modeling gap (retail trades these, few price them) is the edge.

Math: P(X > x) = p  <=>  Q(p) * scale = mu - x, which is linear in
(mu, scale) — closed-form least squares, no scipy needed. Q is the quantile
function of the chosen tail (Normal or Student-t).

WHY A PLUGGABLE TAIL (v6.0)
The original pricer fit a Normal, whose tails decay as exp(-x^2/2). Basketball
margins and totals have HEAVIER tails than that, so the Normal systematically
underprices tail strikes — big overs and big covers. Measured live, the bot
sold those too cheap: 42% win rate vs a ~70% break-even, p~0.4% the fairs were
correctly priced. Switching the tail to Student-t (fat, df=LADDER_T_DF) fattens
exactly those tails while still matching the interior sharp lines. Normal is the
df->inf special case, so nothing about the fit structure changes.

Default is still "normal" so deploying this file does NOT silently change live
behavior. Set LADDER_DIST=t (and LADDER_T_DF, ~5-7) to A/B the fix in paper
mode — that is the intended validation path.
"""
import logging
import math
import os

import dist

log = logging.getLogger("ladder")

# ── distribution selection (env-overridable, matches config's _s/_f style) ─
LADDER_DIST = os.environ.get("LADDER_DIST", "normal")   # "normal" | "t"
LADDER_T_DF = float(os.environ.get("LADDER_T_DF", "6"))  # Student-t df (fat tails)

# Reasonable priors (as point STD-DEVs) when only one constraint exists.
# Fattened from the original thin-tail values per the tail-underpricing fix:
# wnba_total 13.5 -> 16.0, wnba_margin 11.5 -> 13.0. These only bind on
# moneyline-only fits; multi-line fits are data-driven.
SIGMA_PRIOR = {"wnba_margin": float(os.environ.get("SIGMA_WNBA_MARGIN", "13.0")),
               "wnba_total":  float(os.environ.get("SIGMA_WNBA_TOTAL", "16.0")),
               "nba_margin":  float(os.environ.get("SIGMA_NBA_MARGIN", "12.5")),
               "nba_total":   float(os.environ.get("SIGMA_NBA_TOTAL", "19.0"))}


def _q(p: float) -> float:
    """Quantile of the chosen standard tail."""
    return dist.ppf(p, LADDER_DIST, LADDER_T_DF)


def _sf(z: float) -> float:
    """Survival P(Z > z) of the chosen standard tail."""
    return 1.0 - dist.cdf(z, LADDER_DIST, LADDER_T_DF)


def _cdf(z: float) -> float:
    return dist.cdf(z, LADDER_DIST, LADDER_T_DF)


# Backward-compatible aliases (old code / tests may import these) ───────────
def norm_cdf(x: float) -> float:
    return dist.norm_cdf(x)


def norm_ppf(p: float) -> float:
    return dist.norm_ppf(p)


def fit_normal(points, sigma_prior: float):
    """points: [(x, p)] meaning P(X > x) = p. Returns (mu, scale).

    Linearized: x_i = mu - scale * q_i where q_i = Q(p_i) is the quantile
    of the chosen tail. With <2 distinct x we pin scale to the prior. Scale
    is clamped to a sane band around the prior so a bad quote can't produce
    an insane distribution.

    Name kept as fit_normal for drop-in compatibility; it now fits whichever
    tail LADDER_DIST selects. The prior is a point std-dev; for Student-t it
    is converted to the equivalent scale so central spread is preserved and
    only the tail shape changes.
    """
    # convert desired point-std prior into this tail's scale parameter
    scale_prior = sigma_prior / dist.tail_std(LADDER_DIST, LADDER_T_DF)

    pts = [(x, p) for x, p in points if 0.001 < p < 0.999]
    if not pts:
        return None, None
    zs = [_q(p) for _, p in pts]
    xs = [x for x, _ in pts]
    if len(set(xs)) < 2:
        scale = scale_prior
        mu = sum(x + scale * z for x, z in zip(xs, zs)) / len(xs)
        return mu, scale
    n = len(xs)
    zbar = sum(zs) / n
    xbar = sum(xs) / n
    denom = sum((z - zbar) ** 2 for z in zs)
    if denom < 1e-9:
        scale = scale_prior
    else:
        scale = -sum((z - zbar) * (x - xbar) for z, x in zip(zs, xs)) / denom
    scale = min(max(scale, scale_prior * 0.5), scale_prior * 2.0)
    mu = xbar + scale * zbar
    return mu, scale


def prob_greater(mu: float, scale: float, x: float) -> float:
    """P(X > x) under the fitted distribution."""
    return _sf((x - mu) / scale)


class GameDists:
    """Fitted margin (home - away) and total distributions for one game."""

    def __init__(self, margin_points, total_points, sport="wnba"):
        self.mu_m, self.sigma_m = fit_normal(
            margin_points, SIGMA_PRIOR[f"{sport}_margin"])
        self.mu_t, self.sigma_t = fit_normal(
            total_points, SIGMA_PRIOR[f"{sport}_total"])

    def spread_fair(self, team_is_home: bool, threshold: float):
        """Fair P(team wins by more than threshold)."""
        if self.mu_m is None:
            return None
        if team_is_home:
            return prob_greater(self.mu_m, self.sigma_m, threshold)
        # away margin = -(home margin): P(-M > t) = P(M < -t)
        return _cdf((-threshold - self.mu_m) / self.sigma_m)

    def total_fair(self, threshold: float):
        """Fair P(total points > threshold)."""
        if self.mu_t is None:
            return None
        return prob_greater(self.mu_t, self.sigma_t, threshold)


def build_margin_points(game: dict) -> list:
    """From a fair_value game dict -> [(x, P(home margin > x))].
    Moneyline contributes (0, p_home); each book's home spread line s
    contributes (-s, p_home_cover) (s is negative when home favored)."""
    pts = []
    hp = game.get("home_prob")
    if hp:
        pts.append((0.0, hp))
    for s, p in game.get("spread_lines", []):
        pts.append((-s, p))
    return pts


def build_total_points(game: dict) -> list:
    return [(line, p) for line, p in game.get("total_lines", [])]
