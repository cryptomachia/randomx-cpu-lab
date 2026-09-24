"""Which hardware properties set RandomX speed? A model of the XMRig benchmark database.

Reads data/leaderboard/rigs.csv, first rebuilding it from the raw-response
cache when one exists (--rebuild also crawls api.xmrig.com for anything missing),
forms the per-CPU table (median of rig medians), cross-validates the structural
model hashrate = r(uarch) x min(threads, L3 / 2 MiB) against log-linear
regressions, measures within-CPU rig effects (huge pages, MSR, memory), and
places the Apple M-series rows. Writes results/hardware_model.json and
figures/hw_*.png.
"""
import argparse
import json
import statistics
import sys
import warnings
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rxlab import plotstyle, sysinfo  # noqa: E402
from rxlab.analysis import leaderboard as lb  # noqa: E402

OUT = ROOT / "results" / "hardware_model.json"
FIG = ROOT / "figures"
K, SEED, REPEATS = 10, 0, 5
BOOT = 500
MIN_RIGS = 3
VENDOR_NAMES = {"amd": "AMD", "intel": "Intel", "hygon": "Hygon"}
X86 = ("amd", "intel", "hygon")

log = np.log
# numpy 2.0 + Apple Accelerate raises spurious FP flags in matmul; r2() rejects non-finite values
warnings.filterwarnings("ignore", message=".*encountered in matmul", category=RuntimeWarning)
FEATURES = {
    "log_threads_used": lambda r: log(r["threads_used"]),
    "log_cores": lambda r: log(r["cores"]),
    "log_l3": lambda r: log(r["l3_mib"]),
    "log_l2": lambda r: log(r["l2_mib"]),
}
SLOT_TERM = {"log_min_threads_l3slots": lambda r: log(min(r["threads_used"], lb.cache_slots(r)))}
MEMORY = {
    "log_mem_mts": lambda r: log(r["mem_mts"]),
    "log_channels": lambda r: log(r["channels"]),
}


def _round(x, nd=4):
    if isinstance(x, float):
        return float(f"{x:.{nd}g}")
    if isinstance(x, dict):
        return {k: _round(v, nd) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_round(v, nd) for v in x]
    return x


def _cv(name, make, rows, **kw):
    res = lb.cross_validate(make, rows, k=K, seed=SEED, repeats=REPEATS, **kw)
    res["name"], res["n"] = name, len(rows)
    return res


def compare_models(cpus):
    """Cross-validated R^2 of structural vs log-linear models on the x86 CPU table."""
    results = []
    for by_power in (False, True):
        for cache in lb.CACHE_MODES:
            for smt in (1.0, None):
                name = (f"structural[{'uarch x power_class' if by_power else 'uarch'}, {cache or 'nocache'}, "
                        f"{'smt=1' if smt else 'smt fitted'}]")
                res = _cv(name, lambda c=cache, s=smt, b=by_power: lb.StructuralModel(c, smt=s, by_power=b), cpus)
                res["cache"], res["smt"], res["by_power"] = cache, smt, by_power
                if smt is None:
                    res["smt_fitted_on_all_rows"] = lb.StructuralModel(cache, by_power=by_power).fit(cpus).smt
                results.append(res)
    results += [
        _cv("loglinear[threads]", lambda: lb.LogLinear({"log_threads_used": FEATURES["log_threads_used"]}), cpus),
        _cv("loglinear[threads,cores,l3,l2 + vendor]", lambda: lb.LogLinear(FEATURES, ["vendor"]), cpus),
        _cv("loglinear[threads,cores,l3,l2 + uarch]", lambda: lb.LogLinear(FEATURES, ["uarch"]), cpus),
        _cv("loglinear[min(threads,l3slots) + uarch]", lambda: lb.LogLinear(SLOT_TERM, ["uarch"]), cpus),
        _cv("loglinear[threads,cores,l3,l2,min(threads,l3slots) + uarch]",
            lambda: lb.LogLinear({**FEATURES, **SLOT_TERM}, ["uarch"]), cpus),
        _cv("loglinear[min(threads,l3slots) + uarch + power_class]",
            lambda: lb.LogLinear(SLOT_TERM, ["uarch", "power_class"]), cpus),
        _cv("loglinear[threads,cores,l3,l2,min(threads,l3slots) + uarch + power_class]",
            lambda: lb.LogLinear({**FEATURES, **SLOT_TERM}, ["uarch", "power_class"]), cpus),
    ]
    return results


def memory_models(cpus, best_make):
    """On CPUs whose rigs report DIMMs: does DRAM speed/channel count add anything,
    and which features carry the signal (add-one to threads-only, drop-one from full)?"""
    sub = [c for c in cpus if c["mem_mts"] and c["channels"] and c["mem_type"]]
    base = {**FEATURES, **SLOT_TERM}
    full = {**base, **MEMORY}
    cats = ["uarch", "vendor", "power_class", "mem_type"]
    out = {"n": len(sub), "models": [
        _cv("loglinear[specs + uarch]", lambda: lb.LogLinear(base, ["uarch"]), sub),
        _cv("loglinear[specs + memory + uarch, mem_type]", lambda: lb.LogLinear(full, ["uarch", "mem_type"]), sub),
        _cv("best structural", best_make, sub),
    ]}
    for m in out["models"]:
        m.pop("oof_log_pred")

    def score(feats, categories):
        res = lb.cross_validate(lambda: lb.LogLinear(feats, categories), sub, K, SEED, REPEATS)
        return res["r2_log_hashrate"], res["r2_log_per_thread"]

    threads = {"log_threads_used": full["log_threads_used"]}
    b_h, b_t = score(threads, [])
    f_h, f_t = score(full, cats)
    add, drop = {}, {}
    for name in [k for k in full if k != "log_threads_used"] + cats:
        feats = {**threads, name: full[name]} if name in full else threads
        h, t = score(feats, [name] if name in cats else [])
        add[name] = {"r2_log_hashrate_gain": h - b_h, "r2_log_per_thread_gain": t - b_t}
        h, t = score({k: v for k, v in full.items() if k != name}, [c for c in cats if c != name])
        drop[name] = {"r2_log_hashrate_loss": f_h - h, "r2_log_per_thread_loss": f_t - t}
    out["importance"] = {
        "threads_only": {"r2_log_hashrate": b_h, "r2_log_per_thread": b_t},
        "full": {"r2_log_hashrate": f_h, "r2_log_per_thread": f_t, "categories": cats},
        "add_one_to_threads_only": add,
        "drop_one_from_full": drop,
    }
    return out


class RelativeOLS:
    """OLS of log(rig hashrate / its CPU's median) on rig features; predicts log hashrate."""

    def __init__(self, feats, ref):
        self.feats, self.ref = feats, ref

    def design(self, rows):
        return np.column_stack([np.ones(len(rows))] + [[f(r) for r in rows] for f in self.feats.values()])

    def offset(self, rows):
        return log([self.ref[r["cpu"]]["hashrate"] for r in rows])

    def fit(self, rows):
        y = log([r["hashrate"] for r in rows]) - self.offset(rows)
        self.coef = np.linalg.lstsq(self.design(rows), y, rcond=None)[0]
        return self

    def predict(self, rows):
        return self.design(rows) @ self.coef + self.offset(rows)


def within_cpu(rigs, cpus, min_rigs=5):
    """Rig-to-rig variation inside one CPU model: huge pages, MSR, threads, memory, OS."""
    ref = {c["cpu"]: c for c in cpus if c["rigs"] >= min_rigs}
    rows = [r for r in rigs if r["cpu"] in ref and r["hugepages_frac"] is not None and r["msr"] is not None
            and (r["cores"], r["l3_mib"]) == (ref[r["cpu"]]["cores"], ref[r["cpu"]]["l3_mib"])]
    feats = {
        "hugepages_frac": lambda r: r["hugepages_frac"],
        "msr_on": lambda r: float(r["msr"]),
        "log_threads_rel": lambda r: log(r["threads_used"] / ref[r["cpu"]]["threads_used"]),
        "linux": lambda r: float(r["os"] == "linux"),
    }
    mem_feats = {
        "log_mem_mts_rel": lambda r: log(r["mem_mts"] / ref[r["cpu"]]["mem_mts"]),
        "log_channels_rel": lambda r: log(r["channels"] / ref[r["cpu"]]["channels"]),
    }
    with_mem = [r for r in rows if r["mem_mts"] and r["channels"]
                and ref[r["cpu"]]["mem_mts"] and ref[r["cpu"]]["channels"]]
    out = {"min_rigs_per_cpu": min_rigs}
    for tag, fs, sub in (("no_memory", feats, rows), ("with_memory", {**feats, **mem_feats}, with_mem)):
        model = RelativeOLS(fs, ref).fit(sub)
        groups = [r["cpu"] for r in sub]
        members = {c: [i for i, g in enumerate(groups) if g == c] for c in sorted(set(groups))}
        rng = np.random.default_rng(SEED)
        boots = []
        for _ in range(BOOT):
            pick = rng.choice(sorted(members), len(members))
            boots.append(RelativeOLS(fs, ref).fit([sub[i] for c in pick for i in members[c]]).coef)
        lo, hi = np.percentile(boots, [2.5, 97.5], axis=0)
        y = log([r["hashrate"] for r in sub]) - model.offset(sub)
        cv = lb.cross_validate(lambda: RelativeOLS(fs, ref), sub, K, SEED, REPEATS, groups=groups)
        out[tag] = {
            "n_rigs": len(sub), "n_cpus": len(members),
            "coef": {name: {"estimate": float(model.coef[i + 1]), "ci95": [float(lo[i + 1]), float(hi[i + 1])],
                            "pct_effect_of_unit_change": float(np.expm1(model.coef[i + 1]) * 100)}
                     for i, name in enumerate(fs)},
            "r2_in_sample": lb.r2(y, model.predict(sub) - model.offset(sub)),
            "r2_grouped_cv": lb.r2(y, cv["oof_log_pred"] - model.offset(sub)),
            "rig_over_cpu_median_quantiles": {q: float(np.exp(np.quantile(y, float(q))))
                                              for q in ("0.1", "0.25", "0.5", "0.75", "0.9")},
        }
    hp = {"full (>=0.99)": [], "none (0)": [], "partial": []}
    for r in rows:
        f = r["hugepages_frac"]
        hp["full (>=0.99)" if f >= 0.99 else "none (0)" if f == 0 else "partial"].append(
            r["hashrate"] / ref[r["cpu"]]["hashrate"])
    out["hugepages_rig_over_cpu_median"] = {
        k: {"n": len(v), "median": float(np.median(v)) if v else None} for k, v in hp.items()}
    dual = {1: [], 2: []}
    for r in with_mem:
        if ref[r["cpu"]]["channels"] == 2 and r["channels"] in dual and (r["hugepages_frac"] or 0) >= 0.99:
            dual[r["channels"]].append(r["hashrate"] / ref[r["cpu"]]["hashrate"])
    out["dual_channel_cpus_full_hugepages"] = {
        f"{k}_channel": {"n": len(v), "median_rig_over_cpu_median": float(np.median(v)) if v else None}
        for k, v in dual.items()}
    return out


WITHIN_LABELS = {
    "hugepages_frac": "huge pages (none -> all)",
    "msr_on": "XMRig MSR mod on",
    "log_threads_rel": "log(threads / CPU's usual)",
    "linux": "Linux (vs Windows/other)",
    "log_mem_mts_rel": "log(DRAM MT/s / CPU median)",
    "log_channels_rel": "log(channels / CPU median)",
}


def cache_saturation(rigs, cpus, min_rigs=5):
    """Within-CPU test of min(threads, L3 / 2 MiB): full-huge-page rigs of x86 CPUs with >= min_rigs rigs.

    expected = T_eff(rig) / T_eff(CPU's usual thread count), observed = rig hashrate / CPU
    median. A slope of 1 for log observed on log expected supports the hypothesis, 0 means
    the thread count does not matter. SMT threads count fully (smt=1, the literal
    hypothesis) or not at all (smt=0).
    """
    ref = {c["cpu"]: c for c in cpus if c["rigs"] >= min_rigs}
    pts = []
    for r in rigs:
        c = ref.get(r["cpu"])
        if not c or (r["cores"], r["l3_mib"]) != (c["cores"], c["l3_mib"]) or (r["hugepages_frac"] or 0) < 0.99:
            continue
        usual = dict(r, threads_used=c["threads_used"])
        pts.append({"cpu": r["cpu"], "over": r["threads_used"] / lb.cache_slots(r),
                    "observed": r["hashrate"] / c["hashrate"],
                    **{f"expected_smt{w:g}": lb.effective_threads(r, "l3", w) / lb.effective_threads(usual, "l3", w)
                       for w in (1.0, 0.0)}})
    out = {"n_rigs": len(pts), "n_cpus": len({p["cpu"] for p in pts})}
    under = [p for p in pts if p["over"] <= 1]
    for key in ("expected_smt1", "expected_smt0"):
        est, ci = _cluster_slope(under, key)
        out[key] = {"slope_threads_le_slots": est, "slope_ci95": ci,
                    "n_rigs_expected_not_1": sum(abs(p[key] - 1) > 0.05 for p in under)}
    over = [p for p in pts if p["over"] > 1.05]
    usual = [p["observed"] for p in under if abs(p["expected_smt1"] - 1) <= 0.05]
    out["baseline_median_observed_usual_threads"] = float(np.median(usual))
    out["threads_over_slots"] = {
        "n_rigs": len(over),
        "median_observed_over_expected_smt1": float(np.median([p["observed"] / p["expected_smt1"] for p in over]))
        if over else None}
    x86 = [r for r in rigs if r["vendor"] in X86 and r["l3_mib"]]
    out["share_rigs_threads_eq_min_logical_l3slots"] = float(np.mean(
        [r["threads_used"] == min(r["threads"], int(lb.cache_slots(r))) for r in x86]))
    out["share_rigs_threads_over_l3slots"] = float(np.mean([r["threads_used"] > lb.cache_slots(r) for r in x86]))
    return out, pts


def _cluster_slope(rows, key):
    """Slope of log observed on log expected, with a CPU-cluster bootstrap 95% interval."""
    def slope(sel):
        x = log([p[key] for p in sel])
        return float(np.polyfit(x, log([p["observed"] for p in sel]), 1)[0]) if np.ptp(x) > 0 else None

    members = {}
    for i, p in enumerate(rows):
        members.setdefault(p["cpu"], []).append(i)
    rng = np.random.default_rng(SEED)
    boots = [b for b in (slope([rows[i] for c in rng.choice(sorted(members), len(members)) for i in members[c]])
                         for _ in range(BOOT)) if b is not None]
    ci = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))] if boots else None
    return slope(rows), ci


def dram_demand(rigs):
    fr = [(lb.dram_demand_fraction(r), r) for r in rigs if not lb.is_apple(r)]
    fr = [(f, r) for f, r in fr if f is not None]
    vals = np.array([f for f, _ in fr])
    top = sorted(fr, key=lambda t: -t[0])[:5]
    return {
        "n_rigs": len(vals),
        "bytes_per_hash": lb.params.PROGRAM_COUNT * lb.params.PROGRAM_ITERATIONS * lb.params.DATASET_ITEM_SIZE,
        "quantiles": {q: float(np.quantile(vals, float(q))) for q in ("0.5", "0.9", "0.99")},
        "share_over_0.25": float(np.mean(vals > 0.25)),
        "top": [{"cpu": r["cpu"], "hashrate": r["hashrate"], "channels": r["channels"],
                 "mem_mts": r["mem_mts"], "mem_type": r["mem_type"], "fraction": f} for f, r in top],
    }, vals


def headline_bias(index, cpus, rigs):
    """Index headline (max of every submission) vs the typical rig, and how much of the gap
    is max-vs-median alone: the max run inside the filtered set (10M, 1 socket, modal topology)."""
    head = {}
    for r in index:  # brand strings that differ only in whitespace are separate index rows
        name = lb._clean(r["cpu"])
        head[name] = max(head.get(name, 0.0), r["hashrate"])
    pairs = [(c, head[c["cpu"]]) for c in cpus if c["cpu"] in head]
    ratio = np.array([h / c["hashrate"] for c, h in pairs])
    filtered_max = {}
    for r in rigs:
        key = (r["cpu"], r["cores"], r["l3_mib"])
        filtered_max[key] = max(filtered_max.get(key, 0.0), r["hashrate_max"])
    within = np.array([filtered_max[(c["cpu"], c["cores"], c["l3_mib"])] / c["hashrate"] for c, _ in pairs])
    worst = sorted(pairs, key=lambda p: -p[1] / p[0]["hashrate"])[:8]
    return {
        "n_cpus": len(pairs),
        "headline_over_typical_quantiles": {q: float(np.quantile(ratio, float(q))) for q in ("0.1", "0.5", "0.9")},
        "share_headline_over_1.10x": float(np.mean(ratio > 1.10)),
        "filtered_max_over_typical_quantiles": {q: float(np.quantile(within, float(q)))
                                                for q in ("0.1", "0.5", "0.9")},
        "share_filtered_max_over_1.10x": float(np.mean(within > 1.10)),
        "largest": [{"cpu": c["cpu"], "headline_max": h, "median_of_rig_medians": c["hashrate"],
                     "rigs": c["rigs"], "ratio": h / c["hashrate"]} for c, h in worst],
    }, pairs


TIERS = ("", "pro", "max", "ultra")


def _apple_name(brand):
    """'Apple M2-Max ' -> 'Apple M2 Max' (hwloc spellings differ across XMRig versions)."""
    return " ".join(brand.replace("-", " ").split())


def _apple_order(name):
    parts = name.lower().split()[1:]
    return int(parts[0][1:]), TIERS.index(parts[1] if len(parts) > 1 else "")


def apple(index, rigs, offline):
    """Apple M-series: 10M rigs (the filtered set) and all-size submissions (bursts, context only)."""
    names = {}
    for r in index:
        if r["cpu"].startswith("Apple M"):
            names.setdefault(_apple_name(r["cpu"]), []).append(r)
    table, per_thread, m4 = [], {}, None
    for name in sorted(names, key=_apple_order):
        subs = []
        for r in names[name]:
            try:
                subs += [s for s in lb.fetch_cpu(r["cpu"], offline) if s.get("status") == 3]
            except lb.Offline:
                pass
        big = [s["hashrate"] for s in subs if s["size"] >= lb.MIN_SIZE]
        small = [s["hashrate"] for s in subs if s["size"] < lb.MIN_SIZE]
        rig_rows = [r for r in rigs if _apple_name(r["cpu"]) == name]
        per_thread[name] = [s["hashrate"] / s["threads"] for s in subs]
        row = {
            "cpu": name,
            "threads_typical": statistics.mode([s["threads"] for s in subs]) if subs else None,
            "index_headline_max": max(r["hashrate"] for r in names[name]),
            "n_submissions": len(subs),
            "n_submissions_10M": len(big),
            "median_submission_10M": float(np.median(big)) if big else None,
            "median_submission_under_10M": float(np.median(small)) if small else None,
            "rigs_10M": len(rig_rows),
            "median_of_rig_medians_10M": float(np.median([r["hashrate"] for r in rig_rows])) if rig_rows else None,
            "median_per_thread_all_sizes": float(np.median(per_thread[name])) if subs else None,
            "cores": _mode_or_none([r["cores"] for r in rig_rows]),
            "l2_mib": _mode_or_none([r["l2_mib"] for r in rig_rows]),
        }
        table.append(row)
        if name == "Apple M4":
            m4 = {
                "summary": row,
                "rigs_10M": rig_rows,
                "submissions": sorted(({k: s.get(k) for k in ("id", "size", "hashrate", "threads", "version", "os")}
                                       for s in subs), key=lambda s: -s["hashrate"]),
                "notes": [
                    "XMRig cannot tell MacBook Air (fanless) from MacBook Pro / Mac mini / iMac M4; "
                    "all report 'Apple M4'.",
                    "Only size >= 10M rows pass the repo's filter; 1M rows are short bursts (about 4 minutes at M4 speed) and are context only.",
                    "macOS gives XMRig no thread affinity, so its per-thread split is not per-core.",
                ],
            }
    return table, per_thread, m4


def _mode_or_none(vals):
    return statistics.mode(vals) if vals else None


def figures(pairs, cpus, best, rates, within, dram_vals, sat_pts):
    plt = plotstyle.apply()
    pal = plotstyle.PALETTE
    FIG.mkdir(exist_ok=True)

    fig, ax = plt.subplots()
    typ = np.array([c["hashrate"] for c, _ in pairs])
    head = np.array([h for _, h in pairs])
    ax.scatter(typ, head, s=10, color=pal[0], alpha=0.6, linewidths=0)
    lim = [min(typ.min(), head.min()) * 0.8, max(typ.max(), head.max()) * 1.2]
    ax.plot(lim, lim, color=pal[5], lw=1, label="headline = typical")
    ax.set(xscale="log", yscale="log", xlim=lim, ylim=lim,
           xlabel=f"typical: median of rig medians, 10M runs, 1 socket, CPUs with >= {MIN_RIGS} rigs (H/s)",
           ylabel="leaderboard headline: max submission (H/s)",
           title="The XMRig leaderboard headline overstates the typical rig")
    ax.legend(loc="upper left", frameon=False)
    fig.savefig(FIG / "hw_headline_vs_typical.png")
    plt.close(fig)

    fig, ax = plt.subplots()
    y = np.array([c["hashrate"] for c in cpus])
    pred = np.exp(best["oof_log_pred"])
    for i, (vendor, marker) in enumerate((("amd", "o"), ("intel", "^"), ("hygon", "s"))):
        m = np.array([c["vendor"] == vendor for c in cpus])
        if m.any():
            ax.scatter(pred[m], y[m], s=14, marker=marker, color=pal[i], alpha=0.7, linewidths=0,
                       label=f"{VENDOR_NAMES[vendor]} ({m.sum()} CPUs)")
    lim = [min(y.min(), pred.min()) * 0.8, max(y.max(), pred.max()) * 1.2]
    ax.plot(lim, lim, color=pal[5], lw=1)
    ax.set(xscale="log", yscale="log", xlim=lim, ylim=lim, xlabel="out-of-fold prediction (H/s)",
           ylabel="per-CPU median of rig medians (H/s)",
           title=f"{best['name']}\nout-of-fold R\u00b2 {best['r2_log_hashrate']:.2f} (log H), "
                 f"{best['r2_log_per_thread']:.2f} (log H/thread)")
    ax.legend(frameon=False, loc="upper left")
    fig.savefig(FIG / "hw_structural_fit.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.5, 0.26 * len(rates) + 1.6), layout="constrained")
    for i, (name, vals, kind) in enumerate(rates):
        apple_row = kind == "apple"
        ax.scatter(vals, np.full(len(vals), i), s=12, alpha=0.45, linewidths=0,
                   marker="D" if apple_row else "o", color=pal[3] if apple_row else pal[0])
        ax.scatter([np.median(vals)], [i], s=70, marker="|", color="black", linewidths=2)
    ax.set_yticks(range(len(rates)))
    unit = {"apple": ("run", "runs"), "x86": ("CPU", "CPUs")}
    ax.set_yticklabels([f"{n} ({len(v)} {unit[k][len(v) != 1]})" for n, v, k in rates])
    handles = [plt.Line2D([], [], ls="", marker="o", color=pal[0], label="x86 CPU: 10M runs, H/s per effective thread"),
               plt.Line2D([], [], ls="", marker="D", color=pal[3], label="Apple run: any size, H/s per thread"),
               plt.Line2D([], [], ls="", marker="|", color="black", markersize=10, label="median")]
    fig.legend(handles=handles, frameon=False, loc="outside lower center", ncol=3, fontsize=8)
    ax.set(xlabel="RandomX hashrate per thread (H/s)", title="Per-thread RandomX rate by microarchitecture")
    ax.grid(axis="y", alpha=0)
    fig.savefig(FIG / "hw_per_thread_rate.png")
    plt.close(fig)

    fig, ax = plt.subplots()
    coefs = within["no_memory"]["coef"]
    mem = within["with_memory"]["coef"]
    names = list(coefs) + [n for n in mem if n not in coefs]
    for j, (tag, cf, marker, color) in enumerate((("all rigs", coefs, "o", pal[0]),
                                                  ("rigs with DIMM data", mem, "s", pal[1]))):
        for i, n in enumerate(names):
            if n in cf:
                e = cf[n]
                ax.errorbar(e["estimate"], i + (j - 0.5) * 0.25,
                            xerr=[[e["estimate"] - e["ci95"][0]], [e["ci95"][1] - e["estimate"]]],
                            fmt=marker, color=color, ms=5, lw=1.2, label=tag if i == 0 else None)
    ax.axvline(0, color=pal[5], lw=1)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels([WITHIN_LABELS[n] for n in names])
    ax.set(xlabel="effect on log(rig hashrate / CPU median), 95% CPU-cluster bootstrap CI",
           title="Within one CPU model, what separates fast rigs from slow ones")
    ax.legend(frameon=False, loc="upper right")
    fig.savefig(FIG / "hw_within_cpu.png")
    plt.close(fig)

    fig, ax = plt.subplots()
    for over, marker, color, label in ((False, "o", pal[0], "threads <= L3 slots"),
                                       (True, "^", pal[1], "threads > L3 slots")):
        pts = [p for p in sat_pts if (p["over"] > 1.05) == over]
        ax.scatter([p["expected_smt1"] for p in pts], [p["observed"] for p in pts], s=10, marker=marker,
                   color=color, alpha=0.5, linewidths=0, label=f"{label} ({len(pts)})")
    vals = [p[k] for p in sat_pts for k in ("expected_smt1", "observed")]
    lim = [min(vals) * 0.8, max(vals) * 1.25]
    ax.plot(lim, lim, color=pal[5], lw=1, label="hypothesis: observed = expected")
    ax.set(xscale="log", yscale="log", xlim=lim, ylim=lim,
           xlabel="expected rig / CPU median: min(threads, L3/2 MiB) vs the CPU's usual",
           ylabel="observed rig hashrate / CPU median",
           title="Within-CPU test of min(threads, L3 / 2 MiB), full huge pages only")
    ax.legend(frameon=False, loc="upper left")
    fig.savefig(FIG / "hw_cache_saturation.png")
    plt.close(fig)

    fig, ax = plt.subplots()
    ax.hist(dram_vals * 100, bins=np.linspace(0, max(40, dram_vals.max() * 100 + 1), 41), color=pal[0])
    ax.set(xlabel="dataset traffic as % of theoretical DRAM peak (1 MiB/hash)", ylabel="rigs",
           title=f"Median rig moves {np.median(dram_vals):.0%} of its theoretical DRAM peak")
    fig.savefig(FIG / "hw_dram_demand.png")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="crawl api.xmrig.com (uncached parts) and rewrite rigs.csv")
    ap.add_argument("--offline", action="store_true", help="never touch the network")
    args = ap.parse_args()
    counts = None
    if args.rebuild or (lb.CACHE / "index.json.gz").exists():
        _, counts = lb.build(offline=args.offline or not args.rebuild)
    rigs = lb.load_rigs()
    cpus_all = lb.cpu_table(rigs)
    cpus = [c for c in cpus_all if c["vendor"] in X86 and not lb.is_apple(c)
            and lb.cache_slots(c) >= 1 and c["l2_mib"]]
    excluded = [c["cpu"] for c in cpus_all if c not in cpus and not lb.is_apple(c)]

    models = compare_models(cpus)
    robust_cpus = [c for c in cpus if c["rigs"] >= MIN_RIGS]
    models_robust = compare_models(robust_cpus)
    structural = [m for m in models if m["name"].startswith("structural")]
    best = max(structural, key=lambda m: m["r2_log_hashrate"])
    y = log([c["hashrate"] for c in cpus])
    resid = y - best["oof_log_pred"]
    robust = [i for i, c in enumerate(cpus) if c["rigs"] >= MIN_RIGS]
    order = sorted(robust, key=lambda i: resid[i])

    def outlier(i):
        c = cpus[i]
        return {**{k: c[k] for k in ("cpu", "uarch", "power_class", "rigs", "threads_used", "l3_mib", "hashrate")},
                "predicted": float(np.exp(best["oof_log_pred"][i])), "ratio": float(np.exp(resid[i]))}

    cache, best_smt, by_power = best["cache"], best["smt"], best["by_power"]
    final = lb.StructuralModel(cache, smt=best_smt, by_power=by_power).fit(cpus)
    rate_rows = {}
    for c in cpus:
        rate_rows.setdefault(c["uarch"], []).append(c["hashrate"] / lb.effective_threads(c, cache, final.smt))

    index = lb.fetch_index(args.offline)
    bias, pairs = headline_bias(index, robust_cpus, rigs)
    apple_rows, apple_pt, m4 = apple(index, rigs, args.offline)
    within = within_cpu(rigs, cpus)
    saturation, sat_pts = cache_saturation(rigs, cpus)
    dram, dram_vals = dram_demand(rigs)

    rates = sorted(((u, v, "x86") for u, v in rate_rows.items() if len(v) >= 3), key=lambda t: np.median(t[1]))
    rates += [(name, vals, "apple") for name, vals in apple_pt.items() if vals]
    figures(pairs, cpus, best, rates, within, dram_vals, sat_pts)

    result = {
        "machine": sysinfo.describe(),
        "source": "api.xmrig.com /1/benchmarks, /1/benchmarks?cpu=, /1/benchmark/<id>",
        "filters": {"algo": lb.ALGO, "min_size": lb.MIN_SIZE, "status": 3, "packages": 1,
                    "cpu_topology": "rigs whose (cores, L3) match the brand's modal topology"},
        "crawl_counts": counts,
        "n_rigs": len(rigs),
        "n_cpus_x86_model": len(cpus),
        f"n_cpus_x86_with_min{MIN_RIGS}_rigs": len(robust_cpus),
        "excluded_non_x86_or_l3_under_2mib": excluded,
        "cv": {"k": K, "repeats": REPEATS, "seed": SEED, "unit": "CPU (median of rig medians)"},
        "models": [{k: v for k, v in m.items() if k != "oof_log_pred"} for m in models],
        f"models_cpus_with_min{MIN_RIGS}_rigs": [{k: v for k, v in m.items() if k != "oof_log_pred"}
                                                 for m in models_robust],
        "best_structural": best["name"],
        "best_structural_full_fit": {
            "cache": cache, "smt_weight": final.smt, "by_power_class": by_power,
            "per_thread_rate_by_vendor": dict(sorted(final.table("vendor").items())),
            "per_thread_rate_by_uarch": dict(sorted(final.table("uarch").items())),
            "per_thread_rate_by_uarch_power_class": {
                f"{u}/{p}": v for (u, p), v in sorted(final.table("uarch+power").items())},
        },
        "memory_models": memory_models(cpus, lambda: lb.StructuralModel(cache, smt=best_smt, by_power=by_power)),
        f"residual_outliers_min{MIN_RIGS}_rigs": {"slower_than_predicted": [outlier(i) for i in order[:8]],
                                       "faster_than_predicted": [outlier(i) for i in order[::-1][:8]]},
        "within_cpu": within,
        "cache_saturation": saturation,
        "dram_demand": dram,
        "headline_bias": bias,
        "apple": apple_rows,
        "apple_m4": m4,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(_round(result), indent=1, sort_keys=False) + "\n")
    for m in models + models_robust:
        print(f"{m['name']:<72} R2(logH) {m['r2_log_hashrate']:.3f}  R2(log H/thread) {m['r2_log_per_thread']:.3f}  "
              f"MdAPE {m['median_abs_pct_error']:.1f}%")
    print(f"{len(rigs)} rigs, {len(cpus)} x86 CPUs; best structural: {best['name']}")


if __name__ == "__main__":
    main()
