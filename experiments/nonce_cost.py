"""Nonce cherry-picking: can program 0 tell a miner which nonces are slow to hash?

Every nonce has the same chance of solving a block, so only hashrate matters.
Program 0 depends only on Blake2b(input), so its features are known before the
hash is run. If they predicted hash time, skipping the predicted-slowest nonces
would raise hashrate. For K nonces of a real Monero blob (test vector 1e, nonce
at byte 39) this script

  1. dumps all 8 programs of every nonce with build/progdump (static features
     and dynamic counters; hashes cross-checked against the timed VM),
  2. measures in C what deriving program 0 and its features costs per nonce,
  3. times every hash: one fast-mode JIT VM, perf_counter_ns around the call,
     r shuffled passes, median per nonce, samples kept only from the fast CPU
     cluster (macOS has no affinity; the CPU is read after every hash),
  4. fits out-of-fold predictors and computes the net hashrate change from
     skipping the predicted-slowest q% after paying for the features. Features
     of all 8 programs, dynamic counters and a repeat-timing oracle bound what
     program 0 could explain at best.

    python3 experiments/nonce_cost.py          # smoke: K=200, r=1, v1 -> data/cache/
    python3 experiments/nonce_cost.py --full   # K=20000, r=3, v1+v2 -> results/, figures/

Only --full on an otherwise idle machine produces results.
"""
import argparse
import ctypes
import gc
import json
import sys
import time
import warnings
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rxlab import plotstyle, sysinfo  # noqa: E402
from rxlab.analysis import programs as P  # noqa: E402
from rxlab.binding import VM, Cache, Dataset, Flags, recommended_flags  # noqa: E402
from rxlab.spec import params  # noqa: E402

Q_GRID = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5]
STATIC = [c for c in P.STATIC_COLUMNS if c != "n_NOP"]
DYNAMIC = ["executed", "taken", "fprc_writes", "fprc_changes"]
FAST_CPU_SLACK = 1.15  # a CPU is "fast" if its median hash time is within 15% of the best
BENCH_NONCES = 5000
NULL_DRAWS = 200
SEED = 20260926

_libc = ctypes.CDLL(None)


def prefer_performance_cores():
    """Ask macOS to schedule this thread as user-interactive (P-cores first)."""
    if hasattr(_libc, "pthread_set_qos_class_self_np"):
        _libc.pthread_set_qos_class_self_np(0x21, 0)


def cpu_number():
    if not hasattr(_libc, "pthread_cpu_number_np"):
        return -1
    n = ctypes.c_size_t()
    _libc.pthread_cpu_number_np(ctypes.byref(n))
    return n.value


def time_hashes(v2, nonces, repeats, threads, seed):
    """(repeats, K) ns per hash, (repeats, K) CPU after each hash, and the hashes."""
    base = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    inputs = [P.blob_with_nonce(n) for n in nonces]
    rng = np.random.default_rng(seed)
    ns = np.zeros((repeats, len(inputs)), dtype=np.int64)
    cpu = np.full((repeats, len(inputs)), -1, dtype=np.int64)
    with Cache(P.MONERO_KEY, flags=base) as cache, Dataset(cache, threads=threads) as ds:
        with VM(base | Flags.FULL_MEM | (Flags.V2 if v2 else 0), dataset=ds) as vm:
            prefer_performance_cores()
            hashes = [vm.hash(x).hex() for x in inputs]  # also the warm-up pass
            clock = time.perf_counter_ns
            gc.disable()
            try:
                for rep in range(repeats):
                    for i in rng.permutation(len(inputs)):
                        x = inputs[i]
                        t0 = clock()
                        vm.hash(x)
                        ns[rep, i] = clock() - t0
                        cpu[rep, i] = cpu_number()
            finally:
                gc.enable()
    return ns, cpu, hashes


def fast_cpus(ns, cpu):
    """CPUs whose median hash time is within FAST_CPU_SLACK of the fastest CPU."""
    ids, counts = np.unique(cpu, return_counts=True)
    med = {int(c): float(np.median(ns[cpu == c])) for c in ids}
    common = [int(c) for c, k in zip(ids, counts) if k >= 0.01 * cpu.size]
    best = min(med[c] for c in common)
    fast = [c for c in med if med[c] <= FAST_CPU_SLACK * best]
    table = {str(int(c)): {"samples": int(k), "median_us": med[int(c)] / 1e3}
             for c, k in zip(ids, counts)}
    return fast, table


def masked_median(ns, mask):
    x = np.where(mask, ns.astype(float), np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # nonces with no fast-CPU sample
        return np.nanmedian(x, axis=0)


def _degenerate(a, b):
    return len(a) < 3 or np.ptp(a) == 0 or np.ptp(b) == 0


def spearman(a, b):
    """Rank correlation, or None for < 3 pairs or a constant side (e.g. a flat model)."""
    return None if _degenerate(a, b) else float(stats.spearmanr(a, b)[0])


def log_pearson(a, b):
    return None if _degenerate(a, b) else float(np.corrcoef(np.log(a), np.log(b))[0, 1])


def select_inputs(tab, keep):
    rows = keep[tab["input"] - tab["input"].min()]
    return {k: v[rows] if isinstance(v, np.ndarray) else [x for x, r in zip(v, rows) if r]
            for k, v in tab.items()}


def predictors(tab, y):
    """Scores to rank nonces by (ridge fits on log time, out of fold), and whether a
    miner could compute them before hashing."""
    col = lambda c: P.per_input(tab, c)
    x0 = np.column_stack([col(c)[:, 0] for c in STATIC])
    xall = np.column_stack([col(c).sum(1) for c in STATIC])
    xdyn = np.column_stack([xall] + [col(c).sum(1) for c in DYNAMIC])
    v2 = int(tab["size"][0]) == params.PROGRAM_SIZE_V2
    cost = P.cost_model(tab, v2)
    model = lambda k: P.per_input({"input": tab["input"], "prog": tab["prog"], "c": cost[k]}, "c")
    fit = lambda x: np.exp(P.cv_predict(x, np.log(y), seed=SEED))
    return {
        "program0_ridge": (fit(x0), True),
        "program0_model_compute": (model("compute")[:, 0], True),
        "all8_model_cycles": (model("cycles").sum(1), False),
        "all8_ridge": (fit(xall), False),
        "all8_dynamic_ridge": (fit(xdyn), False),
    }


def gains(times, score, charges):
    out = {}
    for name, (c_skip, c_keep) in charges.items():
        g = [P.skip_gain(times, score, q, c_skip, c_keep) for q in Q_GRID]
        best = int(np.argmax(g))
        out[name] = {"by_q": dict(zip(map(str, Q_GRID), g)), "best_q": Q_GRID[best],
                     "best_gain": g[best]}
    return out


def null_gains(times, charges):
    """Gains from skipping by random scores: the noise band a predictor must beat.
    The best-over-q statistic is taken per draw, like a predictor's best_gain."""
    rng = np.random.default_rng(SEED)
    draws = {name: [] for name in charges}
    for _ in range(NULL_DRAWS):
        score = rng.random(len(times))
        for name, (c_skip, c_keep) in charges.items():
            draws[name].append([P.skip_gain(times, score, q, c_skip, c_keep) for q in Q_GRID])
    out = {}
    for name, d in draws.items():
        d = np.array(d)
        out[name] = {"best_gain_p95": float(np.percentile(d.max(1), 95)),
                     "band_by_q": {str(q): [float(np.percentile(d[:, j], 2.5)),
                                            float(np.percentile(d[:, j], 97.5))]
                                   for j, q in enumerate(Q_GRID)}}
    return out


def analyse(tab, ns, cpu, bench):
    fast, cpu_table = fast_cpus(ns, cpu)
    mask = np.isin(cpu, fast) | (cpu < 0)
    y = masked_median(ns, mask)
    ok = ~np.isnan(y)
    sub = select_inputs(tab, ok)
    y = y[ok]
    charges = {
        "free": (0.0, 0.0),
        "optimistic": (bench["ns_prefix_and_features"],
                       bench["ns_prefix_and_features"] - bench["ns_prefix"]),
        "conservative": (bench["ns_prefix_and_features"], bench["ns_prefix_and_features"]),
    }
    null = null_gains(y, charges)
    res = {"nonces_used": int(ok.sum()), "fast_cpus": sorted(fast), "cpu_table": cpu_table,
           "hash_time_us": P.describe(y / 1e3),
           "pass_median_us": [float(np.median(ns[r][mask[r]]) / 1e3) for r in range(len(ns))],
           "prefix_cost_ns": bench,
           "prefix_cost_share_of_mean_hash": bench["ns_prefix_and_features"] / float(y.mean()),
           "charges_ns": {k: {"skipped": c[0], "kept_extra": c[1]} for k, c in charges.items()},
           "null_random_skip": null}
    if len(ns) >= 2:
        both = mask[0] & mask[1]
        res["repeat_spearman"] = spearman(ns[0][both], ns[1][both])
        res["repeat_pearson_log"] = log_pearson(ns[0][both], ns[1][both])
        held = masked_median(ns[1:], mask[1:])[ok]
        first = np.where(mask[0], ns[0].astype(float), np.nan)[ok]
        good = ~np.isnan(held) & ~np.isnan(first)
        if good.sum() >= 3:  # a busy machine can leave almost no fast-CPU pairs
            res["oracle_repeat"] = {
                "spearman": spearman(first[good], held[good]),
                "gain": gains(held[good], first[good], {"free": (0.0, 0.0)})["free"],
                "note": "rank by timing pass 1, evaluate on the median of the other passes"}
    res["oracle_in_sample"] = gains(y, y, {"free": (0.0, 0.0)})["free"]
    res["predictors"] = {}
    scores = predictors(sub, y)
    for name, (score, achievable) in scores.items():
        entry = {"achievable_before_hashing": achievable, "spearman": spearman(score, y)}
        if name.endswith("ridge"):
            entry["r2_log_out_of_fold"] = P.r2(np.log(y), np.log(score))
        entry["gain"] = gains(y, score, charges if achievable else {"free": charges["free"]})
        for charge, g in entry["gain"].items():
            g["beats_random_p95"] = g["best_gain"] > null[charge]["best_gain_p95"]
        res["predictors"][name] = entry
    return res, y, {k: v[0] for k, v in scores.items()}


def figures(runs, prefix):
    plt = plotstyle.apply()
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    for v, (res, y, ns, _) in runs.items():
        rel = y / np.median(y)
        lo, hi = np.percentile(rel, [0.1, 99.9])
        axes[0].hist(rel, bins=np.linspace(lo, hi, 100), density=True, alpha=0.55,
                     label=f"{v}: median {np.median(y) / 1e3:.0f} us")
        if len(ns) >= 2:
            rho = res["repeat_spearman"]
            axes[1].loglog(ns[0] / 1e3, ns[1] / 1e3, ".", ms=1.5, alpha=0.4,
                           label=f"{v}: Spearman " + ("n/a" if rho is None else f"{rho:.3f}"))
    axes[0].set_xlabel("per-nonce hash time / median (0.1-99.9 percentile range)")
    axes[0].set_ylabel("density")
    axes[0].legend()
    axes[1].set_xlabel("pass 1 hash time (us)")
    axes[1].set_ylabel("pass 2 hash time (us)")
    axes[1].set_title("Is a nonce's hash time persistent?")
    if any(len(r[2]) >= 2 for r in runs.values()):
        axes[1].legend(markerscale=6)
    fig.savefig(prefix + "times.png")
    plt.close(fig)

    fig, axes = plt.subplots(1, len(runs), figsize=(5.2 * len(runs), 3.8), squeeze=False)
    qs = np.array(Q_GRID) * 100
    for ax, (v, (res, _, _, _)) in zip(axes[0], runs.items()):
        p0 = res["predictors"]["program0_ridge"]["gain"]
        for charge, style in (("free", ":"), ("optimistic", "-"), ("conservative", "--")):
            ax.plot(qs, [100 * g for g in p0[charge]["by_q"].values()], style, color="#3366aa",
                    label=f"program 0, {charge} cost")
        bound = res["predictors"]["all8_dynamic_ridge"]["gain"]["free"]["by_q"]
        ax.plot(qs, [100 * g for g in bound.values()], color="#dd7733",
                label="all 8 programs + dynamic (bound, free)")
        if "oracle_repeat" in res:
            ax.plot(qs, [100 * g for g in res["oracle_repeat"]["gain"]["by_q"].values()],
                    color="#339966", label="repeat-timing oracle (bound, free)")
        band = np.array(list(res["null_random_skip"]["free"]["band_by_q"].values())) * 100
        ax.fill_between(qs, band[:, 0], band[:, 1], color="#888888", alpha=0.25,
                        label="random skipping, 95% band (free)")
        ax.axhline(0, color="k", lw=0.8)
        ax.set_xscale("log")
        ax.set_xlabel("nonces skipped (%)")
        ax.set_title(v)
    axes[0][0].set_ylabel("net hashrate change (%)")
    axes[0][0].legend(fontsize=7)
    fig.savefig(prefix + "gain.png")
    plt.close(fig)

    fig, axes = plt.subplots(1, len(runs), figsize=(5.2 * len(runs), 3.8), squeeze=False)
    for ax, (v, (res, y, _, scores)) in zip(axes[0], runs.items()):
        for name, color in (("program0_ridge", "#3366aa"), ("all8_dynamic_ridge", "#dd7733")):
            r2v = res["predictors"][name]["r2_log_out_of_fold"]
            ax.plot(scores[name] / 1e3, y / 1e3, ".", ms=1.5, alpha=0.4, color=color,
                    label=f"{name} (R2 of log time {r2v:.3f})")
        ax.set_xlabel("predicted hash time (us, out of fold)")
        ax.set_title(v)
        ax.legend(fontsize=7, markerscale=6)
    axes[0][0].set_ylabel("measured hash time (us)")
    fig.savefig(prefix + "pred.png")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--full", action="store_true", help="K=20000, r=3, v1+v2 -> results/")
    ap.add_argument("--k", type=int)
    ap.add_argument("--repeats", type=int)
    ap.add_argument("--versions", default=None, help="comma list of v1,v2")
    ap.add_argument("--threads", type=int, default=4, help="dataset init / progdump threads")
    args = ap.parse_args()
    k = args.k or (20000 if args.full else 200)
    repeats = args.repeats or (3 if args.full else 1)
    versions = (args.versions or ("v1,v2" if args.full else "v1")).split(",")
    if args.full:
        out_json, prefix = ROOT / "results" / "nonce_cost.json", str(ROOT / "figures" / "nonce_cost_")
    else:
        out_json, prefix = P.CACHE_DIR / "nonce_cost_smoke.json", str(P.CACHE_DIR / "nonce_cost_smoke_")
    P.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    out = {"machine": sysinfo.describe(),
           "setup": {"k": k, "repeats": repeats, "key": P.MONERO_KEY.decode(),
                     "blob": P.MONERO_BLOB.hex(), "nonce_offset": P.NONCE_OFFSET,
                     "nonces": [0, k - 1], "timing": "one fast-mode JIT VM (hard AES, secure "
                     "W^X), perf_counter_ns around randomx_calculate_hash via ctypes",
                     "q_grid": Q_GRID, "folds": 5, "seed": SEED},
           "model": P.model_description()}
    runs = {}
    for v in versions:
        v2 = v == "v2"
        t0 = time.time()
        tab = P.dump(v2, start=0, count=k, threads=args.threads)
        bench = P.bench_prefix(v2, min(k, BENCH_NONCES))
        ns, cpu, hashes = time_hashes(v2, range(k), repeats, args.threads, SEED)
        matched = sum(h == tab["hash"][i] for i, h in zip(np.flatnonzero(tab["prog"] == 7), hashes))
        res, y, scores = analyse(tab, ns, cpu, bench)
        res["hash_check_vs_progdump"] = {"checked": len(hashes), "matched": int(matched)}
        out[v] = res
        runs[v] = (res, y, ns, scores)
        print(f"{v}: {time.time() - t0:.0f} s, hashes matched {matched}/{len(hashes)}, "
              f"mean {res['hash_time_us']['mean']:.0f} us", flush=True)
        out_json.write_text(json.dumps(out, indent=1) + "\n")  # per version: a later failure keeps it
    print("wrote", out_json.relative_to(ROOT))
    figures(runs, prefix)


if __name__ == "__main__":
    main()
