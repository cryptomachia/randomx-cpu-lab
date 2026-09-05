"""What is inside a RandomX program? Static and dynamic structure, v1 and v2.

Runs build/progdump (the reference library's own program generation and
interpreter) over N nonces of the official Monero blob (test vector 1e) in fast
mode and summarises every program: instruction mix against the configured
frequencies, memory operands by scratchpad level, dataset reads, FP share,
CBRANCH counts and measured jump rate, register-dependency critical paths and a
latency/throughput cost model. Timing-free, so it can run on a busy machine.

    python3 experiments/program_structure.py [--n 20000] [--threads 4]

Writes results/program_structure.json and figures/program_*.png.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rxlab import plotstyle, sysinfo  # noqa: E402
from rxlab.analysis import programs as P  # noqa: E402
from rxlab.binding import VM, Cache, Flags, recommended_flags  # noqa: E402
from rxlab.spec import params  # noqa: E402

RAW_INPUTS = 512   # inputs whose raw program bytes are kept for the SIMT analysis
HASH_CHECKS = 16   # progdump hashes re-checked against the library's own VM
WARP = 32
SKIP_Q = (0.01, 0.05, 0.1, 0.25, 0.5)
VERSIONS = (("v1", False), ("v2", True))


def check_hashes(tab, v2):
    """progdump's fast-mode hashes must equal the library VM's (light mode, JIT)."""
    base = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    rows = np.flatnonzero(tab["prog"] == 7)[:HASH_CHECKS]
    with Cache(P.MONERO_KEY, flags=base) as cache, VM(base | (Flags.V2 if v2 else 0), cache=cache) as vm:
        ok = sum(vm.hash(P.blob_with_nonce(tab["input"][i])).hex() == tab["hash"][i] for i in rows)
    return {"checked": int(len(rows)), "matched": int(ok)}


def simt(tab, v2):
    """Instruction and branch divergence if programs ran as lanes of 32-wide warps."""
    raw = [r for r in tab["raw"] if r]
    types = P.opcode_types(raw, v2)
    warps = types[: len(types) // WARP * WARP].reshape(-1, WARP, types.shape[1])
    distinct = np.array([[len(np.unique(w[:, s])) for s in range(w.shape[1])] for w in warps])
    p = np.array([f for _, f in params.FREQUENCIES], dtype=float) / 256
    lane_jump = P.per_input(tab, "taken").ravel() / params.PROGRAM_ITERATIONS
    # Warps of 32 consecutive programs; a lane's jumps in one iteration taken as Poisson.
    warp_jump = lane_jump[: len(lane_jump) // WARP * WARP].reshape(-1, WARP).sum(1)
    return {
        "programs_sampled": int(len(raw)),
        "distinct_types_per_warp_slot_mean": float(distinct.mean()),
        "distinct_types_per_warp_slot_expected": float(np.sum(1 - (1 - p) ** WARP)),
        "same_type_probability_two_lanes": float(np.sum(p ** 2)),
        "lane_jumps_per_iteration_mean": float(lane_jump.mean()),
        "warp_iterations_with_a_jump": float(1 - np.mean(np.exp(-warp_jump))),
    }


def summarise(tab, v2):
    size = params.PROGRAM_SIZE_V2 if v2 else params.PROGRAM_SIZE_V1
    iters = params.PROGRAM_ITERATIONS
    n = len(tab["prog"])
    exp = P.expected_counts(size)
    counts = np.array([tab["n_" + t].sum() for t in P.TYPES], dtype=float)
    freq = np.array([f for _, f in params.FREQUENCIES], dtype=float)
    live = freq > 0
    chi = stats.chisquare(counts[live], counts.sum() * freq[live] / 256)
    fp = sum(tab["n_" + t] for t in P.FP_TYPES)
    cost = P.cost_model(tab, v2)
    by_hash = lambda k: P.per_input({"input": tab["input"], "prog": tab["prog"], "c": cost[k]}, "c")
    compute = by_hash("compute")
    hash_compute = compute.sum(1) * iters
    hash_cycles = by_hash("cycles").sum(1) * iters
    adjacent = np.corrcoef(compute[:, :-1].ravel(), compute[:, 1:].ravel())[0, 1]
    executed = P.per_input(tab, "executed").sum(1)
    reexec = (tab["executed"] - iters * size) / iters
    # Expected jumps from the static branch count: every jump re-executes its own
    # CBRANCH once (cbr_exec = static + taken), so cbr_exec depends on the outcome.
    expected_taken = iters * tab["n_CBRANCH"] * 2.0 ** -params.JUMP_BITS
    return {
        "programs": n,
        "inputs": n // params.PROGRAM_COUNT,
        "instructions_per_program": size,
        "instruction_mix": {
            t: {"configured": f / 256, "measured": float(counts[i] / counts.sum())}
            for i, (t, f) in enumerate(params.FREQUENCIES) if f},
        "mix_chisquare": {"statistic": float(chi.statistic), "dof": int(live.sum() - 1),
                          "p_value": float(chi.pvalue)},
        "type_absent": {
            t: {"programs": float(np.mean(tab["n_" + t] == 0)),
                "hashes_all_8": float(np.mean((P.per_input(tab, "n_" + t) == 0).all(1)))}
            for t, f in params.FREQUENCIES if f},
        "effective_nops_per_program": {"measured": float(tab["nop_eff"].mean()),
                                       "expected": exp["nop_eff"]},
        "memory_operands_per_program": {
            k: {"measured": float(tab[k].mean()), "expected": exp[k]}
            for k in ("rd_l1", "rd_l2", "rd_l3", "wr_l1", "wr_l2", "wr_l3")},
        "fp_reads_per_program": float(tab["rd_fp"].mean()),
        "per_iteration_fixed": {
            "dataset_reads": float(tab["ds_reads"].sum() / (iters * n)),
            "dataset_prefetches": 1, "scratchpad_reads_64B": 2, "scratchpad_writes_64B": 2,
            "note": "dataset_reads counted by progdump; the rest fixed by spec 4.6.2"},
        "fp_share": {"measured": float(fp.sum() / (size * n)),
                     "configured": float(exp["fp"] / size)},
        "cbranch": {
            "per_program": P.describe(tab["n_CBRANCH"]),
            "expected_per_program": exp["cbranch"],
            "taken_rate_measured": float(tab["taken"].sum() / tab["cbr_exec"].sum()),
            "taken_rate_expected": 2.0 ** -params.JUMP_BITS,
            "taken_per_static_execution": float(tab["taken"].sum()
                                                / (iters * tab["n_CBRANCH"].sum())),
            "body_length_mean": float(tab["cbr_body"].sum() / tab["n_CBRANCH"].sum()),
            "body_length_max": int(tab["cbr_body_max"].max()),
            "reexecuted_slots_per_iteration_measured": float(reexec.mean()),
            "reexecuted_slots_per_iteration_expected": float(tab["cbr_body"].mean() / 256),
            "programs_with_no_jump_taken": float(np.mean(tab["taken"] == 0)),
            "taken_per_program": P.describe(tab["taken"]),
            "taken_dispersion_ratio": float(np.var(tab["taken"] - expected_taken) / expected_taken.mean()),
            "programs_taken_z_above_5": int(np.sum((tab["taken"] - expected_taken)
                                                   / np.sqrt(expected_taken) > 5)),
        },
        "cfround": {
            "per_program": float(tab["n_CFROUND"].mean()),
            "fprc_writes_per_iteration": float(tab["fprc_writes"].mean() / iters),
            "fprc_changes_per_iteration": float(tab["fprc_changes"].mean() / iters),
            "writes_per_execution": float(tab["fprc_writes"].sum() / max(1, tab["cfround_exec"].sum())),
        },
        "critical_path_unit": P.describe(tab["cp_unit"]),
        "critical_path_model_cycles": P.describe(tab["cp_model"]),
        "ilp_unit": P.describe((size - tab["nop_eff"]) / tab["cp_unit"]),
        "fp_chain_longest_share": float(np.mean(tab["cp_model"] > tab["cp_int"])),
        "cost_model": {
            "compute_cycles_per_iteration": P.describe(cost["compute"]),
            "bounds": {k: P.describe(cost[k]) for k in ("throughput", "int_chain", "window")},
            "binding_bound_share": {k: float(np.mean(cost["binding"] == k))
                                    for k in ("throughput", "int_chain", "window")},
            "mean_additive_components": {k: float(np.mean(cost[k]))
                                         for k in ("reexec", "mispredict", "fprc")},
            "memory_floor_cycles": P.describe(cost["memory_floor"]),
            "compute_bound_above_cycles": float(cost["compute_bound_above"]),
            "compute_above_memory_floor_share": float(np.mean(cost["compute"] > cost["memory_floor"])),
            "cycles_per_iteration": P.describe(cost["cycles"]),
            "per_hash_compute_cycles": P.describe(hash_compute),
            "per_hash_cycles": P.describe(hash_cycles),
            "cv_ratio_hash_over_program": float(P.describe(hash_compute)["cv"]
                                                / P.describe(cost["compute"])["cv"]),
            "adjacent_program_correlation": float(adjacent),
            "program0_share_of_hash_compute_variance": float(np.var(compute[:, 0])
                                                             / np.var(compute.sum(1))),
            "skip_on_program0_free_gain": {
                str(q): P.skip_gain(hash_cycles, compute[:, 0], q, 0.0) for q in SKIP_Q},
        },
        "executed_slots_per_hash": P.describe(executed),
        "duplicate_programs": int(n - len(set(tab["fingerprint"]))),
        "simt": simt(tab, v2),
    }


def figures(tabs, out):
    plt = plotstyle.apply()
    names = [t for t, f in params.FREQUENCIES if f]
    order = sorted(names, key=lambda t: -dict(params.FREQUENCIES)[t])

    fig, ax = plt.subplots(figsize=(9, 4.2))
    x = np.arange(len(order))
    ax.bar(x - 0.27, [dict(params.FREQUENCIES)[t] / 256 * 100 for t in order], 0.27,
           label="configured (spec)", color="#bbbbbb")
    for j, (v, _) in enumerate(VERSIONS):
        mix = out[v]["instruction_mix"]
        ax.bar(x + 0.27 * j, [mix[t]["measured"] * 100 for t in order], 0.27,
               label=f"measured {v} ({out[v]['programs']:,} programs)")
    ax.set_xticks(x)
    ax.set_xticklabels(order, rotation=70, fontsize=8)
    ax.set_ylabel("share of instructions (%)")
    ax.set_title("RandomX instruction mix: generated programs vs configured frequencies")
    ax.legend()
    fig.savefig(ROOT / "figures" / "program_mix.png")
    plt.close(fig)

    fig, ax = plt.subplots()
    keys = ["rd_l1", "rd_l2", "rd_l3", "wr_l1", "wr_l2", "wr_l3"]
    labels = ["read L1", "read L2", "read L3", "write L1", "write L2", "write L3"]
    x = np.arange(len(keys))
    for j, (v, _) in enumerate(VERSIONS):
        mem = out[v]["memory_operands_per_program"]
        ax.bar(x + 0.4 * j - 0.2, [mem[k]["measured"] for k in keys], 0.4, label=f"measured {v}")
        ax.plot(x + 0.4 * j - 0.2, [mem[k]["expected"] for k in keys], "k_", ms=14,
                label="spec expectation" if j == 0 else None)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("memory operands per program")
    ax.set_title("Scratchpad operands per program (plus 1 dataset read per iteration)")
    ax.legend()
    fig.savefig(ROOT / "figures" / "program_memory.png")
    plt.close(fig)

    fig, (a, b) = plt.subplots(1, 2, figsize=(10, 3.8))
    for v, v2 in VERSIONS:
        tab = tabs[v]
        size = params.PROGRAM_SIZE_V2 if v2 else params.PROGRAM_SIZE_V1
        k = np.arange(tab["n_CBRANCH"].min(), tab["n_CBRANCH"].max() + 1)
        a.hist(tab["n_CBRANCH"], bins=np.append(k, k[-1] + 1) - 0.5, density=True, alpha=0.55,
               label=f"{v} measured")
        a.plot(k, stats.binom.pmf(k, size, 25 / 256), "k-", lw=0.8)
        top = np.percentile(tab["taken"], 99.99)
        b.hist(tab["taken"], bins=np.arange(0, top + 2) - 0.5, density=True, alpha=0.55,
               label=f"{v}: rate {out[v]['cbranch']['taken_rate_measured']:.5f}, "
                     f"max {tab['taken'].max()} (off scale)")
    a.set_xlabel("CBRANCH instructions per program")
    a.set_ylabel("fraction of programs")
    a.set_title("CBRANCH count (black: binomial from spec)")
    a.legend()
    b.set_xlabel("jumps taken per program (2048 iterations)")
    b.set_title(f"Jumps taken (expected rate 1/256 = {1 / 256:.5f})")
    b.legend(fontsize=8)
    fig.savefig(ROOT / "figures" / "program_cbranch.png")
    plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8))
    for j, (v, v2) in enumerate(VERSIONS):
        tab = tabs[v]
        cost = P.cost_model(tab, v2)
        color = plotstyle.PALETTE[j]
        per_hash = P.per_input({"input": tab["input"], "prog": tab["prog"], "c": cost["compute"]},
                               "c").sum(1)
        axes[0].hist(tab["cp_int"], bins=60, density=True, histtype="step", color=color,
                     label=f"{v} integer registers")
        axes[0].hist(tab["cp_model"], bins=60, density=True, alpha=0.4, color=color,
                     label=f"{v} all registers")
        axes[1].hist(cost["compute"], bins=60, density=True, alpha=0.55, color=color, label=v)
        axes[1].axvline(cost["compute_bound_above"], color=color, ls="--",
                        label=f"{v} compute-bound threshold")
        axes[2].hist(per_hash / per_hash.mean(), bins=60, density=True, alpha=0.55, color=color,
                     label=f"{v}: CV {per_hash.std() / per_hash.mean():.3f}")
    axes[0].set_xlabel("critical path of one iteration (model cycles)")
    axes[1].set_xlabel("modelled compute cycles per iteration")
    axes[2].set_xlabel("modelled compute per hash / mean")
    axes[0].set_ylabel("density")
    for ax in axes:
        ax.legend(fontsize=7)
    fig.suptitle("Per-program and per-hash cost under the latency/throughput model (assumed parameters)")
    fig.savefig(ROOT / "figures" / "program_cost.png")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--n", type=int, default=20000, help="inputs per version")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    out = {"machine": sysinfo.describe(),
           "setup": {"key": P.MONERO_KEY.decode(), "blob": P.MONERO_BLOB.hex(),
                     "nonce_offset": P.NONCE_OFFSET, "nonces": [0, args.n - 1],
                     "mode": "fast (2080 MiB dataset), bytecode interpreter",
                     "library": "RandomX v2.0.1 (computes v1 and v2)"},
           "model": P.model_description()}
    tabs = {}
    for v, v2 in VERSIONS:
        tab = P.dump(v2, start=0, count=args.n, threads=args.threads, raw_limit=RAW_INPUTS)
        tabs[v] = tab
        out[v] = summarise(tab, v2)
        out[v]["hash_check_vs_library"] = check_hashes(tab, v2)
        print(v, json.dumps(out[v]["hash_check_vs_library"]), flush=True)
    figures(tabs, out)
    path = ROOT / "results" / "program_structure.json"
    path.write_text(json.dumps(out, indent=1) + "\n")
    print("wrote", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
