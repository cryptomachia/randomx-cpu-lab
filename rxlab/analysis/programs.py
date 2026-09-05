"""What is inside a RandomX program, and what skipping nonces by program cost buys.

Program data comes from build/progdump (tools/progdump.cpp), which replays
randomx_calculate_hash with the reference library's own generator, compiler and
interpreter and writes one CSV row per program. This module drives it, holds
the spec reference decoder the tests check progdump against, a simple
latency/throughput cost model, and the throughput arithmetic of nonce skipping.
"""
import csv
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from rxlab.spec import params

ROOT = Path(__file__).resolve().parents[2]
PROGDUMP = ROOT / "build" / "progdump"
CACHE_DIR = ROOT / "data" / "cache"

TYPES = [name for name, _ in params.FREQUENCIES]
OPCODE_TYPE = np.repeat(np.arange(len(TYPES)), [f for _, f in params.FREQUENCIES])
INT_MEM = ("IADD_M", "ISUB_M", "IMUL_M", "IMULH_M", "ISMULH_M", "IXOR_M")
FP_MEM = ("FADD_M", "FSUB_M", "FDIV_M")
FP_TYPES = ("FSWAP_R", "FADD_R", "FADD_M", "FSUB_R", "FSUB_M", "FSCAL_R", "FMUL_R", "FDIV_M", "FSQRT_R")
IMM_WHEN_SAME = ("ISUB_R", "IMUL_R", "IXOR_R", "IROR_R", "IROL_R")
STATIC_COLUMNS = (["n_" + t for t in TYPES] + [
    "nop_eff", "rd_l1", "rd_l2", "rd_l3", "rd_fp", "wr_l1", "wr_l2", "wr_l3",
    "imm_src", "cbr_body", "cbr_body_max", "cp_unit", "cp_model", "cp_int"])

# Official test vector 1e (src/tests/tests.cpp): a real Monero hashing blob,
# nonce as 4 little-endian bytes at offset 39.
MONERO_KEY = b"test key 001"
MONERO_BLOB = bytes.fromhex(
    "0b0b98bea7e805e0010a2126d287a2a0cc833d312cb786385a7c2f9de69d25537f584a9bc9977b"
    "00000000666fd8753bf61a8631f12984e3fd44f4014eca629276817b56f32e9b68bd82f416")
NONCE_OFFSET = 39

# Cost model parameters: rough cycle counts for a wide out-of-order core (Apple
# M-series P-core / Zen 4 class, public microbenchmark tables). They are
# assumptions that shape the model, not measurements on this machine.
LATENCY_KEYS = ("alu", "mul", "mulh", "agu", "load_l1", "load_l2", "load_l3", "fadd",
                "fmul", "fdiv", "fsqrt", "fswap", "fxor", "fcvt", "emask")
LATENCY = dict(alu=1, mul=3, mulh=3, agu=2, load_l1=4, load_l2=10, load_l3=18, fadd=3,
               fmul=4, fdiv=10, fsqrt=13, fswap=2, fxor=2, fcvt=5, emask=2)
UNIT_LATENCY = dict(alu=1, mul=1, mulh=1, agu=0, load_l1=0, load_l2=0, load_l3=0, fadd=1,
                    fmul=1, fdiv=1, fsqrt=1, fswap=1, fxor=1, fcvt=0, emask=0)
UNITS = dict(alu=6, mul=2, load=3, store=2, fp=4, branch=2)
ISSUE_WIDTH = 8
WINDOW_UOPS = 600       # in-flight window (reorder buffer)
DIVIDER_CYCLES = 2      # divider occupancy per FDIV_M / FSQRT_R
MISPREDICT_CYCLES = 13  # every taken CBRANCH is a misprediction (p = 1/256)
FPRC_WRITE_CYCLES = 10  # rounding-mode register write
DATASET_LATENCY_CYCLES = 450  # ~100 ns DRAM + page walk (2 GiB, 16 KiB pages) at ~4.4 GHz


def blob_with_nonce(nonce, blob=MONERO_BLOB, offset=NONCE_OFFSET):
    b = bytearray(blob)
    b[offset:offset + 4] = int(nonce).to_bytes(4, "little")
    return bytes(b)


# --- progdump driver -------------------------------------------------------

def ensure_progdump():
    """Build build/progdump if it is missing or older than its source."""
    src = ROOT / "tools" / "progdump.cpp"
    if not PROGDUMP.exists() or PROGDUMP.stat().st_mtime < src.stat().st_mtime:
        subprocess.run(["bash", str(ROOT / "tools" / "build_progdump.sh")], check=True,
                       stdout=subprocess.DEVNULL)
    return PROGDUMP


def _latency_arg(latency):
    return ",".join(str(int(latency[k])) for k in LATENCY_KEYS)


def progdump_args(v2, inputs=None, start=0, count=0, key=MONERO_KEY, blob=MONERO_BLOB,
                  light=False, raw_limit=0, latency=LATENCY):
    args = ["--key", key.decode(), "--latency", _latency_arg(latency)]
    if v2:
        args.append("--v2")
    if light:
        args.append("--light")
    if raw_limit:
        args += ["--raw-limit", str(raw_limit)]
    if inputs is not None:
        for x in inputs:
            args += ["--input", bytes(x).hex()]
    else:
        args += ["--blob", blob.hex(), "--start", str(start), "--count", str(count)]
    return args


def dump(v2, inputs=None, start=0, count=0, threads=4, cache=True, **kw):
    """Run progdump (or reuse its cached CSV; threads do not change the output)
    and return the parsed table."""
    args = progdump_args(v2, inputs=inputs, start=start, count=count, **kw)
    tag = hashlib.sha1(" ".join(args).encode()).hexdigest()[:12]
    args += ["--threads", str(threads)]
    path = CACHE_DIR / f"progdump_{'v2' if v2 else 'v1'}_{tag}.csv"
    if cache and path.exists():
        return load_csv(path)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=CACHE_DIR, suffix=".tmp", delete=False) as fh:
        tmp = Path(fh.name)
        subprocess.run([str(ensure_progdump())] + args, stdout=fh, check=True)
    if not cache:
        try:
            return load_csv(tmp)
        finally:
            tmp.unlink()
    os.replace(tmp, path)
    return load_csv(path)


def load_csv(path):
    """progdump CSV -> dict of numpy columns (hash/raw/fingerprint stay strings)."""
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = list(reader)
    cols = list(zip(*rows)) if rows else [()] * len(header)
    table = {}
    for name, values in zip(header, cols):
        if name in ("hash", "raw", "fingerprint"):
            table[name] = list(values)
        else:
            table[name] = np.array(values, dtype=np.int64)
    return table


def per_input(table, column):
    """Column reshaped to (inputs, 8), rows ordered by (input, prog)."""
    order = np.lexsort((table["prog"], table["input"]))
    return np.asarray(table[column])[order].reshape(-1, params.PROGRAM_COUNT)


def bench_prefix(v2, count, latency=LATENCY, blob=MONERO_BLOB):
    """ns per nonce to derive program 0 (Blake2b + scratchpad fill + generation),
    and with the static feature pass added, measured in C by progdump."""
    args = ["--bench-prefix", "--latency", _latency_arg(latency), "--blob", blob.hex(),
            "--start", "0", "--count", str(count)] + (["--v2"] if v2 else [])
    out = subprocess.run([str(ensure_progdump())] + args, capture_output=True, text=True,
                         check=True).stdout
    res = json.loads(out)
    res.pop("sink", None)
    return res


# --- spec reference decoder (independent of the library; used by the tests) --

def decode(raw, v2):
    """Raw program bytes (128 B configuration + 8 B per instruction) -> instruction tuples
    (type name, dst, src, mod, imm32) per spec section 5.1."""
    raw = bytes(raw)
    n = params.PROGRAM_SIZE_V2 if v2 else params.PROGRAM_SIZE_V1
    out = []
    for i in range(n):
        w = raw[128 + 8 * i:136 + 8 * i]
        out.append((TYPES[OPCODE_TYPE[w[0]]], w[1] % 8, w[2] % 8, w[3],
                    int.from_bytes(w[4:8], "little")))
    return out


def _read_level(name, dst, src, mod):
    if name in INT_MEM and src == dst:
        return 2
    return 1 if mod % 4 == 0 else 0


def reference_features(raw, v2, latency=LATENCY):
    """The static columns of progdump, recomputed from the spec for one program."""
    f = {c: 0 for c in STATIC_COLUMNS}
    last = [-1] * 8
    unit, model = [0] * 16, [0] * 16
    for i, (name, dst, src, mod, imm) in enumerate(decode(raw, v2)):
        f["n_" + name] += 1
        rcp_nop = name == "IMUL_RCP" and params.is_zero_or_power_of_2(imm)
        if rcp_nop or (name == "ISWAP_R" and src == dst):
            f["nop_eff"] += 1
        if name in INT_MEM or name in FP_MEM:
            f["rd_l%d" % (_read_level(name, dst, src, mod) + 1)] += 1
            f["rd_fp"] += name in FP_MEM
        if name == "ISTORE":
            lvl = 2 if mod >> 4 >= 14 else (1 if mod % 4 == 0 else 0)
            f["wr_l%d" % (lvl + 1)] += 1
        if name in IMM_WHEN_SAME and src == dst:
            f["imm_src"] += 1
        if name == "CBRANCH":
            body = i - last[dst]
            f["cbr_body"] += body
            f["cbr_body_max"] = max(f["cbr_body_max"], body)
            last = [i] * 8
        elif name == "ISWAP_R":
            if src != dst:
                last[dst] = last[src] = i
        elif name == "IMUL_RCP":
            if not rcp_nop:
                last[dst] = i
        elif name.startswith("I") and name != "ISTORE":
            last[dst] = i
        for ready, lat in ((unit, UNIT_LATENCY), (model, latency)):
            _advance(ready, lat, name, dst, src, mod, rcp_nop)
    f["cp_unit"], f["cp_model"], f["cp_int"] = max(unit), max(model), max(model[:8])
    return f


def _advance(ready, lat, name, dst, src, mod, rcp_nop):
    """One step of the critical-path recurrence over r0-r7, f0-f3 (8-11), e0-e3 (12-15)."""
    fd, ed = 8 + dst % 4, 12 + dst % 4
    load = (lat["load_l1"], lat["load_l2"], lat["load_l3"])
    if name in INT_MEM:
        addr = ready[src] + lat["agu"] if src != dst else 0
        op = {"IMUL_M": "mul", "IMULH_M": "mulh", "ISMULH_M": "mulh"}.get(name, "alu")
        ready[dst] = max(ready[dst], addr + load[_read_level(name, dst, src, mod)]) + lat[op]
    elif name in ("FADD_M", "FSUB_M", "FDIV_M"):
        t = ready[src] + lat["agu"] + load[_read_level(name, dst, src, mod)] + lat["fcvt"]
        if name == "FDIV_M":
            ready[ed] = max(ready[ed], t + lat["emask"]) + lat["fdiv"]
        else:
            ready[fd] = max(ready[fd], t) + lat["fadd"]
    elif name in ("IADD_RS", "IMULH_R", "ISMULH_R"):
        ready[dst] = max(ready[dst], ready[src]) + lat["alu" if name == "IADD_RS" else "mulh"]
    elif name in IMM_WHEN_SAME:
        other = ready[src] if src != dst else 0
        ready[dst] = max(ready[dst], other) + lat["mul" if name == "IMUL_R" else "alu"]
    elif name == "IMUL_RCP":
        ready[dst] += 0 if rcp_nop else lat["mul"]
    elif name in ("INEG_R", "CBRANCH"):
        ready[dst] += lat["alu"]
    elif name == "ISWAP_R":
        ready[dst], ready[src] = ready[src], ready[dst]
    elif name == "FSWAP_R":
        ready[8 + dst] += lat["fswap"]
    elif name in ("FADD_R", "FSUB_R"):
        ready[fd] += lat["fadd"]
    elif name == "FSCAL_R":
        ready[fd] += lat["fxor"]
    elif name == "FMUL_R":
        ready[ed] += lat["fmul"]
    elif name == "FSQRT_R":
        ready[ed] += lat["fsqrt"]


def opcode_types(raw_hex, v2):
    """(programs, slots) matrix of instruction-type indices from raw program hex."""
    n = params.PROGRAM_SIZE_V2 if v2 else params.PROGRAM_SIZE_V1
    raw = np.frombuffer(b"".join(bytes.fromhex(h) for h in raw_hex), dtype=np.uint8)
    ops = raw.reshape(len(raw_hex), -1)[:, 128::8][:, :n]
    return OPCODE_TYPE[ops]


# --- expectations from the spec -------------------------------------------

def expected_counts(size):
    """Expected per-program counts implied by the opcode frequencies and mod/src rules."""
    freq = dict(params.FREQUENCIES)
    per = lambda names: size * sum(freq[n] for n in names) / 256
    int_mem, fp_mem, store = per(INT_MEM), per(FP_MEM), per(["ISTORE"])
    same = 1 / 8  # src == dst
    return {
        "rd_l3": int_mem * same,
        "rd_l2": int_mem * (1 - same) / 4 + fp_mem / 4,
        "rd_l1": int_mem * (1 - same) * 3 / 4 + fp_mem * 3 / 4,
        "wr_l3": store * 2 / 16,
        "wr_l2": store * 14 / 16 / 4,
        "wr_l1": store * 14 / 16 * 3 / 4,
        "fp": per(FP_TYPES),
        "cbranch": per(["CBRANCH"]),
        "cfround": per(["CFROUND"]),
        "nop_eff": per(["ISWAP_R"]) * same,
    }


# --- cost model ------------------------------------------------------------

def uops(t):
    """Approximate micro-op counts per program by resource class (a64 JIT expansions)."""
    n = lambda name: np.asarray(t["n_" + name], dtype=float)
    mem_int = sum(n(x) for x in INT_MEM)
    l3 = np.asarray(t["rd_l3"], dtype=float)
    alu = (n("IADD_RS") + n("ISUB_R") + n("IXOR_R") + n("IROR_R") + 2 * n("IROL_R")
           + n("INEG_R") + n("ISWAP_R") + np.asarray(t["imm_src"], dtype=float)
           + n("IADD_M") + n("ISUB_M") + n("IXOR_M")
           + 2 * (mem_int - l3) + l3
           + 2 * (n("FADD_M") + n("FSUB_M") + n("FDIV_M"))
           + 2 * n("ISTORE") + 2 * n("CBRANCH") + 2 * n("CFROUND"))
    mul = (n("IMUL_R") + n("IMUL_M") + n("IMULH_R") + n("IMULH_M") + n("ISMULH_R")
           + n("ISMULH_M") + n("IMUL_RCP"))
    load = (np.asarray(t["rd_l1"], dtype=float) + np.asarray(t["rd_l2"], dtype=float) + l3
            + n("IMUL_RCP"))
    fp = (n("FSWAP_R") + n("FADD_R") + n("FSUB_R") + n("FSCAL_R") + n("FMUL_R") + n("FSQRT_R")
          + 3 * (n("FADD_M") + n("FSUB_M")) + 5 * n("FDIV_M"))
    u = dict(alu=alu, mul=mul, load=load, store=n("ISTORE"), fp=fp, branch=n("CBRANCH"))
    u["total"] = sum(u.values())
    u["divider"] = n("FDIV_M") + n("FSQRT_R")
    return u


def cost_model(t, v2):
    """Modelled cycles per loop iteration of each program, with its components.

    Compute: the largest of the throughput bound, the loop-carried integer chain
    (integer critical path plus the scratchpad-address load that starts the next
    iteration) and the window bound (the full critical path overlapped across
    iterations by a WINDOW_UOPS reorder buffer), plus expected CBRANCH
    re-execution and misprediction and rounding-mode writes (v2 applies only
    1/16 of CFROUNDs). Memory: the line read at the end of iteration i is
    requested when program i-d ends (prefetch distance d = 1 in v1, 2 in v2),
    and nothing of iteration i+1 can start before it arrives, so in steady
    state (d+1) * cycles - compute >= latency. cycles = max(compute,
    (compute + latency) / (d+1)): compute-bound exactly when compute >= latency / d."""
    u = uops(t)
    size = params.PROGRAM_SIZE_V2 if v2 else params.PROGRAM_SIZE_V1
    col = lambda k: np.asarray(t[k], dtype=float)
    tput = np.maximum.reduce([u[k] / UNITS[k] for k in UNITS]
                             + [u["total"] / ISSUE_WIDTH, u["divider"] * DIVIDER_CYCLES])
    chain = col("cp_int") + LATENCY["agu"] + LATENCY["load_l3"] + LATENCY["alu"]
    window = col("cp_model") * u["total"] / WINDOW_UOPS
    bounds = np.stack([tput, chain, window])
    reexec = col("cbr_body") / 256 * tput / size
    mispredict = col("n_CBRANCH") / 256 * MISPREDICT_CYCLES
    fprc = col("n_CFROUND") / (16 if v2 else 1) * FPRC_WRITE_CYCLES
    compute = bounds.max(0) + reexec + mispredict + fprc
    distance = 2 if v2 else 1
    floor = (compute + DATASET_LATENCY_CYCLES) / (distance + 1)
    return {"throughput": tput, "int_chain": chain, "window": window, "compute": compute,
            "binding": np.array(["throughput", "int_chain", "window"])[bounds.argmax(0)],
            "memory_floor": floor, "compute_bound_above": DATASET_LATENCY_CYCLES / distance,
            "cycles": np.maximum(compute, floor),
            "reexec": reexec, "mispredict": mispredict, "fprc": fprc}


def model_description():
    return {"latency_cycles": dict(LATENCY), "units_per_cycle": dict(UNITS),
            "issue_width": ISSUE_WIDTH, "window_uops": WINDOW_UOPS,
            "divider_cycles": DIVIDER_CYCLES, "mispredict_cycles": MISPREDICT_CYCLES,
            "fprc_write_cycles": FPRC_WRITE_CYCLES,
            "dataset_latency_cycles": DATASET_LATENCY_CYCLES,
            "status": "assumed parameters (public figures for wide OoO cores), not measured"}


# --- statistics and nonce-skipping economics -------------------------------

def describe(x):
    x = np.asarray(x, dtype=float)
    q = np.percentile(x, [0.1, 1, 50, 99, 99.9])
    return {"n": int(x.size), "mean": float(x.mean()), "sd": float(x.std(ddof=1)),
            "cv": float(x.std(ddof=1) / x.mean()), "min": float(x.min()), "p0.1": float(q[0]),
            "p1": float(q[1]), "p50": float(q[2]), "p99": float(q[3]), "p99.9": float(q[4]),
            "max": float(x.max())}


def cv_predict(X, y, folds=5, seed=0, ridge=1e-3):
    """Out-of-fold ridge predictions of y from standardised X (k-fold, fixed seed)."""
    X = np.asarray(X, dtype=float).reshape(len(y), -1)
    y = np.asarray(y, dtype=float)
    fold = np.random.default_rng(seed).permutation(len(y)) % folds
    pred = np.empty(len(y))
    # numpy 2.0 with macOS Accelerate raises spurious FP warnings in matmul;
    # the finiteness check below is the real guard.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        for k in range(folds):
            tr, te = fold != k, fold == k
            mu, sd = X[tr].mean(0), X[tr].std(0)
            sd[sd == 0] = 1
            A = np.column_stack([np.ones(tr.sum()), (X[tr] - mu) / sd])
            reg = ridge * len(A) * np.eye(A.shape[1])
            reg[0, 0] = 0
            w = np.linalg.solve(A.T @ A + reg, A.T @ y[tr])
            pred[te] = np.column_stack([np.ones(te.sum()), (X[te] - mu) / sd]) @ w
    if not np.all(np.isfinite(pred)):
        raise FloatingPointError("non-finite predictions")
    return pred


def r2(y, pred):
    y = np.asarray(y, dtype=float)
    return float(1 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2))


def skip_gain(times, score, q, cost_skip, cost_keep=0.0):
    """Net hashrate gain from never finishing the fraction q of nonces with the highest
    score. A skipped nonce still costs cost_skip (deriving its features); a kept one
    costs its hash time plus cost_keep. The baseline hashes every nonce."""
    times = np.asarray(times, dtype=float)
    k = int(round(q * len(times)))
    keep = np.ones(len(times), dtype=bool)
    keep[np.argsort(-np.asarray(score, dtype=float), kind="stable")[:k]] = False
    spent = times[keep].sum() + keep.sum() * cost_keep + k * cost_skip
    return float(keep.sum() / spent / (len(times) / times.sum()) - 1)
