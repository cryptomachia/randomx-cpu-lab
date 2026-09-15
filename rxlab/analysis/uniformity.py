"""Statistical checks that hash outputs behave like independent uniform 256-bit values.

Hashes are (n, 32) uint8 arrays. Monero reads a PoW hash as a little-endian
256-bit integer and accepts it when hash_int * difficulty < 2**256, so the
difficulty-relevant leading bits are the high bits of byte 31, then byte 30,
and so on. A hash meets difficulty 2**k exactly when it has >= k leading
zero bits in that order.
"""
import numpy as np
from scipy import stats
from scipy.special import expit

POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.int64)
_CLZ8 = np.array([8 - i.bit_length() for i in range(256)], dtype=np.int64)


def as_hash_array(hashes):
    """Bytes objects or an array -> contiguous (n, 32) uint8 array."""
    if isinstance(hashes, np.ndarray):
        arr = np.ascontiguousarray(hashes, dtype=np.uint8)
    else:
        arr = np.frombuffer(b"".join(bytes(h) for h in hashes), dtype=np.uint8)
    return arr.reshape(-1, 32)


def hash_int(h):
    """One 32-byte hash as Monero's little-endian 256-bit integer."""
    return int.from_bytes(bytes(h), "little")


def meets_difficulty(h, difficulty):
    """Monero's check_hash: accept when hash_int * difficulty < 2**256."""
    return hash_int(h) * int(difficulty) < 1 << 256


def leading_zero_bits(hashes):
    """Leading zero bits of each hash read as a little-endian integer (256 for zero)."""
    rev = as_hash_array(hashes)[:, ::-1]
    nonzero = rev != 0
    first = nonzero.argmax(axis=1)
    lz = 8 * first + _CLZ8[rev[np.arange(len(rev)), first]]
    lz[~nonzero.any(axis=1)] = 256
    return lz


def unit_interval(hashes):
    """hash_int / 2**256 in [0, 1), to double precision (top 53 bits)."""
    top = np.ascontiguousarray(as_hash_array(hashes)[:, 31:23:-1])
    return (top.view(">u8").ravel() >> np.uint64(11)).astype(np.float64) * 2.0 ** -53


def hamming(a, b):
    """Bitwise Hamming distance between matching rows (any shape ending in 32 bytes)."""
    return POPCOUNT8[np.bitwise_xor(a, b)].sum(axis=-1)


def pooled_chisquare(observed, expected, min_expected=5.0):
    """Pearson chi-square after merging adjacent cells until each expects >= min_expected."""
    obs_bins, exp_bins = [], []
    o = e = 0.0
    for oi, ei in zip(observed, expected):
        o += oi
        e += ei
        if e >= min_expected:
            obs_bins.append(o)
            exp_bins.append(e)
            o = e = 0.0
    if o or e:
        if exp_bins:
            obs_bins[-1] += o
            exp_bins[-1] += e
        else:
            obs_bins.append(o)
            exp_bins.append(e)
    obs, exp = np.array(obs_bins), np.array(exp_bins)
    stat = float(((obs - exp) ** 2 / exp).sum())
    df = len(obs) - 1
    return {"statistic": stat, "df": df, "p_value": float(stats.chi2.sf(stat, df)) if df else 1.0}


def binomial_check(hits, trials, p):
    """Exact two-sided binomial test plus a Clopper-Pearson 95% interval."""
    res = stats.binomtest(int(hits), int(trials), p)
    ci = res.proportion_ci(confidence_level=0.95, method="exact")
    expected = trials * p
    return {
        "hits": int(hits),
        "trials": int(trials),
        "expected": float(expected),
        "ratio": float(hits / expected),
        "ratio_ci95": [float(ci.low / p), float(ci.high / p)],
        "p_value": float(res.pvalue),
    }


def homogeneity(hits, trials, rng, draws=100_000):
    """Do the groups share one hit rate?

    Pearson chi-square on the hits/misses table. When any expected hit count is
    below 5 the p-value comes from the exact conditional (hypergeometric) null,
    sampled `draws` times, instead of the chi-square approximation.
    """
    hits = np.asarray(hits, dtype=np.int64)
    trials = np.asarray(trials, dtype=np.int64)
    total = int(hits.sum())
    if total == 0 or total == trials.sum():
        return {"statistic": 0.0, "df": len(hits) - 1, "p_value": 1.0, "method": "degenerate"}
    share = trials / trials.sum()

    def stat(h):
        e_hit = total * share
        e_miss = (trials.sum() - total) * share
        return (((h - e_hit) ** 2 / e_hit) + (((trials - h) - e_miss) ** 2 / e_miss)).sum(axis=-1)

    observed = float(stat(hits))
    df = len(hits) - 1
    if (total * share).min() >= 5:
        return {"statistic": observed, "df": df, "p_value": float(stats.chi2.sf(observed, df)),
                "method": "chi2"}
    sims = rng.multivariate_hypergeometric(trials, total, size=draws)
    exceed = int((stat(sims) >= observed - 1e-9).sum())
    return {"statistic": observed, "df": df, "p_value": (exceed + 1) / (draws + 1),
            "method": f"exact-conditional-montecarlo-{draws}"}


def geometric_lz_test(lz):
    """Leading-zero counts against P(lz = j) = 2**-(j+1) (fair independent bits)."""
    lz = np.asarray(lz)
    n = len(lz)
    observed = np.bincount(lz, minlength=257)
    pmf = 2.0 ** -(np.arange(257) + 1.0)
    pmf[256] = 2.0 ** -256
    res = pooled_chisquare(observed, n * pmf)
    shown = int(lz.max()) + 1
    res.update({"n": n, "mean": float(lz.mean()), "observed": observed[:shown].tolist(),
                "expected": (n * pmf[:shown]).tolist()})
    return res


def byte_counts(hashes):
    """(32, 256) table: how often each byte value appears at each position."""
    arr = as_hash_array(hashes)
    return np.stack([np.bincount(arr[:, j], minlength=256) for j in range(32)])


def byte_uniformity(counts):
    """Per byte position chi-square against uniform bytes (df 255), and all positions jointly."""
    n = int(counts[0].sum())
    chi = ((counts - n / 256) ** 2 / (n / 256)).sum(axis=1)
    joint = float(chi.sum())
    return {
        "n": n,
        "statistic": chi.tolist(),
        "p_value": stats.chi2.sf(chi, 255).tolist(),
        "joint_statistic": joint,
        "joint_df": 32 * 255,
        "joint_p_value": float(stats.chi2.sf(joint, 32 * 255)),
    }


def bit_balance(counts):
    """Ones per output bit (little-endian bit order) against n/2, from byte_counts()."""
    n = int(counts[0].sum())
    bit_of = (np.arange(256)[None, :] >> np.arange(8)[:, None]) & 1
    ones = (counts[:, None, :] * bit_of[None, :, :]).sum(axis=2).reshape(256)
    z = (ones - n / 2) / np.sqrt(n / 4)
    stat = float((z ** 2).sum())
    return {"n": n, "max_abs_z": float(np.abs(z).max()), "statistic": stat, "df": 256,
            "p_value": float(stats.chi2.sf(stat, 256))}


def serial_correlation(u, lags):
    """Pearson correlation between u[t] and u[t + lag] for each lag."""
    out = []
    for lag in lags:
        r, p = stats.pearsonr(u[:-lag], u[lag:])
        out.append({"lag": int(lag), "r": float(r), "n": int(len(u) - lag), "p_value": float(p)})
    return out


def binomial_gof(values, n_trials=256, p=0.5):
    """Integer values against Binomial(n_trials, p): pooled chi-square and a z-test of the mean."""
    values = np.asarray(values)
    m = len(values)
    observed = np.bincount(values, minlength=n_trials + 1)
    res = pooled_chisquare(observed, m * stats.binom.pmf(np.arange(n_trials + 1), n_trials, p))
    sd = np.sqrt(n_trials * p * (1 - p))
    z = (values.mean() - n_trials * p) / (sd / np.sqrt(m))
    res.update({"n": int(m), "mean": float(values.mean()), "sd": float(values.std(ddof=1)),
                "mean_z": float(z), "mean_p_value": float(2 * stats.norm.sf(abs(z)))})
    return res


def avalanche(base, flipped):
    """Hashes of base inputs (m, 32) and of one-bit variants (m, k, 32).

    Returns Hamming distances (m, k) and the strict-avalanche flip counts (k, 256):
    how often each output bit changed when input bit j was flipped.
    """
    diff = np.bitwise_xor(base[:, None, :], flipped)
    distances = POPCOUNT8[diff].sum(axis=-1)
    flips = np.unpackbits(diff, axis=-1, bitorder="little").sum(axis=0, dtype=np.int64)
    return distances, flips


def sac_z(flips, m):
    """z-score of each (input bit, output bit) flip count against Binomial(m, 1/2)."""
    return (flips - m / 2) / np.sqrt(m / 4)


def sac_test(flips, m):
    """All strict-avalanche cells jointly: sum of z**2 ~ chi2(cells)."""
    z = sac_z(flips, m)
    stat = float((z ** 2).sum())
    return {"m": int(m), "cells": int(z.size), "max_abs_z": float(np.abs(z).max()),
            "statistic": stat, "df": int(z.size), "p_value": float(stats.chi2.sf(stat, z.size))}


def hit_gaps(hit_mask):
    """Hashes from the start (or previous hit) to each hit, in search order."""
    pos = np.flatnonzero(hit_mask) + 1
    return np.diff(np.concatenate([[0], pos]))


def geometric_gap_test(gaps, p, bins=20):
    """Waiting times against Geometric(p) on {1, 2, ...}, near-equiprobable bins."""
    gaps = np.asarray(gaps)
    bins = min(bins, len(gaps) // 5)
    if bins < 2:
        return {"n": int(len(gaps)), "p_value": None}
    edges = np.unique(stats.geom.ppf(np.linspace(0, 1, bins + 1)[1:-1], p))
    probs = np.diff(np.concatenate([[0.0], stats.geom.cdf(edges, p), [1.0]]))
    observed = np.bincount(np.searchsorted(edges, gaps, side="left"), minlength=len(probs))
    expected = len(gaps) * probs
    stat = float(((observed - expected) ** 2 / expected).sum())
    df = len(probs) - 1
    return {"n": int(len(gaps)), "mean": float(gaps.mean()), "expected_mean": 1 / p,
            "statistic": stat, "df": df, "p_value": float(stats.chi2.sf(stat, df))}


def residual_waits(gaps, elapsed):
    """Mean remaining wait E[G - m | G > m]: constant in m for a memoryless search."""
    gaps = np.asarray(gaps, dtype=np.float64)
    out = []
    for m in elapsed:
        rest = gaps[gaps > m] - m
        se = float(rest.std(ddof=1) / np.sqrt(len(rest))) if len(rest) > 1 else None
        out.append({"elapsed": float(m), "n": int(len(rest)),
                    "mean_remaining": float(rest.mean()) if len(rest) else None, "se": se})
    return out


def nonce_bits(nonces, width=32):
    """(n, width) 0/1 feature matrix of the nonce bits."""
    nonces = np.asarray(nonces, dtype=np.uint64)
    return ((nonces[:, None] >> np.arange(width, dtype=np.uint64)) & np.uint64(1)).astype(np.float64)


def logistic_fit(X, y, l2=1.0, iters=50):
    """Ridge-penalised logistic regression by Newton-Raphson; weights with the intercept first."""
    X1 = np.column_stack([np.ones(len(X)), X])
    w = np.zeros(X1.shape[1])
    rate = min(max(y.mean(), 1e-9), 1 - 1e-9)
    w[0] = np.log(rate / (1 - rate))
    ridge = l2 * np.eye(len(w))
    ridge[0, 0] = 0.0
    # numpy 2.0 on macOS Accelerate raises spurious FP flags in matmul; results are checked below.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        for _ in range(iters):
            mu = expit(X1 @ w)
            grad = X1.T @ (y - mu) - ridge @ w
            hess = (X1 * (mu * (1 - mu))[:, None]).T @ X1 + ridge
            step = np.linalg.solve(hess, grad)
            w += step
            if np.abs(step).max() < 1e-10:
                break
    if not np.isfinite(w).all():
        raise FloatingPointError("logistic fit diverged")
    return w


def logistic_score(w, X):
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return w[0] + X @ w[1:]


def auc(scores, y):
    """ROC AUC as the Mann-Whitney U statistic, with its two-sided p-value against 0.5."""
    y = np.asarray(y).astype(bool)
    u, p = stats.mannwhitneyu(scores[y], scores[~y], alternative="two-sided", method="asymptotic")
    return float(u / (y.sum() * (~y).sum())), float(p)


def holm(pvalues):
    """Holm step-down adjusted p-values (valid under any dependence between tests)."""
    p = np.asarray(pvalues, dtype=np.float64)
    m = len(p)
    order = np.argsort(p, kind="stable")
    adj = np.minimum(1.0, np.maximum.accumulate((m - np.arange(m)) * p[order]))
    out = np.empty(m)
    out[order] = adj
    return out
