"""Deterministic summaries and comparisons of benchmark trials.

Bootstrap resampling always uses a fixed seed, so the same trials give the same
intervals on every run. Intervals are percentile bootstraps at 95%.
"""
import numpy as np

SEED = 12345  # fixed so every summary is reproducible
N_BOOT = 10000
LEVEL = 0.95


def _array(values):
    x = np.asarray(values, dtype=float).ravel()
    if x.size == 0:
        raise ValueError("no values")
    if not np.all(np.isfinite(x)):
        raise ValueError("non-finite value")
    return x


def _interval(boots, level):
    lo, hi = np.quantile(boots, [(1 - level) / 2, (1 + level) / 2])
    return [float(lo), float(hi)]


def bootstrap_ci(values, stat=np.median, n_boot=N_BOOT, level=LEVEL, seed=SEED):
    """Percentile bootstrap interval of `stat` (a numpy reducer taking axis=)."""
    x = _array(values)
    if x.size == 1:
        return [float(x[0]), float(x[0])]
    idx = np.random.default_rng(seed).integers(0, x.size, size=(n_boot, x.size))
    return _interval(stat(x[idx], axis=1), level)


def summarize(values, level=LEVEL):
    """n, median, mean, sd, cv, min, max and a bootstrap CI of the median."""
    x = _array(values)
    sd = float(np.std(x, ddof=1)) if x.size > 1 else 0.0
    mean = float(np.mean(x))
    return {
        "n": int(x.size),
        "median": float(np.median(x)),
        "mean": mean,
        "sd": sd,
        "cv": sd / mean if mean else None,
        "min": float(np.min(x)),
        "max": float(np.max(x)),
        "ci95": bootstrap_ci(x, level=level),
    }


def compare(base, other, level=LEVEL, n_boot=N_BOOT, seed=SEED):
    """Independent samples: median(other) / median(base) with a bootstrap CI and a
    two-sided Mann-Whitney U test."""
    a, b = _array(base), _array(other)
    ratio = float(np.median(b) / np.median(a))
    rng = np.random.default_rng(seed)
    ra = np.median(a[rng.integers(0, a.size, size=(n_boot, a.size))], axis=1)
    rb = np.median(b[rng.integers(0, b.size, size=(n_boot, b.size))], axis=1)
    out = {"ratio": ratio, "ci95": _interval(rb / ra, level), "n_base": int(a.size), "n_other": int(b.size)}
    if a.size > 1 and b.size > 1:
        from scipy import stats as sps  # deferred: scipy.stats costs ~1 s to import

        u, p = sps.mannwhitneyu(b, a, alternative="two-sided")
        out.update(mann_whitney_u=float(u), p_value=float(p))
    else:
        out.update(mann_whitney_u=None, p_value=None)
    return out


def compare_paired(base, other, level=LEVEL, n_boot=N_BOOT, seed=SEED):
    """Paired samples (e.g. interleaved trials): median of other_i / base_i with a
    bootstrap CI over pairs and a two-sided Wilcoxon signed-rank test on log ratios."""
    a, b = _array(base), _array(other)
    if a.size != b.size:
        raise ValueError("paired samples need equal length")
    r = b / a
    out = {"ratio": float(np.median(r)), "ci95": bootstrap_ci(r, level=level, n_boot=n_boot, seed=seed),
           "n_pairs": int(r.size), "pairs_other_faster": int(np.sum(r > 1))}
    logs = np.log(r)
    if r.size > 1 and np.any(logs != 0):
        from scipy import stats as sps

        w, p = sps.wilcoxon(logs)
        out.update(wilcoxon_w=float(w), p_value=float(p))
    else:
        out.update(wilcoxon_w=None, p_value=None)
    return out


def window_mean(t, y, start, end):
    """Time-weighted mean of samples whose interval midpoint lies in [start, end)."""
    t, y = np.asarray(t, float), np.asarray(y, float)
    dt = np.diff(np.concatenate([[0.0], t]))
    mid = t - dt / 2
    m = (mid >= start) & (mid < end)
    if not np.any(m):
        raise ValueError("no samples in window")
    return float(np.sum(y[m] * dt[m]) / np.sum(dt[m]))
