"""QoS placement: P-cores only vs E-cores only vs mixed, with per-thread rates.

macOS offers no hard affinity; per-thread QoS is the only lever. Background QoS
keeps a thread on the E-cluster, user-interactive prefers the P-cluster. rxbench
samples the CPU each hash finished on, so every thread's actual cluster
residency is reported next to its rate. The E-cluster curve (1..E threads)
tests whether the E-cluster's shared L2 limits how many 2 MiB scratchpads it can
serve; the mixes test whether P and E throughput add. --smoke runs a 2-thread
light-mode version.

    python3 experiments/bench_qos_placement.py [--smoke] [--estimate]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle, stats, tune  # noqa: E402

NAME = "bench_qos_placement"
FULL = {"trials": 3, "warmup": 1.5, "measure": 5.0}
SMOKE = {"trials": 1, "warmup": 0.3, "measure": 1.0}
CLUSTER_COLOR = {"P": plotstyle.PALETTE[0], "E": plotstyle.PALETTE[1]}


def layouts(n, p, e):
    """(label, threads, qos) for every placement tested."""
    out = [(f"P x{k}", k, "ui") for k in range(1, p + 1)]
    out += [(f"E x{k}", k, "bg") for k in range(1, e + 1)]
    mixes = sorted({min(2, e), e} - {0})
    out += [(f"P x{p} + E x{k}", p + k, bench.qos_mix(("ui", p), "bg")) for k in mixes]
    out += [(f"P x{p} + utility x{e}", n, bench.qos_mix(("ui", p), "ut")),
            (f"all ui x{n}", n, "ui"), (f"unset x{n}", n, "unset"), (f"unset x{p}", p, "unset")]
    return out


def dominant(share):
    return max(share, key=share.get) if share else "?"


def analyse(rows, labels, p, e):
    by_label = {labels[r["config"]]: r for r in rows}
    curves = {}
    for kind, prefix, count in (("P", "P x", p), ("E", "E x", e)):
        curves[kind] = [{"threads": k, "hs_median": by_label[f"{prefix}{k}"]["hs"]["median"],
                         "ci95": by_label[f"{prefix}{k}"]["hs"]["ci95"]} for k in range(1, count + 1)]
    thread_rates = {"P": [], "E": []}
    for r in rows:
        for hs, share in zip(r["thread_hs_median"], r.get("thread_cluster_share", [])):
            if share.get("P", 0) >= 0.9:
                thread_rates["P"].append(hs)
            elif share.get("E", 0) >= 0.9:
                thread_rates["E"].append(hs)
    out = {"cluster_curves": curves,
           "thread_rate_by_cluster": {k: stats.summarize(v) for k, v in thread_rates.items() if v}}
    best_p = max(curves["P"], key=lambda c: c["hs_median"])
    best_e = max(curves["E"], key=lambda c: c["hs_median"])
    mix = by_label.get(f"P x{p} + E x{e}")
    out["best_p_only"], out["best_e_only"] = best_p, best_e
    if mix:
        out["additivity"] = {
            "mixed_hs_median": mix["hs"]["median"],
            "p_plus_e_standalone_hs": curves["P"][-1]["hs_median"] + curves["E"][-1]["hs_median"],
            "ratio": mix["hs"]["median"] / (curves["P"][-1]["hs_median"] + curves["E"][-1]["hs_median"]),
        }
    return out


def plot(rows, labels, analysis):
    plt = plotstyle.apply()
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(11, 4.4), gridspec_kw={"width_ratios": [1, 1.6]})
    for kind, curve in analysis["cluster_curves"].items():
        t = [c["threads"] for c in curve]
        med = [c["hs_median"] for c in curve]
        err = [[m - c["ci95"][0] for m, c in zip(med, curve)], [c["ci95"][1] - m for m, c in zip(med, curve)]]
        ax.errorbar(t, med, yerr=err, marker="o", ms=5, lw=2, capsize=3, color=CLUSTER_COLOR[kind],
                    label=f"{kind}-cores only ({'user_interactive' if kind == 'P' else 'background'} QoS)")
    ax.set_xticks(range(1, max(len(c) for c in analysis["cluster_curves"].values()) + 1))
    ax.set_xlabel("threads on one cluster")
    ax.set_ylabel("total H/s (median, 95% CI)")
    ax.set_title("Per-cluster scaling")
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, fontsize=8)
    order = sorted(rows, key=lambda r: r["hs"]["median"])
    for x, r in enumerate(order):
        for hs, share in zip(r["thread_hs_median"], r.get("thread_cluster_share", [])):
            kind = dominant(share)
            ax2.plot(x, hs, "o", ms=6, color=CLUSTER_COLOR.get(kind, "#888888"), alpha=0.8,
                     markeredgecolor="white", markeredgewidth=1)
    for kind, color in CLUSTER_COLOR.items():
        ax2.plot([], [], "o", color=color, label=f"thread ran mostly on {kind}-cores")
    ax2.set_xticks(range(len(order)))
    ax2.set_xticklabels([f"{labels[r['config']]}\n{r['hs']['median']:.0f} H/s" for r in order],
                        rotation=60, ha="right", fontsize=7)
    ax2.set_ylabel("per-thread H/s (median over trials)")
    ax2.set_title("Per-thread rates by placement (sorted by total H/s)")
    ax2.set_ylim(bottom=0)
    ax2.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    return fig


def main():
    args = bench.experiment_args(__doc__).parse_args()
    cfg = SMOKE if args.smoke else FULL
    n, p, e = (2, 1, 1) if args.smoke else tune.shape()
    mode = bench.smoke_mode(args.smoke)
    lay = layouts(n, p, e)
    configs = [bench.spec(v=1, mode=mode, threads=t, qos=q) for _, t, q in lay]
    labels = dict(zip(configs, (name for name, _, _ in lay)))
    if args.estimate:
        print(round(bench.estimate_sweep(len(configs), cfg["trials"], cfg["warmup"], cfg["measure"])))
        return
    trials, meta = bench.measured_sweep(configs, args.tag, args.smoke, cfg["warmup"], cfg["measure"],
                                        cfg["trials"], label=NAME)
    rows = bench.summarize_trials(trials, bench.cpu_clusters())
    for r in rows:
        r["label"] = labels[r["config"]]
    analysis = analyse(rows, labels, p, e)
    for r in sorted(rows, key=lambda r: -r["hs"]["median"]):
        print(f"{r['label']:>24}: {r['hs']['median']:8.1f} H/s")
    payload = dict(meta, shape={"logical_cpus": n, "p_cores": p, "e_cores": e}, summary=rows,
                   analysis=analysis, trials=trials)
    bench.finish(NAME, payload, plot(rows, labels, analysis), args.smoke)


if __name__ == "__main__":
    main()
