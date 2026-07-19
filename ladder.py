"""Strike-ladder pricing (item: WNBA spreads/totals, extensible).

A ladder market like "Washington wins by over 6.5" is a point on the
margin distribution's survival curve. We fit a Normal distribution to
the sharp consensus — every devigged (line, probability) pair from every
book is one constraint — then read fair value for ANY strike off the
fitted curve. The modeling gap (retail trades these, few price them) is
the edge.

Math: P(X > x) = p  <=>  Phi^-1(p) * sigma = mu - x, which is linear in
(mu, sigma) — closed-form least squares, no scipy needed.
"""
import logging
import math

log = logging.getLogger("ladder")

# Reasonable priors when only one constraint exists (moneyline only)
SIGMA_PRIOR = {"wnba_margin": 11.5, "wnba_total": 13.5,
               "nba_margin": 12.0, "nba_total": 18.0}


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF via bisection on erf (|z| <= 8)."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def fit_normal(points, sigma_prior: float):
    """points: [(x, p)] meaning P(X > x) = p. Returns (mu, sigma).

    Linearized: x_i = mu - sigma * z_i where z_i = Phi^-1(p_i).
    With <2 distinct x we pin sigma to the prior. Sigma is clamped to a
    sane band around the prior so a bad quote can't produce an insane
    distribution.
    """
    pts = [(x, p) for x, p in points if 0.001 < p < 0.999]
    if not pts:
        return None, None
    zs = [norm_ppf(p) for _, p in pts]
    xs = [x for x, _ in pts]
    if len(set(xs)) < 2:
        sigma = sigma_prior
        mu = sum(x + sigma * z for x, z in zip(xs, zs)) / len(xs)
        return mu, sigma
    n = len(xs)
    zbar = sum(zs) / n
    xbar = sum(xs) / n
    denom = sum((z - zbar) ** 2 for z in zs)
    if denom < 1e-9:
        sigma = sigma_prior
    else:
        sigma = -sum((z - zbar) * (x - xbar) for z, x in zip(zs, xs)) / denom
    sigma = min(max(sigma, sigma_prior * 0.5), sigma_prior * 2.0)
    mu = xbar + sigma * zbar
    return mu, sigma


def prob_greater(mu: float, sigma: float, x: float) -> float:
    """P(X > x) under the fitted Normal."""
    return 1.0 - norm_cdf((x - mu) / sigma)


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
        return norm_cdf((-threshold - self.mu_m) / self.sigma_m)

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
