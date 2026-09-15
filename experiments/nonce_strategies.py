"""Can a smarter nonce search beat brute force? Six nonce orders on real RandomX outputs.

Every strategy hashes the same Monero block hashing blob (official test vector
1e, key b"test key 001") with the 4-byte little-endian nonce at offset 39
varied, under an equal hash budget. Raw hashes are checkpointed to data/cache/
so an interrupted run resumes and the analysis reruns offline. The analysis
writes results/nonce_strategies.json and figures/nonce_*.png.

    python3 experiments/nonce_strategies.py                # hash (resumable), then analyse
    python3 experiments/nonce_strategies.py --analyse-only # reuse data/cache only
    python3 experiments/nonce_strategies.py --smoke        # tiny budgets, outputs under data/cache/
"""
import argparse
import json
import queue
import struct
import sys
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import plotstyle, sysinfo  # noqa: E402
from rxlab.analysis import uniformity as U  # noqa: E402
from rxlab.binding import DEFAULT_TAG, VM, Cache, Dataset, Flags, recommended_flags  # noqa: E402

KEY = b"test key 001"
# Test vector 1e of src/tests/tests.cpp: a real 76-byte Monero block hashing blob
# whose nonce field (bytes 39..42) is zero. GOLDEN are its official hashes.
BLOB = bytes.fromhex(
    "0b0b98bea7e805e0010a2126d287a2a0cc833d312cb786385a7c2f9de69d25537f584a9bc9977b"
    "00000000666fd8753bf61a8631f12984e3fd44f4014eca629276817b56f32e9b68bd82f416")
NONCE_OFFSET = 39
GOLDEN = {
    "v1": "c56414121acda1713c2f2a819d8ae38aed7c80c35c2a769298d34f03833cd5f1",
    "v2": "c8e92c5f7c1946fecf06bc382b92e3111da38ee3e6a5ad90704e1a9d8aaf6e76",
}

SEED = 1009
STRATEGIES = {
    "sequential": "nonces 0, 1, 2, ...",
    "sequential_random_start": "s, s+1, ... from a random 32-bit s (mod 2**32)",
    "uniform_random": "uniformly random 32-bit nonces, without replacement",
    "strided_65536": "2**16 pool-style partitions of 2**16 nonces, visited round-robin "
                     "(nonce = (i mod 2**16) * 2**16 + i // 2**16)",
    "gray_code": "binary-reflected Gray code i ^ (i >> 1) over a random aligned block; "
                 "consecutive nonces differ in one bit",
    "low_hamming_weight": "nonces by increasing popcount (0, 1, 2, ... set bits), ascending within a weight",
}
BUDGETS = {"v1": {"log2_per_strategy": 18, "avalanche_bases": 2048},
           "v2": {"log2_per_strategy": 15, "avalanche_bases": 512}}
SMOKE_BUDGETS = {"v1": {"log2_per_strategy": 10, "avalanche_bases": 32},
                 "v2": {"log2_per_strategy": 9, "avalanche_bases": 16}}
THRESHOLDS = range(8, 17)
GAP_LEVELS = (8, 10, 12)
ELAPSED = (0.0, 0.5, 1.0, 2.0, 3.0)
SERIAL_LAGS = (1, 2, 3, 4)
LEARN_LEVELS = (1, 8)
CHUNK = 4096
MASK32 = (1 << 32) - 1


def low_hamming_first(n):
    """The n smallest-popcount 32-bit values, ascending within each popcount."""
    out = [0]
    w = 1
    while len(out) < n:
        v = (1 << w) - 1
        while v <= MASK32 and len(out) < n:
            out.append(v)
            c = v & -v
            r = v + c
            v = (((r ^ v) >> 2) // c) | r  # Gosper's hack: next value with w bits set
        w += 1
    return np.array(out[:n], dtype=np.uint32)


def strategy_nonces(log2n, rng):
    """Nonce sequence of every strategy, in search order, n = 2**log2n each."""
    n = 1 << log2n
    i = np.arange(n, dtype=np.uint64)
    start = int(rng.integers(0, 1 << 32))
    block = int(rng.integers(1, 1 << (32 - log2n))) << log2n
    return {
        "sequential": i.astype(np.uint32),
        "sequential_random_start": ((i + np.uint64(start)) & np.uint64(MASK32)).astype(np.uint32),
        "uniform_random": rng.choice(1 << 32, n, replace=False).astype(np.uint32),
        "strided_65536": (((i & np.uint64(0xFFFF)) << np.uint64(16)) | (i >> np.uint64(16))).astype(np.uint32),
        "gray_code": (np.uint64(block) | (i ^ (i >> np.uint64(1)))).astype(np.uint32),
        "low_hamming_weight": low_hamming_first(n),
    }


def avalanche_nonces(m, rng):
    """m random base nonces, each followed by its 32 one-bit flips: (m * 33,) uint32."""
    bases = rng.choice(1 << 32, m, replace=False).astype(np.uint32)
    masks = np.concatenate([[0], 1 << np.arange(32, dtype=np.uint64)]).astype(np.uint32)
    return (bases[:, None] ^ masks[None, :]).ravel()


def plan(budgets):
    """{version: {set name: nonces}}; strategies plus the 'avalanche' set."""
    out = {}
    for idx, (version, b) in enumerate(sorted(budgets.items()), start=1):
        rng = np.random.default_rng([SEED, idx])
        sets = strategy_nonces(b["log2_per_strategy"], rng)
        sets["avalanche"] = avalanche_nonces(b["avalanche_bases"], rng)
        out[version] = sets
    return out


def blobs(nonces):
    buf = bytearray(BLOB)
    for n in nonces.tolist():
        struct.pack_into("<I", buf, NONCE_OFFSET, n)
        yield bytes(buf)


def vm_flags(version):
    flags = recommended_flags() | Flags.FULL_MEM
    return flags | Flags.V2 if version == "v2" else flags


def chunk_path(cache_dir, version, name, i):
    return cache_dir / version / name / f"{i // CHUNK:04d}.npz"


def load_chunk(path, nonces):
    """Cached hashes for exactly these nonces, else None."""
    try:
        with np.load(path) as z:
            if np.array_equal(z["nonce"], nonces):
                return z["hash"]
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        pass
    return None


def save_chunk(path, nonces, hashes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.npz")
    np.savez(tmp, nonce=nonces, hash=hashes)
    tmp.replace(path)


def hash_all(sets_by_version, cache_dir, threads):
    """Hash every missing chunk with `threads` VMs sharing one fast-mode dataset."""
    todo = []
    for version, sets in sets_by_version.items():
        for name, nonces in sets.items():
            for i in range(0, len(nonces), CHUNK):
                path = chunk_path(cache_dir, version, name, i)
                if load_chunk(path, nonces[i:i + CHUNK]) is None:
                    todo.append((version, path, nonces[i:i + CHUNK]))
    if not todo:
        print("all hashes cached", flush=True)
        return
    total = sum(len(t[2]) for t in todo)
    print(f"{total} hashes to compute in {len(todo)} chunks", flush=True)
    with Cache(KEY, flags=vm_flags("v1")) as cache:
        dataset = Dataset(cache, threads=threads)
    try:
        for version in sets_by_version:
            with VM(vm_flags(version), dataset=dataset) as vm:
                got = vm.hash(BLOB).hex()
            if got != GOLDEN[version]:
                raise RuntimeError(f"{version} golden hash mismatch: {got}")
        work = queue.Queue()
        for t in todo:
            work.put(t)
        errors, lock, done, t0 = [], threading.Lock(), [0], time.monotonic()

        def worker():
            vms = {}
            try:
                while True:
                    try:
                        version, path, nonces = work.get_nowait()
                    except queue.Empty:
                        return
                    if version not in vms:
                        vms[version] = VM(vm_flags(version), dataset=dataset)
                    hashes = U.as_hash_array(list(vms[version].hash_many(blobs(nonces))))
                    save_chunk(path, nonces, hashes)
                    with lock:
                        done[0] += len(nonces)
                        rate = done[0] / (time.monotonic() - t0)
                        print(f"{done[0]}/{total} hashes ({rate:.0f}/s, {path.parent.parent.name} "
                              f"{path.parent.name} {path.stem})", flush=True)
            except Exception as exc:  # re-raised in the main thread after join
                errors.append(exc)
            finally:
                for vm in vms.values():
                    vm.close()

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        if errors:
            raise errors[0]
    finally:
        dataset.close()


def load_all(sets_by_version, cache_dir):
    """{version: {name: (nonces, hashes)}} from the checkpoint files."""
    out = {}
    for version, sets in sets_by_version.items():
        out[version] = {}
        for name, nonces in sets.items():
            parts = []
            for i in range(0, len(nonces), CHUNK):
                h = load_chunk(chunk_path(cache_dir, version, name, i), nonces[i:i + CHUNK])
                if h is None:
                    raise FileNotFoundError(f"missing hashes for {version}/{name} chunk {i // CHUNK}")
                parts.append(h)
            out[version][name] = (nonces, np.concatenate(parts))
    return out


def analyse_version(version, data, rng):
    """All tests for one RandomX version; returns (results, labelled p-values, figure data)."""
    strat = {name: data[name] for name in STRATEGIES}
    n = len(strat["sequential"][0])
    if strat["sequential"][1][0].tobytes().hex() != GOLDEN[version]:
        raise RuntimeError(f"{version}: nonce 0 does not reproduce the official test vector")

    all_n = np.concatenate([data[k][0] for k in data])
    all_h = np.concatenate([data[k][1] for k in data])
    _, first, inverse = np.unique(all_n, return_index=True, return_inverse=True)
    if not np.array_equal(all_h, all_h[first][inverse.ravel()]):
        raise RuntimeError(f"{version}: the same nonce hashed to different values")
    pooled_n, pooled_h = all_n[first], all_h[first]

    pvals = []
    lz = {name: U.leading_zero_bits(h) for name, (_, h) in strat.items()}

    hits = {}
    for name in STRATEGIES:
        hits[name] = {}
        for k in THRESHOLDS:
            r = U.binomial_check((lz[name] >= k).sum(), n, 2.0 ** -k)
            hits[name][f"2^{k}"] = r
            pvals.append((f"{version} hits {name} D=2^{k}", r["p_value"]))

    seen = np.empty(0, dtype=np.uint32)
    exclusive = {}
    for name in STRATEGIES:
        nonces = strat[name][0]
        exclusive[name] = ~np.isin(nonces, seen)
        seen = np.concatenate([seen, nonces])
    homog = {}
    for k in THRESHOLDS:
        r = U.homogeneity([(lz[s][exclusive[s]] >= k).sum() for s in STRATEGIES],
                          [exclusive[s].sum() for s in STRATEGIES], rng)
        homog[f"2^{k}"] = r
        pvals.append((f"{version} homogeneity D=2^{k}", r["p_value"]))

    lz_tests = {name: U.geometric_lz_test(lz[name]) for name in STRATEGIES}
    lz_tests["pooled_unique"] = U.geometric_lz_test(U.leading_zero_bits(pooled_h))
    for name, r in lz_tests.items():
        pvals.append((f"{version} leading-zeros {name}", r["p_value"]))

    counts = U.byte_counts(pooled_h)
    byte_res, bit_res = U.byte_uniformity(counts), U.bit_balance(counts)
    pvals += [(f"{version} byte-position {j}", p) for j, p in enumerate(byte_res["p_value"])]
    pvals += [(f"{version} bytes joint", byte_res["joint_p_value"]),
              (f"{version} bit balance", bit_res["p_value"])]

    serial = {}
    for name in ("sequential", "sequential_random_start", "gray_code"):
        h = strat[name][1]
        entry = {"adjacent_hamming": U.binomial_gof(U.hamming(h[:-1], h[1:]))}
        if name != "gray_code":
            entry["pearson"] = U.serial_correlation(U.unit_interval(h), SERIAL_LAGS)
            pvals += [(f"{version} serial r {name} lag {c['lag']}", c["p_value"]) for c in entry["pearson"]]
        pvals += [(f"{version} adjacent hamming {name}", entry["adjacent_hamming"]["p_value"]),
                  (f"{version} adjacent hamming mean {name}", entry["adjacent_hamming"]["mean_p_value"])]
        serial[name] = entry

    m = len(data["avalanche"][0]) // 33
    av = data["avalanche"][1].reshape(m, 33, 32)
    distances, flips = U.avalanche(av[:, 0], av[:, 1:])
    per_bit = []
    for b in range(32):
        r = U.binomial_gof(distances[:, b])
        r["nonce_bit"] = b
        per_bit.append(r)
        pvals += [(f"{version} avalanche bit {b}", r["p_value"]),
                  (f"{version} avalanche mean bit {b}", r["mean_p_value"])]
    sac = U.sac_test(flips, m)
    pvals.append((f"{version} strict avalanche matrix", sac["p_value"]))

    X = U.nonce_bits(pooled_n)
    pooled_lz = U.leading_zero_bits(pooled_h)
    perm = rng.permutation(len(X))
    train, test = perm[: len(X) // 2], perm[len(X) // 2:]
    learn = {}
    for k in LEARN_LEVELS:
        y = (pooled_lz >= k).astype(np.float64)
        w = U.logistic_fit(X[train], y[train])
        a, p = U.auc(U.logistic_score(w, X[test]), y[test])
        n1 = y[test].sum()
        n0 = len(test) - n1
        learn[f"2^{k}"] = {"target": f"hash * 2^{k} < 2^256", "n_train": int(len(train)),
                           "n_test": int(len(test)), "test_positives": int(n1),
                           "auc_out_of_sample": a, "p_value": p,
                           "auc_se_under_null": float(np.sqrt((n1 + n0 + 1) / (12 * n1 * n0))),
                           "max_abs_coefficient": float(np.abs(w[1:]).max())}
        pvals.append((f"{version} learnability D=2^{k}", p))

    gaps, gap_fig = {}, {}
    for k in GAP_LEVELS:
        d = 2.0 ** k
        per = {name: U.hit_gaps(lz[name] >= k) for name in STRATEGIES}
        pooled = np.concatenate(list(per.values()))
        r = U.geometric_gap_test(pooled, 1 / d)
        r["residual_wait"] = U.residual_waits(pooled, [f * d for f in ELAPSED])
        r["per_strategy_mean_gap"] = {s: (float(g.mean()) if len(g) else None) for s, g in per.items()}
        gaps[f"2^{k}"] = r
        gap_fig[k] = pooled
        if r["p_value"] is not None:
            pvals.append((f"{version} inter-hit gaps D=2^{k}", r["p_value"]))

    results = {
        "hashes_per_strategy": n,
        "avalanche_bases": m,
        "unique_nonces_hashed": int(len(pooled_n)),
        "hashes_computed": int(len(all_n)),
        "golden_nonce0_hash": GOLDEN[version],
        "overlap_with_earlier_strategies": {s: int((~exclusive[s]).sum()) for s in STRATEGIES},
        "hit_counts": hits,
        "homogeneity_exclusive_nonces": homog,
        "leading_zero_bits": lz_tests,
        "byte_uniformity": byte_res,
        "bit_balance": bit_res,
        "serial": serial,
        "avalanche": {"per_nonce_bit": per_bit, "strict_avalanche": sac},
        "learnability": learn,
        "inter_hit_gaps": gaps,
    }
    fig = {"hits": hits, "lz": lz_tests["pooled_unique"], "bytes": byte_res, "serial": serial,
           "distances": distances, "sac_z": U.sac_z(flips, m), "gaps": gap_fig, "residual": gaps}
    return results, pvals, fig


def figures(figs, pvals, adjusted, out_dir):
    plt = plotstyle.apply()
    color = dict(zip(STRATEGIES, plotstyle.PALETTE))
    ks = list(THRESHOLDS)

    fig, axes = plt.subplots(1, len(figs), figsize=(11, 4.2), sharey=True, squeeze=False)
    for ax, (version, f) in zip(axes[0], figs.items()):
        for j, name in enumerate(STRATEGIES):
            x = np.array(ks) + (j - 2.5) * 0.11
            r = [f["hits"][name][f"2^{k}"] for k in ks]
            y = np.array([c["ratio"] for c in r])
            lo = y - np.array([c["ratio_ci95"][0] for c in r])
            hi = np.array([c["ratio_ci95"][1] for c in r]) - y
            ax.errorbar(x, y, yerr=[lo, hi], fmt="o", ms=3.5, lw=1, capsize=0, color=color[name], label=name)
        ax.axhline(1, color="0.3", lw=1)
        ax.set_xticks(ks)
        ax.set_xticklabels([f"$2^{{{k}}}$" for k in ks])
        ax.set_xlabel("difficulty D")
        ax.set_title(f"RandomX {version}: {f['hits']['sequential']['2^8']['trials']:,} hashes per strategy")
    axes[0][0].set_ylabel("hits / (hashes / D), exact 95% CI")
    axes[0][0].set_ylim(0, 3)
    fig.legend(*axes[0][0].get_legend_handles_labels(), loc="upper center", bbox_to_anchor=(0.5, 0.0),
               ncol=len(STRATEGIES), fontsize=8, frameon=False)
    fig.suptitle("Every nonce strategy finds hits at the brute-force rate (1 = expected; axis clipped at 3)")
    fig.savefig(out_dir / "nonce_hit_rates.png")
    plt.close(fig)

    fig, ax = plt.subplots()
    for (version, f), c in zip(figs.items(), plotstyle.PALETTE):
        obs, exp = np.array(f["lz"]["observed"]), np.array(f["lz"]["expected"])
        j = np.arange(len(obs))
        ax.plot(j, exp, color=c, lw=1.5, alpha=0.6)
        ax.plot(j[obs > 0], obs[obs > 0], "o", ms=4, color=c, label=f"{version} observed (n={f['lz']['n']:,})")
    ax.plot([], [], color="0.4", lw=1.5, label="geometric(1/2) expectation")
    ax.set_yscale("log")
    ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax.set_xlabel("leading zero bits of the little-endian hash integer (D = 2^k needs >= k)")
    ax.set_ylabel("hashes")
    ax.set_title("Leading zero bits follow the fair-coin geometric law")
    ax.legend(frameon=False)
    fig.savefig(out_dir / "nonce_leading_zeros.png")
    plt.close(fig)

    f = figs["v1"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [1, 1.6]})
    d = f["distances"]
    mean = d.mean(axis=0)
    band = 1.96 * 8 / np.sqrt(len(d))
    a1.axhspan(128 - band, 128 + band, color="0.85", lw=0, label="95% band for Binomial(256, 1/2) mean")
    a1.plot(np.arange(32), mean, "o", ms=4, color=plotstyle.PALETTE[0], label="observed mean")
    a1.set_xlabel("flipped nonce bit")
    a1.set_ylabel("mean Hamming distance (bits of 256)")
    a1.set_ylim(128 - 2.5 * band, 128 + 2.5 * band)
    a1.set_title(f"v1 avalanche, {len(d):,} bases per nonce bit")
    a1.legend(frameon=False, fontsize=8, loc="upper right")
    im = a2.imshow(f["sac_z"], aspect="auto", cmap="RdBu_r", vmin=-4, vmax=4, interpolation="nearest")
    a2.grid(False)
    a2.set_xlabel("output bit (little-endian; 255 = most significant)")
    a2.set_ylabel("flipped nonce bit")
    a2.set_title("strict avalanche: z of each flip rate vs 1/2")
    fig.colorbar(im, ax=a2, label="z")
    fig.savefig(out_dir / "nonce_avalanche.png")
    plt.close(fig)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2))
    for k, c in zip(GAP_LEVELS, plotstyle.PALETTE):
        g = np.sort(f["gaps"][k]) / 2.0 ** k
        surv = 1 - np.arange(1, len(g) + 1) / len(g)
        a1.step(g, surv, where="post", color=c, lw=1.5, label=f"D = $2^{{{k}}}$ ({len(g):,} gaps)")
        rw = f["residual"][f"2^{k}"]["residual_wait"]
        ok = [r for r in rw if r["n"] > 1]
        a2.errorbar([r["elapsed"] / 2.0 ** k for r in ok], [r["mean_remaining"] / 2.0 ** k for r in ok],
                    yerr=[1.96 * r["se"] / 2.0 ** k for r in ok], fmt="o-", ms=4, lw=1.2, capsize=2,
                    color=c, label=f"D = $2^{{{k}}}$")
    x = np.linspace(0, 8, 200)
    a1.plot(x, np.exp(-x), color="0.3", lw=1, ls="--", label="memoryless: exp(-x)")
    a1.set_yscale("log")
    a1.set_ylim(1e-3, 1)
    a1.set_xlim(0, 8)
    a1.set_xlabel("hashes between hits / D")
    a1.set_ylabel("fraction of gaps longer")
    a1.set_title("v1 inter-hit gaps, all strategies pooled")
    a1.legend(frameon=False, fontsize=8)
    a2.axhline(1, color="0.3", lw=1, ls="--")
    a2.set_xlabel("hashes already spent without a hit / D")
    a2.set_ylabel("mean further hashes to a hit / D (95% CI)")
    a2.set_title("No progress: the remaining wait never shrinks")
    a2.set_ylim(0, 2)
    a2.legend(frameon=False, fontsize=8)
    fig.savefig(out_dir / "nonce_gaps.png")
    plt.close(fig)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2))
    chi = np.array(f["bytes"]["statistic"])
    lo, hi = stats.chi2.ppf([0.025, 0.975], 255)
    a1.axhspan(lo, hi, color="0.85", lw=0, label="central 95% of chi2(255)")
    a1.plot(np.arange(32), chi, "o", ms=4, color=plotstyle.PALETTE[0], label="observed")
    a1.set_xlabel("hash byte position (31 = most significant)")
    a1.set_ylabel("chi-square vs uniform bytes")
    a1.set_title(f"v1 byte uniformity, {f['bytes']['n']:,} unique hashes")
    a1.legend(frameon=False, fontsize=8)
    nmin = min(r["n"] for name in ("sequential", "sequential_random_start") for r in f["serial"][name]["pearson"])
    a2.axhspan(-1.96 / np.sqrt(nmin), 1.96 / np.sqrt(nmin), color="0.85", lw=0, label="95% band, r = 0")
    for name, off in (("sequential", -0.08), ("sequential_random_start", 0.08)):
        rows = f["serial"][name]["pearson"]
        a2.plot([r["lag"] + off for r in rows], [r["r"] for r in rows], "o", ms=5, color=color[name], label=name)
    a2.axhline(0, color="0.3", lw=1)
    a2.set_xticks(list(SERIAL_LAGS))
    a2.set_xlabel("nonce lag")
    a2.set_ylabel("Pearson r of hash / 2^256")
    a2.set_title("v1 correlation between hashes of nearby nonces")
    a2.legend(frameon=False, fontsize=8)
    fig.savefig(out_dir / "nonce_uniformity.png")
    plt.close(fig)

    p = np.sort(np.array([v for _, v in pvals]))
    m = len(p)
    expected = (np.arange(1, m + 1) - 0.5) / m
    fig, ax = plt.subplots(figsize=(5.5, 5))
    ax.plot(-np.log10(expected), -np.log10(p), "o", ms=3, color=plotstyle.PALETTE[0], label=f"{m} tests")
    top = max(-np.log10(expected).max(), -np.log10(p).max(), -np.log10(0.05 / m)) + 0.3
    ax.plot([0, top], [0, top], color="0.3", lw=1)
    ax.axhline(-np.log10(0.05 / m), color=plotstyle.PALETTE[3], lw=1, ls="--",
               label="Holm cut for the smallest p (0.05 / m)")
    ax.set_xlabel("expected -log10 p (uniform)")
    ax.set_ylabel("observed -log10 p")
    ax.set_title(f"All p-values vs the null; min Holm-adjusted p = {adjusted.min():.2f}")
    ax.legend(frameon=False, fontsize=8)
    fig.savefig(out_dir / "nonce_pvalues.png")
    plt.close(fig)


def _tidy(x):
    """JSON-ready copy with floats at 6 significant digits (stable across BLAS builds)."""
    if isinstance(x, dict):
        return {k: _tidy(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_tidy(v) for v in x]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return float(f"{float(x):.6g}")
    return x


def analyse(sets_by_version, budgets, cache_dir, results_path, fig_dir):
    data = load_all(sets_by_version, cache_dir)
    rng = np.random.default_rng([SEED, 99])
    versions, figs, pvals = {}, {}, []
    for version in sorted(data):
        res, pv, fig = analyse_version(version, data[version], rng)
        versions[version], figs[version] = res, fig
        pvals += pv
        print(f"{version}: {len(pv)} tests analysed", flush=True)
    adjusted = U.holm([p for _, p in pvals])
    raw = np.array([p for _, p in pvals])
    i_raw, i_adj = int(raw.argmin()), int(adjusted.argmin())
    holm = {
        "tests": len(pvals),
        "min_raw_p": {"test": pvals[i_raw][0], "p": float(raw[i_raw])},
        "min_adjusted_p": {"test": pvals[i_adj][0], "p": float(adjusted[i_adj])},
        "rejected_at_0.05": int((adjusted < 0.05).sum()),
        "raw_p_below_0.05": int((raw < 0.05).sum()),
        "expected_raw_below_0.05_under_null": 0.05 * len(pvals),
        "all": [{"test": t, "p": p, "holm_p": float(a)} for (t, p), a in zip(pvals, adjusted)],
    }
    out = {
        "experiment": "nonce_strategies",
        "question": "Does any nonce order, nonce structure or output pattern find difficulty-meeting "
                    "RandomX hashes faster than blind trial?",
        "machine": sysinfo.describe(),
        "setup": {
            "library": f"tevador/RandomX {DEFAULT_TAG} (computes v1 and v2; Flags.V2 selects v2)",
            "mode": "fast (2080 MiB dataset), JIT, hardware AES",
            "key": KEY.decode(),
            "blob_hex": BLOB.hex(),
            "blob_source": "RandomX src/tests/tests.cpp test vector 1e (a real Monero block hashing blob)",
            "nonce": f"4 bytes little-endian at byte offset {NONCE_OFFSET}",
            "difficulty_rule": "accept when int.from_bytes(hash, 'little') * D < 2**256; for D = 2**k "
                               "this is >= k leading zero bits counted from the top of byte 31",
            "seed": SEED,
            "budgets": budgets,
            "strategies": STRATEGIES,
            "homogeneity_note": "chi-square across strategies uses only nonces not already hashed by an "
                                "earlier strategy in the listed order, so the groups are disjoint",
        },
        "versions": versions,
        "holm": holm,
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(_tidy(out), indent=1) + "\n")
    fig_dir.mkdir(parents=True, exist_ok=True)
    figures(figs, pvals, adjusted, fig_dir)
    print(f"{len(pvals)} tests; min raw p {raw.min():.3g} ({pvals[i_raw][0]}); "
          f"min Holm p {adjusted.min():.3g}; rejected {holm['rejected_at_0.05']}", flush=True)
    print(f"wrote {results_path}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--smoke", action="store_true", help="tiny budgets; outputs stay under data/cache/")
    ap.add_argument("--analyse-only", action="store_true", help="skip hashing, use cached hashes")
    args = ap.parse_args()
    budgets = SMOKE_BUDGETS if args.smoke else BUDGETS
    if args.smoke:
        cache_dir = ROOT / "data" / "cache" / "nonce_strategies_smoke"
        results_path, fig_dir = cache_dir / "nonce_strategies.json", cache_dir / "figures"
    else:
        cache_dir = ROOT / "data" / "cache" / "nonce_strategies"
        results_path, fig_dir = ROOT / "results" / "nonce_strategies.json", ROOT / "figures"
    sets = plan(budgets)
    if not args.analyse_only:
        hash_all(sets, cache_dir, args.threads)
    analyse(sets, budgets, cache_dir, results_path, fig_dir)


if __name__ == "__main__":
    main()
