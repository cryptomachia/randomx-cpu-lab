"""Mode matrix: what each RandomX implementation choice is worth (v1).

Full factorial of fast/light x JIT/interpreter x hard/soft AES x batch/single at
1 thread and at the best thread count from bench_thread_scaling (all logical
CPUs if that result is missing). Each factor's effect is reported as the ratio
to the reference configuration (fast, JIT, hard AES, batch) with one factor
flipped, with a bootstrap CI and a Mann-Whitney test. --smoke runs the light-mode
reference and its three one-factor flips at 1 thread.

    python3 experiments/bench_mode_matrix.py [--smoke] [--estimate]
"""
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle, stats  # noqa: E402

NAME = "bench_mode_matrix"
FULL = {"trials": 3, "warmup": 1.5, "measure": 4.0}
SMOKE = {"trials": 1, "warmup": 0.3, "measure": 1.0}
FACTORS = {"mode": ("fast", "light"), "jit": (1, 0), "aes": ("hard", "soft"), "batch": (1, 0)}


def configs_for(threads, smoke):
    if smoke:
        combos = [("light", 1, "hard", 1), ("light", 0, "hard", 1), ("light", 1, "soft", 1), ("light", 1, "hard", 0)]
    else:
        combos = list(itertools.product(*FACTORS.values()))
    return [bench.spec(v=1, mode=m, jit=j, aes=a, batch=b, threads=t) for t in threads for m, j, a, b in combos]


def label(row):
    return (f"{row['mode']} · {'JIT' if row['jit'] else 'interp'} · {row['aes']} AES · "
            f"{'batch' if row['batch'] else 'single'}")


def effects(trials, rows, threads):
    """One-factor-flipped ratios against the reference config at each thread count
    (fast, JIT, hard AES, batch; light-mode reference if no fast configs ran)."""
    by = {}
    for t in trials:
        by.setdefault(t["config"], []).append(t["hs"])
    want = {k: v[0] for k, v in FACTORS.items()}
    if not any(r["mode"] == "fast" for r in rows):
        want["mode"] = "light"
    out = []
    for n in threads:
        ref = next((r for r in rows if r["threads"] == n and all(r[k] == v for k, v in want.items())), None)
        if ref is None:
            continue
        for k, values in FACTORS.items():
            alt = next(v for v in values if v != ref[k])
            flip = next((r for r in rows if r["threads"] == n and r[k] == alt
                         and all(r[o] == ref[o] for o in FACTORS if o != k)), None)
            if flip:
                out.append({"threads": n, "factor": k, "reference": ref["config"], "flipped": flip["config"],
                            "flipped_over_reference": stats.compare(by[ref["config"]], by[flip["config"]])})
    return out


def plot(rows, threads):
    plt = plotstyle.apply()
    fig, axes = plt.subplots(1, len(threads), figsize=(5.2 * len(threads), 0.32 * len(rows) / len(threads) + 1.6),
                             squeeze=False)
    modes = sorted({r["mode"] for r in rows}, key=["fast", "light"].index)
    for ax, n in zip(axes[0], threads):
        mine = sorted((r for r in rows if r["threads"] == n), key=lambda r: r["hs"]["median"])
        for i, m in enumerate(modes):
            pts = [(y, r) for y, r in enumerate(mine) if r["mode"] == m]
            med = [r["hs"]["median"] for _, r in pts]
            err = [[x - r["hs"]["ci95"][0] for x, (_, r) in zip(med, pts)],
                   [r["hs"]["ci95"][1] - x for x, (_, r) in zip(med, pts)]]
            ax.errorbar(med, [y for y, _ in pts], xerr=err, fmt="o", ms=6, capsize=2, lw=1,
                        color=plotstyle.PALETTE[i], label=f"{m} mode")
        ax.set_yticks(range(len(mine)))
        ax.set_yticklabels([label(r) for r in mine], fontsize=8)
        ax.set_xscale("log")
        ax.set_xlabel("H/s (median, 95% CI; log scale)")
        ax.set_title(f"RandomX v1, {n} thread{'s' if n > 1 else ''}")
    if len(modes) > 1:
        axes[0][-1].legend(frameon=False, loc="upper left")
    fig.tight_layout()
    return fig


def main():
    args = bench.experiment_args(__doc__).parse_args()
    cfg = SMOKE if args.smoke else FULL
    threads = [1] if args.smoke else sorted({1, bench.best_threads(v=1)})
    configs = configs_for(threads, args.smoke)
    if args.estimate:
        print(round(bench.estimate_sweep(len(configs), cfg["trials"], cfg["warmup"], cfg["measure"])))
        return
    trials, meta = bench.measured_sweep(configs, args.tag, args.smoke, cfg["warmup"], cfg["measure"],
                                        cfg["trials"], label=NAME)
    rows = bench.summarize_trials(trials, bench.cpu_clusters())
    eff = effects(trials, rows, threads)
    for e in eff:
        c = e["flipped_over_reference"]
        print(f"{e['threads']:>2} threads, {e['factor']:>5} flipped: x{c['ratio']:.3f} "
              f"[{c['ci95'][0]:.3f}, {c['ci95'][1]:.3f}]")
    payload = dict(meta, threads=threads, summary=rows, effects=eff, trials=trials)
    bench.finish(NAME, payload, plot(rows, threads), args.smoke)


if __name__ == "__main__":
    main()
