"""Tests for rxlab.analysis.uniformity and the nonce-strategy experiment's setup."""
import importlib
import struct

import numpy as np
import pytest
from scipy import stats

from rxlab.analysis import uniformity as U
from rxlab.binding import VM, Cache, Flags, recommended_flags
from tests.test_binding_vectors import VECTORS

NS = importlib.import_module("experiments.nonce_strategies")


def _random_hashes(n, seed=0):
    return np.random.default_rng(seed).integers(0, 256, size=(n, 32), dtype=np.uint8)


# --- difficulty semantics -------------------------------------------------

def test_leading_zero_bits_matches_integer_bit_length():
    h = _random_hashes(2000, 1)
    h[:200, 31] = 0
    h[:50, 30] = 0
    h[:10, 29:] = 0
    h[0] = 0
    h[1] = 0
    h[1, 0] = 1
    expected = [256 - U.hash_int(row).bit_length() for row in h]
    assert U.leading_zero_bits(h).tolist() == expected
    assert U.leading_zero_bits(h)[0] == 256
    assert U.leading_zero_bits(h)[1] == 255


@pytest.mark.parametrize("k", [1, 8, 13, 16, 40])
def test_power_of_two_difficulty_is_leading_zero_count(k):
    h = _random_hashes(3000, k)
    h[::3, 31] = 0
    h[::7, 30] = 0
    lz = U.leading_zero_bits(h)
    assert [U.meets_difficulty(row, 2 ** k) for row in h] == (lz >= k).tolist()
    edge = (1 << (256 - k)) - 1
    assert U.meets_difficulty(edge.to_bytes(32, "little"), 2 ** k)
    assert not U.meets_difficulty((edge + 1).to_bytes(32, "little"), 2 ** k)


def _varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


# Monero mainnet, coinbase-only blocks (so the tx merkle root is the miner tx hash).
# Header fields from get_block_header_by_height on a public node; the RandomX key is
# the hash of seed block 2998272 = (height - 65) & ~2047.
MAINNET_SEED = "3c512c1a6e8210e985b47e855eaf93af952abb61b9bd032872a376910ba7d448"
MAINNET_BLOCKS = [
    # height, difficulty, nonce, timestamp, prev_hash, miner_tx_hash
    (3000002, 308854246283, 905969741, 1697813363,
     "2310f1d859f937755cbb75cfe3bbf694f6324c29313488b96d5a474550831511",
     "cec4dd89167ec076a948ecee0dac664ac07f52a9c13c0310ea6b244d1a559333"),
    (3000005, 308357644933, 2986414165, 1697813836,
     "3aca48eee33bfbfdfea282277e8c8ac78739f94638ca5085de15620d08fe04cc",
     "ecbfef84b1695cdd9c82db3c2f2b80529da9baed6b9a6c1bacf47207460ca6bd"),
]


def test_real_monero_blocks_meet_difficulty_little_endian():
    """Accepted mainnet blocks pass hash*D < 2**256 only when the hash is read little-endian."""
    flags = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    with Cache(bytes.fromhex(MAINNET_SEED), flags=flags) as cache, VM(flags, cache=cache) as vm:
        for _, difficulty, nonce, ts, prev, miner_tx in MAINNET_BLOCKS:
            blob = (_varint(16) + _varint(16) + _varint(ts) + bytes.fromhex(prev)
                    + struct.pack("<I", nonce) + bytes.fromhex(miner_tx) + _varint(1))
            assert len(blob) == 76 and blob[NS.NONCE_OFFSET:NS.NONCE_OFFSET + 4] == struct.pack("<I", nonce)
            pow_hash = vm.hash(blob)
            assert U.meets_difficulty(pow_hash, difficulty)
            assert int.from_bytes(pow_hash, "big") * difficulty >= 1 << 256
            assert U.leading_zero_bits([pow_hash])[0] >= difficulty.bit_length() - 1


def test_unit_interval_orders_like_hash_int():
    h = _random_hashes(500, 2)
    u = U.unit_interval(h)
    assert ((u >= 0) & (u < 1)).all()
    ints = [U.hash_int(row) for row in h]
    assert np.array_equal(np.argsort(u, kind="stable"), np.argsort(ints, kind="stable"))
    assert all(abs(x - i / 2 ** 256) < 2 ** -52 for x, i in zip(u, ints))


# --- test calibration: uniform data passes, biased data fails --------------

def test_uniform_bytes_pass_and_biased_bytes_fail():
    h = _random_hashes(200_000, 3)
    counts = U.byte_counts(h)
    assert U.byte_uniformity(counts)["joint_p_value"] > 1e-3
    assert U.bit_balance(counts)["p_value"] > 1e-3
    assert U.geometric_lz_test(U.leading_zero_bits(h))["p_value"] > 1e-3
    slight = h.copy()
    slight[::50, 31] &= 0x7F  # top bit cleared in 2% of rows: ~1% more leading zeros
    assert U.geometric_lz_test(U.leading_zero_bits(slight))["p_value"] < 1e-6
    # ~2,000 flipped bits against sd 224 puts z near 8.9 +- 1; > 5 keeps a wide margin
    assert U.bit_balance(U.byte_counts(slight))["max_abs_z"] > 5
    # the joint tests spread one biased bit over 256 bits / 8160 cells, so need more bias
    strong = h.copy()
    strong[::10, 31] &= 0x7F
    assert U.bit_balance(U.byte_counts(strong))["p_value"] < 1e-6
    assert U.byte_uniformity(U.byte_counts(strong))["joint_p_value"] < 1e-6


def test_byte_counts_and_bit_balance_agree_with_unpackbits():
    h = _random_hashes(5000, 4)
    counts = U.byte_counts(h)
    assert counts.shape == (32, 256) and (counts.sum(axis=1) == 5000).all()
    ones = np.unpackbits(h, axis=1, bitorder="little").sum(axis=0, dtype=np.int64)
    z = (ones - 2500) / np.sqrt(1250)
    assert U.bit_balance(counts)["statistic"] == pytest.approx(float((z ** 2).sum()))


def test_pooled_chisquare_matches_scipy_without_pooling():
    obs = np.array([30, 25, 20, 25])
    exp = np.array([25.0, 25.0, 25.0, 25.0])
    ref = stats.chisquare(obs, exp)
    res = U.pooled_chisquare(obs, exp)
    assert res["statistic"] == pytest.approx(ref.statistic)
    assert res["p_value"] == pytest.approx(ref.pvalue)


def test_binomial_check_uses_exact_test():
    r = U.binomial_check(30, 4096, 1 / 256)
    ref = stats.binomtest(30, 4096, 1 / 256)
    assert r["p_value"] == pytest.approx(ref.pvalue)
    ci = ref.proportion_ci(method="exact")
    assert r["ratio_ci95"] == pytest.approx([ci.low * 256, ci.high * 256])


def test_homogeneity_chi2_matches_scipy_and_detects_difference():
    rng = np.random.default_rng(5)
    hits, trials = [100, 120, 90], [25600, 25600, 25600]
    table = np.array([hits, [t - h for h, t in zip(hits, trials)]]).T
    ref = stats.chi2_contingency(table, correction=False)
    res = U.homogeneity(hits, trials, rng)
    assert res["method"] == "chi2"
    assert res["p_value"] == pytest.approx(ref.pvalue)
    assert U.homogeneity([100, 200, 100], trials, rng)["p_value"] < 1e-6


def test_homogeneity_small_counts_uses_exact_null():
    rng = np.random.default_rng(6)
    res = U.homogeneity([2, 3, 1, 2], [4096] * 4, rng, draws=20_000)
    assert res["method"].startswith("exact")
    assert res["p_value"] > 0.3
    assert U.homogeneity([0, 0, 0, 12], [4096] * 4, rng, draws=20_000)["p_value"] < 1e-3


def test_serial_correlation_detects_dependence():
    rng = np.random.default_rng(7)
    u = rng.random(50_000)
    assert all(r["p_value"] > 1e-3 for r in U.serial_correlation(u, (1, 2, 3)))
    ar = u.copy()
    ar[1:] = 0.9 * u[1:] + 0.1 * u[:-1]
    assert U.serial_correlation(ar, (1,))[0]["p_value"] < 1e-6


def test_binomial_gof_and_avalanche_on_random_and_weak_mixing():
    rng = np.random.default_rng(8)
    base = rng.integers(0, 256, size=(3000, 32), dtype=np.uint8)
    flipped = rng.integers(0, 256, size=(3000, 4, 32), dtype=np.uint8)
    d, flips = U.avalanche(base, flipped)
    assert d.shape == (3000, 4) and flips.shape == (4, 256)
    assert (d == U.hamming(base[:, None, :], flipped)).all()
    assert all(U.binomial_gof(d[:, j])["p_value"] > 1e-3 for j in range(4))
    assert U.sac_test(flips, 3000)["p_value"] > 1e-3
    weak = flipped.copy()
    weak[:, 0, :4] = base[:, :4]  # 32 output bits never change when input bit 0 flips
    d2, flips2 = U.avalanche(base, weak)
    assert U.binomial_gof(d2[:, 0])["mean_p_value"] < 1e-6
    assert U.sac_test(flips2, 3000)["p_value"] < 1e-6


def test_gap_tests_accept_geometric_and_reject_regular_arrivals():
    rng = np.random.default_rng(9)
    p = 1 / 256
    mask = rng.random(400_000) < p
    gaps = U.hit_gaps(mask)
    assert gaps.sum() == np.flatnonzero(mask)[-1] + 1
    assert U.geometric_gap_test(gaps, p)["p_value"] > 1e-3
    waits = U.residual_waits(gaps, [0, 128, 256, 512])
    assert all(abs(w["mean_remaining"] - 256) < 4 * w["se"] for w in waits)
    regular = np.full(1500, 256)
    assert U.geometric_gap_test(regular, p)["p_value"] < 1e-6


def test_logistic_auc_finds_real_signal_only():
    rng = np.random.default_rng(10)
    nonces = rng.integers(0, 1 << 32, 40_000, dtype=np.uint64)
    X = U.nonce_bits(nonces)
    assert X.shape == (40_000, 32) and (X[:, 3] == ((nonces >> np.uint64(3)) & np.uint64(1))).all()
    noise = (rng.random(40_000) < 0.5).astype(float)
    signal = ((X[:, 3] + rng.random(40_000) * 1.2) > 1.0).astype(float)
    for y, lo, hi in ((noise, 0.47, 0.53), (signal, 0.7, 1.0)):
        w = U.logistic_fit(X[:20_000], y[:20_000])
        a, _ = U.auc(U.logistic_score(w, X[20_000:]), y[20_000:])
        assert lo < a < hi


def test_auc_matches_pairwise_definition():
    rng = np.random.default_rng(11)
    s, y = rng.random(300), rng.random(300) < 0.3
    pairs = (s[y][:, None] > s[~y][None, :]).mean()
    assert U.auc(s, y)[0] == pytest.approx(pairs)


def test_holm_textbook_example():
    # sorted: 0.005*4, 0.01*3, 0.03*2, 0.04*1 -> running max 0.02, 0.03, 0.06, 0.06
    assert U.holm([0.01, 0.04, 0.03, 0.005]).tolist() == pytest.approx([0.03, 0.06, 0.06, 0.02])


# --- experiment setup --------------------------------------------------------

def test_experiment_blob_is_official_vector_1e():
    key, data, h1, h2 = VECTORS[4]
    assert (NS.KEY, NS.BLOB, NS.GOLDEN["v1"], NS.GOLDEN["v2"]) == (key, data, h1, h2)
    assert len(NS.BLOB) == 76 and NS.BLOB[39:43] == b"\0\0\0\0"


def test_blobs_write_little_endian_nonce_at_offset_39():
    nonces = np.array([0, 1, 0x01020304, 0xFFFFFFFF], dtype=np.uint32)
    for n, blob in zip(nonces.tolist(), NS.blobs(nonces)):
        assert blob[:39] == NS.BLOB[:39] and blob[43:] == NS.BLOB[43:]
        assert blob[39:43] == n.to_bytes(4, "little")


def test_strategy_nonce_sequences():
    log2n = 12
    n = 1 << log2n
    sets = NS.strategy_nonces(log2n, np.random.default_rng(0))
    assert list(sets) == list(NS.STRATEGIES)
    for name, s in sets.items():
        assert s.dtype == np.uint32 and len(s) == n and len(np.unique(s)) == n, name
    assert (sets["sequential"] == np.arange(n)).all()
    assert (np.diff(sets["sequential_random_start"].astype(np.int64)) % (1 << 32) == 1).all()
    g = sets["gray_code"].astype(np.int64)
    assert all(bin(int(x)).count("1") == 1 for x in np.bitwise_xor(g[:-1], g[1:]))
    assert len(np.unique(g >> log2n)) == 1 and g[0] >> log2n != 0
    st = sets["strided_65536"].astype(np.int64)
    assert (st[:4] == [0, 1 << 16, 2 << 16, 3 << 16]).all() and (st & 0xFFFF == 0).all()
    w = np.array([bin(int(v)).count("1") for v in sets["low_hamming_weight"]])
    assert (np.diff(w) >= 0).all() and w[0] == 0 and (w[1:33] == 1).all()
    wmax = w.max()
    assert (w < wmax).sum() == sum(int(stats.binom.pmf(j, 32, 0.5) * 2 ** 32 + 0.5) for j in range(wmax))


def test_low_hamming_first_crosses_weight_classes():
    v = NS.low_hamming_first(1 + 32 + 496 + 10)
    w = [bin(int(x)).count("1") for x in v]
    assert w.count(0) == 1 and w.count(1) == 32 and w.count(2) == 496 and w.count(3) == 10
    assert (np.diff(v[33:529].astype(np.int64)) > 0).all()
    assert v[529] == 0b111


def test_avalanche_nonces_layout():
    a = NS.avalanche_nonces(5, np.random.default_rng(1)).reshape(5, 33).astype(np.int64)
    for row in a:
        assert sorted(np.bitwise_xor(row[1:], row[0]).tolist()) == [1 << b for b in range(32)]


def test_experiment_hashes_match_single_hash_api():
    """The experiment's blob builder plus batch hashing equals one-shot hashing (light mode)."""
    flags = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    nonces = np.array([0, 7, 123456789], dtype=np.uint32)
    with Cache(NS.KEY, flags=flags) as cache, VM(flags, cache=cache) as vm:
        batch = list(vm.hash_many(NS.blobs(nonces)))
        single = [vm.hash(NS.BLOB[:39] + int(n).to_bytes(4, "little") + NS.BLOB[43:]) for n in nonces]
    assert batch == single
    assert batch[0].hex() == NS.GOLDEN["v1"]
