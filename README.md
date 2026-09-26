# randomx-cpu-lab

**Question.** Is there a better way to solve RandomX, Monero's proof-of-work, on a CPU?

**Answer.** Not algorithmically. On real v1 and v2 hashes, no nonce order beats brute force, skipping expensive nonces loses hashrate, and program structure allows no early reject, reuse or batching: a block costs D hashes however it is searched. Hashrate comes from implementation (fast mode, JIT, huge pages on x86) and hardware (microarchitecture, threads up to L3 / 2 MiB). RandomX v2 beats v1 on one M4 P-core. [FINDINGS.md](FINDINGS.md) has full intervals and limitations.

## Headline findings

1. **Nonce order does not matter.** Over 1,853,952 hashes and six orders, v1 hits/expected at D = 2^8 is 0.952–1.032, every exact 95% CI including 1. The smallest Holm-adjusted p of 376 tests is 0.93; the wait for a hit is memoryless.
2. **Skipping expensive nonces loses hashrate.** Program 0 exists only after Blake2b and the 2 MiB scratchpad fill (41.2 µs per nonce, v1). With that prefix charged, the best program-0 skip rule nets −0.25% (v1) and −0.60% (v2) when kept nonces reuse their fill, and −2.21% and −2.96% otherwise (20,000 nonces × 3 passes, load average about 20). Even a perfect, free program-0 filter gains only +0.33% and +1.02% gross at a 50% skip rate (cost model).
3. **Program structure offers nothing to exploit** (160,000 programs per version). Instruction slots per hash vary with CV 0.016% (v1) and 0.014% (v2); adjacent programs' modelled compute correlates at r = 0.0014 and 0.0021; no program repeats; a 32-wide warp sees about 16 instruction types per slot.
4. **Fast mode and the JIT are each worth about 6×.** v1 on this M4, 7 threads, 3 vs 3 unpaired trials, against the fast/JIT/hardware-AES/batch reference:

   | Flip | Ratio [95% interval] |
   |---|---|
   | fast → light mode | 0.162 [0.073, 0.314] |
   | JIT → interpreter | 0.169 [0.074, 0.388] |
   | hardware → software AES | 0.682 [0.454, 1.504] |
   | batch → single call | 1.014 [0.446, 2.187] |

   The autotuner's QoS winner ran at 0.928× [0.862, 0.984] of the default on 6 fresh pairs (default first in each), so the default (all logical CPUs, no QoS, batch API) is best supported.
5. **RandomX v2 is faster on one P-core:** v2/v1 = 1.155 [1.071, 1.455] on the v2.0.1 library, 6/6 ABBA pairs, Wilcoxon p = 0.031. At 7 threads, 1.004 [0.946, 1.388] (10 pairs): no detectable difference. A rounding-mode-changing FPCR write costs 7.92 ns; at 12,331 changes per hash that is about 4.1% of v1 single-thread hash time, so v2's CFROUND filter explains 1.039×, a quarter of the gap.
6. **Across CPUs, hashrate ≈ r(microarchitecture × power class) × min(threads, L3 / 2 MiB).** On the 392 of 927 x86 models in the XMRig benchmark database with at least 3 rigs, out-of-fold R² is 0.968 (log H/s) and 0.447 (log per-thread rate); no free-form log-linear model does better (best 0.9675 / 0.4419). Within a CPU model, huge pages add 52.7% (95% CI 47.2–58.7%; 4,301 rigs). Leaderboard headlines are a median 1.60× the typical rig.
7. **Absolute throughput, measured under load and on battery:** 2,168 H/s at 10 threads over the last 300 s of a 1,200 s run, 0.65% below the first-90 s burst; `thermalState` never serious. XMRig's M4 submissions: 4,072 H/s (one 10M-hash run), median 3,940 H/s (26 runs of 1M); load, implementation, power and cooling cannot be separated.

## Measurement conditions

| | |
|---|---|
| Machine | Apple M4 MacBook Air (Mac16,13), fanless; 4 P-cores sharing 16 MiB L2, 6 E-cores sharing 4 MiB L2; 24 GB; 16 KiB pages; macOS |
| Power | Low Power Mode off; source recorded per result file (AC: thread scaling, QoS placement; battery: other benchmarks) |
| Load | **Not idle.** An unrelated background workload ran throughout. 1-minute load average at start: 5.27 (benchmark suite), about 20 (19.3–21.5; `nonce_cost.py --full`), 4.4–12.7 (pure-Python timings) |
| Contamination | paging and compressor counters per trial (`results/bench_*.json`); decompressions in all 361 hashing trials, all 120 sustained samples |

Absolute H/s are therefore depressed, noisy and drift between experiments: v1 at 10 threads had a 1,424 H/s median in thread scaling, then 3,151 H/s in one 3 s autotuner window once load dropped. Interleaved or paired trial orders protect within-experiment comparisons. For clean absolute numbers, rerun `experiments/run_measurements.sh` on an idle machine.

## Repository map

| Path | Contents |
|---|---|
| `rxlab/` | librandomx ctypes binding, benchmark front end with hash verification, bootstrap statistics, autotuner, economics |
| `rxlab/spec/` | pure-Python RandomX v1/v2 light mode (Argon2d, SuperscalarHash, AES, VM, IEEE-754 rounding) |
| `rxlab/analysis/` | output test battery, program analysis and cost model, XMRig crawler and model |
| `scripts/` | `build_randomx.sh`: RandomX v1.2.1 and v2.0.1 into `third_party/` |
| `tools/` | `rxbench.c` driver, `progdump.cpp`, `fpcr_bench.c`, `golden/` reference harnesses |
| `experiments/` | one script per result, plus `run_measurements.sh` |
| `results/` | JSON per experiment, with the machine description |
| `figures/` | experiment PNGs |
| `data/` | `golden/` C-library reference dumps, `leaderboard/rigs.csv`, local `cache/` (not published) |
| `docs/` | method documents |
| `tests/` | pytest suite; `slow` marks upstream and fast-mode checks |

## Quick start

```sh
pip install --user cmake numpy scipy matplotlib gmpy2 pytest
scripts/build_randomx.sh            # v1.2.1, v2.0.1 -> third_party/
tools/build_rxbench.sh              # build/rxbench-<tag>
python3 -m pytest -q -m "not slow"
```

**Do not build with `-DARCH=native` on Apple Silicon.** Apple clang's `-march=native` defines neither `__ARM_FEATURE_CRYPTO` nor `__ARM_FEATURE_AES`; `-march=armv8-a+crypto` defines both. So librandomx compiles with `HAVE_AES=0` and `randomx-benchmark` fails with `ERROR: Cannot create VM with the selected options. Try using --softAes`. `scripts/build_randomx.sh` keeps the default ARCH.

**The upstream batch API leaks the rounding mode.** In v1.2.1 and v2.0.1, `randomx_calculate_hash` restores the FP environment but `randomx_calculate_hash_next` and `randomx_calculate_hash_last` do not; in one negative-control run, 49 of 64 batch steps left a non-nearest mode. `rxlab.binding.VM.hash_many` restores it after every call (`tests/test_binding_vectors.py::test_batch_api_restores_rounding_mode`).

## Reproducing each result

```sh
experiments/run_measurements.sh              # preflight + seven bench_* experiments, ~70 min, idle machine; --smoke: quick check
python3 experiments/nonce_strategies.py      # resumable; --analyse-only reuses cached hashes
python3 experiments/program_structure.py     # defaults --n 20000 --threads 4; timing-free
python3 experiments/nonce_cost.py --full     # idle machine only; without --full, a smoke run
python3 experiments/fpcr_cost.py             # reads program_structure.json, bench_v1_vs_v2.json
python3 experiments/hardware_model.py        # needs network the first time; --rebuild rewrites rigs.csv
python3 experiments/economics.py             # needs network (Monero nodes, XMR/USD)
python3 -m rxlab.spec --vectors              # pure Python; --fresh-cache --json > results/spec_light.json
```

## Documentation

- [FINDINGS.md](FINDINGS.md): all results
- [docs/benchmark-harness.md](docs/benchmark-harness.md): driver, verification, statistics, autotuner
- [docs/nonce-strategies.md](docs/nonce-strategies.md): nonce orders, output test battery
- [docs/program-structure.md](docs/program-structure.md): program survey, cost model, nonce cherry-picking
- [docs/hardware-model.md](docs/hardware-model.md): XMRig model, economics
- [docs/pure-python-randomx.md](docs/pure-python-randomx.md): `rxlab.spec`, verification
- [data/leaderboard/README.md](data/leaderboard/README.md): `rigs.csv` provenance
