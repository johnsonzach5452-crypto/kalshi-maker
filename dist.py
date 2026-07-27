"""Location-scale distribution primitives for the ladder pricer.

The ladder pricer fits a distribution to sharp consensus lines, then reads
fair value off the survival curve. The original pricer used a Normal, whose
tails decay as exp(-x^2/2) — too thin for basketball margins and totals.
Thin tails systematically UNDERprice tail outcomes (big overs, big covers),
which is exactly the documented bias (bot sold overs/covers too cheap).

This module provides a pluggable tail: Normal (thin) and Student-t (fat,
controlled by degrees of freedom `nu`). As nu -> inf, Student-t -> Normal,
so the Normal is the nu=inf special case and everything stays backward
compatible. No scipy: the incomplete-beta continued fraction and a
Lanczos log-gamma give an exact Student-t CDF in ~60 lines.

All functions are on the STANDARD distribution (location 0, scale 1). The
ladder code scales via z = (x - mu) / sigma exactly as before.
"""
import math

# ── Normal (unchanged behavior, kept here as the single source) ─────────

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


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


# ── Student-t (the fat-tailed upgrade) ──────────────────────────────────

def _log_gamma(x: float) -> float:
    """Lanczos approximation of ln(Gamma(x)), x > 0."""
    g = 7
    c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028,
         771.32342877765313, -176.61502916214059, 12.507343278686905,
         -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7]
    x -= 1
    a = c[0]
    t = x + g + 0.5
    for i in range(1, g + 2):
        a += c[i] / (x + i)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(_log_gamma(a + b) - _log_gamma(a) - _log_gamma(b)
                  + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(x: float, nu: float) -> float:
    """CDF of the standard Student-t with nu degrees of freedom."""
    if nu >= 200:          # numerically indistinguishable from Normal
        return norm_cdf(x)
    xt = nu / (nu + x * x)
    ib = _betai(nu / 2.0, 0.5, xt)
    return 1.0 - 0.5 * ib if x > 0 else 0.5 * ib


def t_ppf(p: float, nu: float) -> float:
    """Inverse Student-t CDF via bisection (|z| <= 60 covers fat tails)."""
    if nu >= 200:
        return norm_ppf(p)
    p = min(max(p, 1e-9), 1 - 1e-9)
    lo, hi = -60.0, 60.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if t_cdf(mid, nu) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ── unified interface: pick a tail by name ──────────────────────────────

def cdf(x: float, dist: str = "normal", nu: float = 7.0) -> float:
    return t_cdf(x, nu) if dist == "t" else norm_cdf(x)


def ppf(p: float, dist: str = "normal", nu: float = 7.0) -> float:
    return t_ppf(p, nu) if dist == "t" else norm_ppf(p)


def tail_std(dist: str = "normal", nu: float = 7.0) -> float:
    """Std-dev of the STANDARD distribution used, so callers can convert a
    fitted scale parameter into an actual point std-dev if needed.
    Student-t variance = nu/(nu-2) for nu>2 (else undefined/heavy)."""
    if dist == "t" and nu > 2:
        return math.sqrt(nu / (nu - 2.0))
    return 1.0
