"""Sustained hashing: burst vs steady state on a fanless MacBook Air.

Runs the best configuration (the bench_tune winner, else the best thread count
from bench_thread_scaling, else all logical CPUs) continuously for 20 minutes,
sampling aggregate H/s, the share of hashes finishing on P-cores and the macOS
thermal state every 10 s. Reports the burst rate (first 90 s), the sustained
rate (last 5 min) and the haircut between them.

xmrig --bench=N scores a fixed amount of work: N hashes (250K, 500K, 1M..10M)
divided by the time from worker start until the last worker finishes
(xmrig src/base/net/stratum/benchmark/BenchClient.cpp, onBenchDone). Its clock
starts when the CPU backend launches the worker threads, which happens only
once the dataset is ready (Miner.cpp onDatasetReady -> Workers.cpp start), so
dataset init is excluded; worker VM creation is inside xmrig's clock but before
t = 0 here. The same score is emulated here from the cumulative hash trace,
which shows how much of a published xmrig number is burst. --smoke runs 20 s in
light mode.

    python3 experiments/bench_sustained.py [--smoke] [--estimate] [--minutes M]
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle, stats, sysinfo  # noqa: E402

NAME = "bench_sustained"
FULL = {"duration": 1200.0, "interval": 10.0, "burst": 90.0, "tail": 300.0,
        "xmrig_sizes": [250_000, 500_000, 1_000_000, 10_000_000]}
SMOKE = {"duration": 20.0, "interval": 2.0, "burst": 6.0, "tail": 6.0, "xmrig_sizes": [200, 500]}


def pick_config(smoke):
    if smoke:
        return bench.spec(v=1, mode="light", threads=2), "smoke default"
    tuned = bench.load_result("bench_tune")
    if tuned and not tuned.get("smoke"):
        return tuned["hashing"]["winner"], "bench_tune winner"
    scaled = bench.load_result("bench_thread_scaling")
    if scaled and not scaled.get("smoke"):
        return bench.spec(v=1, threads=bench.best_threads(v=1)), "bench_thread_scaling best (v1)"
    return bench.spec(v=1, threads=sysinfo.describe()["logical_cpus"]), "all logical CPUs (no prior results)"


def size_label(n):
    """xmrig spelling of a benchmark size: 250K, 1M, ..."""
    return f"{n // 1_000_000}M" if n >= 1_000_000 else f"{n // 1000}K" if n >= 1000 else str(n)


def fixed_work_score(t_end, hashes, n):
    """xmrig-style score: n / time until the cumulative count reaches n (None if never)."""
    cum = np.cumsum(hashes)
    if cum[-1] < n:
        return None
    i = int(np.searchsorted(cum, n))
    t0, c0 = (t_end[i - 1], cum[i - 1]) if i else (0.0, 0.0)
    t = t0 + (n - c0) / (cum[i] - c0) * (t_end[i] - t0)
    return {"hashes": n, "seconds": float(t), "hs": float(n / t)}


def analyse(samples, cfg, clusters):
    t = np.array([s["t"] for s in samples])
    dt = np.array([s["dt"] for s in samples])
    hashes = np.array([sum(s["hashes"]) for s in samples], dtype=float)
    burst = stats.window_mean(t, hashes / dt, 0, cfg["burst"])
    tail = stats.window_mean(t, hashes / dt, t[-1] - cfg["tail"], t[-1])
    p_share = [bench.cluster_share(s["cpus"], clusters).get("P") for s in samples]
    return {
        "duration_s": float(t[-1]), "total_hashes": int(hashes.sum()), "mean_hs": float(hashes.sum() / t[-1]),
        "burst_window_s": cfg["burst"], "burst_hs": burst,
        "sustained_window_s": cfg["tail"], "sustained_hs": tail,
        "haircut": 1 - tail / burst,
        "min_sample_hs": float(np.min(hashes / dt)), "max_sample_hs": float(np.max(hashes / dt)),
        "sample_hs_summary": stats.summarize(hashes / dt),
        "thermal_states_seen": sorted({s["thermal"] for s in samples}),
        "first_non_nominal_thermal_t": next((s["t"] for s in samples if s["thermal"] > 0), None),
        "samples_with_paging": sum(bench.paged(s) for s in samples),
        "samples_with_decompressions": sum(bench.decompressed(s) for s in samples),
        "p_core_share": p_share,
        "xmrig_fixed_work": [fixed_work_score(t, hashes, n) for n in cfg["xmrig_sizes"]],
    }


def plot(samples, analysis, config):
    plt = plotstyle.apply()
    fig, (ax, ax2, ax3) = plt.subplots(3, 1, figsize=(9, 7), sharex=True,
                                       gridspec_kw={"height_ratios": [3, 1.2, 1]})
    t = [s["t"] for s in samples]
    mid = [s["t"] - s["dt"] / 2 for s in samples]
    hs = [s["hs"] for s in samples]
    blue, grey = plotstyle.PALETTE[0], "#888888"
    ax.axvspan(0, analysis["burst_window_s"], color=blue, alpha=0.10, lw=0)
    ax.axvspan(t[-1] - analysis["sustained_window_s"], t[-1], color=grey, alpha=0.15, lw=0)
    ax.plot(mid, hs, marker="o", ms=3, lw=2, color=blue)
    ax.axhline(analysis["burst_hs"], color=blue, lw=1)
    ax.axhline(analysis["sustained_hs"], color=grey, lw=1)
    ax.text(t[-1], analysis["burst_hs"], f"burst {analysis['burst_hs']:.0f} H/s ", ha="right", va="bottom",
            fontsize=8)
    ax.text(t[-1], analysis["sustained_hs"], f"sustained {analysis['sustained_hs']:.0f} H/s "
            f"(haircut {100 * analysis['haircut']:.1f}%) ", ha="right", va="top", fontsize=8)
    for score in analysis["xmrig_fixed_work"]:
        if score:
            ax.axvline(score["seconds"], color=grey, lw=1)
            ax.text(score["seconds"], ax.get_ylim()[0], f" --bench={size_label(score['hashes'])}\n"
                    f" {score['hs']:.0f} H/s",
                    fontsize=7, va="bottom", color="#555555")
    ax.set_ylabel(f"H/s ({samples[0]['dt']:.0f} s samples)")
    ax.set_title(f"Sustained run: {config}", fontsize=9)
    ax2.plot(t, [100 * (x or 0) for x in analysis["p_core_share"]], lw=2, color=blue)
    ax2.set_ylabel("% hashes\non P-cores")
    ax2.set_ylim(0, 105)
    ax3.step(t, [s["thermal"] for s in samples], where="post", lw=2, color=blue)
    ax3.set_yticks([0, 1, 2, 3])
    ax3.set_yticklabels(["nominal", "fair", "serious", "critical"], fontsize=8)
    ax3.set_ylim(-0.3, 3.3)
    ax3.set_ylabel("thermal\nstate")
    ax3.set_xlabel("seconds since hashing started")
    fig.tight_layout()
    return fig


def main():
    ap = bench.experiment_args(__doc__)
    ap.add_argument("--minutes", type=float, help="override the run length")
    args = ap.parse_args()
    cfg = dict(SMOKE if args.smoke else FULL)
    if args.minutes:
        cfg["duration"] = 60 * args.minutes
    if args.estimate:
        print(round(cfg["duration"] + bench.SESSION_OVERHEAD_S))
        return
    config, source = pick_config(args.smoke)
    print(f"[{NAME}] {config} ({source}) for {cfg['duration']:.0f} s", file=sys.stderr)
    with bench.Session(args.tag, init_threads=bench.SMOKE_INIT_THREADS if args.smoke else None) as s:
        verifier = bench.Verifier(args.tag, info=s.info())
        samples, end = bench.sample(s, config, cfg["duration"], cfg["interval"], label=NAME)
        setup = s.setup
    verification = verifier.verify([end])
    verifier.close()
    analysis = analyse(samples, cfg, bench.cpu_clusters())
    print(f"burst {analysis['burst_hs']:.1f} H/s, sustained {analysis['sustained_hs']:.1f} H/s, "
          f"haircut {100 * analysis['haircut']:.2f}%")
    payload = {"tag": args.tag, "smoke": args.smoke, "config": config, "config_source": source,
               "settings": cfg, "setup": setup, "verification": verification, "analysis": analysis,
               "power_source_at_end": sysinfo.describe().get("power_source"), "samples": samples, "end": end}
    bench.finish(NAME, payload, plot(samples, analysis, config), args.smoke)


if __name__ == "__main__":
    main()
