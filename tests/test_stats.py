"""rxlab.stats: summaries, bootstrap intervals and comparisons."""
from math import comb

import numpy as np
import pytest

from rxlab import stats


def test_summarize_matches_numpy():
    x = [10.0, 12.0, 11.0, 13.0, 9.0, 11.5]
    s = stats.summarize(x)
    assert s["n"] == 6
    assert s["median"] == pytest.approx(np.median(x))
    assert s["mean"] == pytest.approx(np.mean(x))
    assert s["sd"] == pytest.approx(np.std(x, ddof=1))
    assert s["min"] == 9.0 and s["max"] == 13.0
    lo, hi = s["ci95"]
    assert 9.0 <= lo <= s["median"] <= hi <= 13.0


def test_bootstrap_is_deterministic():
    x = np.random.default_rng(1).normal(100, 5, 12)
    assert stats.summarize(x) == stats.summarize(x)
    assert stats.compare(x, x * 1.1) == stats.compare(x, x * 1.1)
    # the median of 12 values bootstraps onto order statistics; the mean shows the seed
    assert stats.bootstrap_ci(x, np.mean, seed=1) != stats.bootstrap_ci(x, np.mean, seed=2)


def test_single_value():
    s = stats.summarize([42.0])
    assert s["sd"] == 0.0 and s["ci95"] == [42.0, 42.0]


@pytest.mark.parametrize("bad", [[], [1.0, float("nan")], [float("inf")]])
def test_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        stats.summarize(bad)


def test_compare_identical_samples():
    x = [5.0, 6.0, 7.0, 8.0]
    c = stats.compare(x, x)
    assert c["ratio"] == 1.0
    assert c["p_value"] == pytest.approx(1.0)


def test_compare_separated_samples_exact_p():
    base, other = [1.0, 2.0, 3.0, 4.0, 5.0], [11.0, 12.0, 13.0, 14.0, 15.0]
    c = stats.compare(base, other)
    assert c["ratio"] == pytest.approx(13.0 / 3.0)
    # complete separation, n = m = 5: exact two-sided p = 2 / C(10, 5)
    assert c["p_value"] == pytest.approx(2 / comb(10, 5))
    assert c["mann_whitney_u"] == 25.0
    lo, hi = c["ci95"]
    assert lo <= c["ratio"] <= hi


def test_compare_paired_all_faster_exact_p():
    base = np.array([100.0, 98.0, 103.0, 99.0, 101.0, 97.0])
    ratios = np.array([1.05, 1.10, 1.15, 1.20, 1.25, 1.30])
    c = stats.compare_paired(base, base * ratios)
    assert c["ratio"] == pytest.approx(np.median(ratios))
    assert c["pairs_other_faster"] == 6
    # six positive, untied differences: exact two-sided signed-rank p = 2 / 2**6
    assert c["p_value"] == pytest.approx(2 / 2 ** 6)


def test_compare_paired_requires_equal_length():
    with pytest.raises(ValueError):
        stats.compare_paired([1.0, 2.0], [1.0])


def test_compare_paired_identical_has_no_test():
    c = stats.compare_paired([3.0, 4.0], [3.0, 4.0])
    assert c["ratio"] == 1.0 and c["p_value"] is None


def test_window_mean_is_time_weighted():
    t = [10.0, 20.0, 30.0, 40.0]
    y = [100.0, 100.0, 50.0, 50.0]
    assert stats.window_mean(t, y, 0, 20) == 100.0
    assert stats.window_mean(t, y, 20, 40) == 50.0
    assert stats.window_mean(t, y, 0, 40) == 75.0
    with pytest.raises(ValueError):
        stats.window_mean(t, y, 100, 200)
