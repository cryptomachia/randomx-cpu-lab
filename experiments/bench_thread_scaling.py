"""Thread scaling: fast-mode JIT hashrate at 1..12 threads for RandomX v1 and v2.

All configurations run interleaved (trial-major) in one rxbench session, so
thermal drift is spread across thread counts instead of biasing the later ones.
QoS is left unset: this is what a plain miner gets from the macOS scheduler.
--smoke uses light mode and 1-2 threads.

    python3 experiments/bench_thread_scaling.py [--smoke] [--estimate]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle  # noqa: E402

NAME = "bench_thread_scaling"
FULL = {"threads": list(range(1, 13)), "trials": 3, "warmup": 2.0, "measure": 5.0}
SMOKE = {"threads": [1, 2], "trials": 1, "warmup": 0.3, "measure": 1.0}
VERSIONS = (1, 2)


def analyse(rows):
    best, curves = {}, {}
    for v in VERSIONS:
        mine = [r for r in rows if r["v"] == v]
        top = max(mine, key=lambda r: r["hs"]["median"])
        one = next((r for r in mine if r["threads"] == 1), None)
        best[f"v{v}"] = {"threads": top["threads"], "hs_median": top["hs"]["median"], "ci95": top["hs"]["ci95"],
                         "speedup_vs_1_thread": top["hs"]["median"] / one["hs"]["median"] if one else None}
        curves[f"v{v}"] = [{"threads": r["threads"], "hs_median": r["hs"]["median"], "ci95": r["hs"]["ci95"],
                            "hs_per_thread": r["hs"]["median"] / r["threads"]} for r in mine]
    return {"best": best, "curves": curves}


def plot(analysis, clusters, mode):
    plt = plotstyle.apply()
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10, 4.2))
    for i, v in enumerate(VERSIONS):
        c = analysis["curves"][f"v{v}"]
        t = [x["threads"] for x in c]
        med = [x["hs_median"] for x in c]
        err = [[m - x["ci95"][0] for m, x in zip(med, c)], [x["ci95"][1] - m for m, x in zip(med, c)]]
        color = plotstyle.PALETTE[i]
        ax.errorbar(t, med, yerr=err, marker="o", ms=5, lw=2, capsize=3, color=color, label=f"RandomX v{v}")
        ax2.plot(t, [x["hs_per_thread"] for x in c], marker="o", ms=5, lw=2, color=color, label=f"RandomX v{v}")
    p = sum(1 for k in clusters.values() if k == "P")
    for a in (ax, ax2):
        if p:
            a.axvline(p, color="#888888", lw=1)
            a.axvline(len(clusters), color="#888888", lw=1)
        a.set_xlabel("hashing threads (QoS unset)")
        a.set_xticks(range(1, max(t) + 1))
    if p:
        ax.text(p, ax.get_ylim()[1], " P-cores", va="top", fontsize=8, color="#555555")
        ax.text(len(clusters), ax.get_ylim()[1], " all CPUs", va="top", fontsize=8, color="#555555")
    ax.set_ylabel("H/s (median, 95% bootstrap CI)")
    ax.set_title(f"Total hashrate, {mode} mode, JIT")
    ax2.set_ylabel("H/s per thread")
    ax2.set_title("Per-thread rate")
    ax.set_ylim(bottom=0)
    ax2.set_ylim(bottom=0)
    ax.legend(frameon=False)
    return fig


def main():
    args = bench.experiment_args(__doc__).parse_args()
    cfg = SMOKE if args.smoke else FULL
    mode = bench.smoke_mode(args.smoke)
    configs = [bench.spec(v=v, mode=mode, threads=t) for t in cfg["threads"] for v in VERSIONS]
    if args.estimate:
        print(round(bench.estimate_sweep(len(configs), cfg["trials"], cfg["warmup"], cfg["measure"])))
        return
    trials, meta = bench.measured_sweep(configs, args.tag, args.smoke, cfg["warmup"], cfg["measure"],
                                        cfg["trials"], label=NAME)
    clusters = bench.cpu_clusters()
    rows = bench.summarize_trials(trials, clusters)
    analysis = analyse(rows)
    for v, b in analysis["best"].items():
        print(f"{v}: best {b['threads']} threads, {b['hs_median']:.1f} H/s")
    bench.finish(NAME, dict(meta, summary=rows, analysis=analysis, trials=trials), plot(analysis, clusters, mode),
                 args.smoke)


if __name__ == "__main__":
    main()
