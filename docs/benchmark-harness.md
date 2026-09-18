# Benchmark harness

This document covers how `randomx-cpu-lab` measures RandomX hashrate on a CPU. That means the native driver, the Python front end that validates every run, the statistics, the autotuner, the seven benchmark experiments and the script that runs them. It describes design and methodology only. Measured hashrates belong in `FINDINGS.md`, not here.

> **No timing in this document is a benchmark result.** The harness was built and tested on a loaded machine, with 1-minute load averages between 6 and 97 on 10 logical CPUs. The few timings quoted below are development observations from that machine, and each is labelled "measured on a loaded machine; not a benchmark result".

The development machine is a fanless Apple M4 MacBook Air with 10 logical CPUs. Its IORegistry maps logical CPUs 0–5 to the efficiency (E) cluster and 6–9 to the performance (P) cluster. Where this document gives concrete counts, such as candidate or layout counts, they are for that 4 P + 6 E shape. The code derives every count from the machine it runs on.

## Components

| File | Role |
|---|---|
| [tools/rxbench.c](../tools/rxbench.c) | C driver on the public `randomx.h` API. Its commands are `info`, `sweep`, `sample`, `init`, `verify` and `serve`. It prints one JSON object per line on stdout. |
| [tools/build_rxbench.sh](../tools/build_rxbench.sh) | Builds `build/rxbench-v1.2.1` and `build/rxbench-v2.0.1` against the matching `third_party/build-<tag>` library, using clang with `-O2 -std=c11 -Wall -Wextra` and an rpath to that library. |
| [rxlab/bench.py](../rxlab/bench.py) | Python front end. It wraps a long-lived `rxbench serve` process and holds the hash verifier, the sweep, sample, init and reference helpers, the per-config summaries and the result writer. It also has a CLI: `python3 -m rxlab.bench sweep\|sample\|init\|reference`. |
| [rxlab/stats.py](../rxlab/stats.py) | Medians, percentile-bootstrap intervals, paired and unpaired comparisons, and time-weighted window means. Seeds are fixed. |
| [rxlab/tune.py](../rxlab/tune.py) | Successive-halving autotuner over the hashing configuration and the dataset-init thread count. CLI: `python3 -m rxlab.tune [--smoke]`. |
| [rxlab/sysinfo.py](../rxlab/sysinfo.py) | Machine description embedded in every result file. |
| `experiments/bench_*.py` | The seven experiments (see [Experiments](#experiments)). |
| [experiments/run_measurements.sh](../experiments/run_measurements.sh) | Runs the correctness preflight, then all seven experiments in dependency order. |
| [tests/test_bench.py](../tests/test_bench.py), [tests/test_stats.py](../tests/test_stats.py) | 30 fast tests and 12 tests marked `slow`. Tests check hashes, digests and record structure. The only timing assertions are sanity bounds: a positive rate, and a window at least as long as requested. |

## rxbench, the native driver

### Commands

```
rxbench [--seed N] [--init-threads N] COMMAND
  info
  sweep  [--warmup S] [--measure S] [--trials N] [--order interleave|block] CONFIG...
  sample [--interval S] [--duration S] CONFIG
  init   [--threads-list 1,2,..] [--trials N] [--fraction F] [--qos CLASS]
  verify [--nonces N] CONFIG
  serve
```

| Command | What it does | Default settings |
|---|---|---|
| `info` | Prints the library tag, library flags, input template, nonce layout, logical CPU count and available QoS classes | none |
| `sweep` | Runs timed trials of one or more configs | 2 s warm-up, 5 s measurement, 3 trials, interleaved order |
| `sample` | Hashes one config continuously and prints a sample record at each interval | 10 s interval, 60 s duration |
| `init` | Times dataset initialisation at each listed thread count | the `--init-threads` count only, 3 trials, full dataset, QoS `unset` |
| `verify` | Prints the XOR of the hashes of nonces `0..N-1` | 1000 nonces |
| `serve` | Reads one of the above commands per stdin line and runs it in the same process | none |

The seed defaults to 0. `--init-threads` sets the thread count used to build the dataset and defaults to the number of logical CPUs. Every command prints a `start` record first and an `end` record last.

### Configuration strings

A CONFIG is a list of `key=value` pairs joined by commas, for example `v=1,mode=fast,jit=1,aes=hard,batch=1,threads=10,qos=ui*4+bg`.

| Key | Values | Default | Meaning |
|---|---|---|---|
| `v` | 1, 2 | 1 | RandomX version. `v=2` sets `RANDOMX_FLAG_V2` (value 128) and requires a v2.x library. |
| `mode` | `fast`, `light` | `fast` | `fast` uses the 2080 MiB dataset (`RANDOMX_FLAG_FULL_MEM`). `light` uses only the 256 MiB cache. |
| `jit` | 0, 1 | 1 | JIT compiler or interpreter |
| `aes` | `hard`, `soft` | `hard` | Sets or clears `RANDOMX_FLAG_HARD_AES` |
| `batch` | 0, 1 | 1 | `1` uses the pipelined `randomx_calculate_hash_first/next/last` API. `0` uses one `randomx_calculate_hash` call per hash. |
| `threads` | 1–64 | 1 | Number of hashing threads |
| `qos` | `class*count` entries joined by `+` | `unset` | QoS class for each thread. The last class fills any remaining threads. |

The QoS classes, with their aliases, are `unset`, `user_interactive` (`ui`), `user_initiated` (`in`), `default` (`def`), `utility` (`ut`) and `background` (`bg`). rxbench echoes a canonical form of every config. `bench.spec()` and `bench.qos_mix()` build exactly the same string, so the Python side and the C side agree on which config is which, and a test checks this.

Several flags are not chosen by the config:

- On macOS arm64, JIT always carries `RANDOMX_FLAG_SECURE`. rxbench takes it from `randomx_get_flags()`, which sets it under upstream's `RANDOMX_FORCE_SECURE`.
- The Argon2 flags also come from `randomx_get_flags()`.
- Each JIT setting has its own cache. A light-mode interpreter config therefore uses a non-JIT cache, so SuperscalarHash is interpreted too, as in upstream `randomx-benchmark`.
- The dataset is always built from the JIT cache (on a library without JIT, from the interpreter cache).

rxbench exits with a message on malformed or oversized input, rather than silently running something other than what was asked. That covers unknown config keys and options, more than 256 tokens on one `serve` line, strings too long for their buffers and more than 64 entries in `--threads-list`.

### Input and nonces

The hashed input and its layout follow upstream `randomx-benchmark`. That makes rxbench's output directly comparable with upstream's published reference values.

- **Input.** The input is upstream's 76-byte block template, with a 4-byte little-endian nonce at byte offset 39.
- **Key.** The key is the 4-byte little-endian seed, which defaults to 0.
- **Nonces.** Hashing thread *i* uses nonces `i·2^24, i·2^24 + 1, …`, so each thread walks its own range and no two threads share a nonce.
- **Reference check.** With seed 0, `verify --nonces 1000` prints the XOR of the hashes of nonces 0–999, exactly as upstream prints its "Reference result". Upstream's batch loop computes one extra nonce past the end and discards it, where rxbench calls `randomx_calculate_hash_last` instead. The XOR is the same either way.

### Trial lifecycle and rate definition

```
create threads, set QoS, create VMs, thread 0 hashes the check input  (untimed)
  -> warm-up: all threads hash for --warmup s                          (uncounted)
  -> measure: every hash completion is timestamped for --measure s
  -> stretch the window (up to 10 x --measure) until each thread has one fully timed hash
  -> stop, destroy VMs, print one `trial` record
```

- **Fresh threads and VMs every trial.** Every trial creates its own worker threads and VMs, so trials are independent. VM creation and the check hash both happen before the barrier that releases the warm-up, which keeps them out of the timed window.
- **Exact per-thread rates.** Inside the window, each thread timestamps every hash completion with `CLOCK_MONOTONIC`. The first completion starts the thread's clock, and the thread then counts `n` further completions up to the last one at `t_last`. Its rate is `n / (t_last − t_first)`, measured over `n` whole hash intervals, so partial hashes at the window edges cause no ±1 quantisation. The trial's `hs` is the sum of the per-thread rates. `hs_window`, the total count difference divided by the window length, is kept as a cross-check.
- **Window stretch.** A slow config (for example light mode with the interpreter) may finish no hash at all inside a short window. So after `--measure` seconds, the driver waits until every thread has at least 3 completions past its warm-up snapshot, which guarantees one fully timed interval, and gives up at 10 × `--measure`. `complete: false` marks a trial in which some thread still had no timed interval.
- **Order.** `--order interleave` is the default and runs trials in trial-major order: trial 0 of every config, then trial 1 of every config, and so on. Slow drift (heat, background load) is then spread across all configs instead of landing on the ones that happen to run last. `--order block` runs all trials of one config before moving to the next.
- **No false sharing.** Worker structs are 128-byte aligned, so per-thread counters do not share cache lines.

`sample` differs from `sweep` in three ways. There is no warm-up, so hashing starts at `t = 0`, which is what lets a sustained run capture the burst. Each `sample` record's `hs` is the aggregate completion count for the interval divided by its length. The closing `sample_end` record gives exact per-thread rates over the whole run, together with the first and last hashes used for verification.

### One process, one dataset

`serve` lets a whole experiment, including every round of the autotuner, run in one process. That process holds a single 2080 MiB dataset and one 256 MiB cache per JIT setting. The first fast-mode command builds the dataset, using `--init-threads` threads. RandomX v2 changes only the VM (CFROUND, AES mixing, program size 384, prefetch distance) and not the dataset, so the v2.0.1 library serves both v1 and v2 VMs from the same dataset.

`init` reuses the process's dataset memory. A partial init marks the dataset invalid, and the next fast-mode command rebuilds it.

### Dataset-init timing

Each timed init goes through these steps:

1. Zero the target range first, so first-touch page faults stay out of the timing.
2. Split the item range evenly across the requested threads, running at the requested QoS class.
3. Record the elapsed time and a 64-bit FNV-1a-style digest over the initialised bytes.

Within a trial, the thread counts run in interleaved order. After a full init, rxbench hashes nonce 0 with v1 in fast mode on that dataset, using 1 thread, JIT, hard AES and a single call. The result can only match the light-mode hash if every dataset item the hash touches is correct.

### Output records

| Record | Printed by | Fields |
|---|---|---|
| `start` | every command | `command`, `tag`, `seed`, `lib_flags` |
| `cache` | first use of a cache | `jit`, `seconds` |
| `dataset` | first fast-mode use | `threads`, `seconds` |
| `trial` | `sweep` | config fields, `trial`, `warmup_s`, `window_s`, `per_thread[]`, `check`, `hs`, `hs_window`, `complete`, `thermal` `[start, end]`, `vm` |
| `sample` | `sample` | `t`, `dt`, `hashes[]` (per thread), `hs`, `cpus`, `thermal`, `vm` |
| `sample_end` | `sample` | config fields, `duration_s`, `per_thread[]`, `check`, `hs`, `complete` |
| `init` | `init` | `threads`, `trial`, `qos`, `items`, `total_items`, `seconds`, `items_per_s`, `digest`, `thermal`, and `check` for full inits |
| `verify` | `verify` | config fields, `seed`, `nonces`, `result`, `seconds` |
| `info` | `info` | `tag`, `v2`, `lib_flags`, `dataset_items`, `template`, `nonce_offset`, `nonce_stride`, `check_nonce`, `logical_cpus`, `thermal`, `qos_classes` |
| `end` | every command | none |

The config fields are `config` (the canonical string), `v`, `mode`, `jit`, `aes`, `batch`, `threads`, `qos` and `flags`. `flags` is the `randomx_flags` value passed to `randomx_create_vm`. If any VM cannot be created, the `trial` record carries `error` in place of the timing fields, and `bench.sweep` raises.

Each `per_thread` entry holds:

| Field | Content |
|---|---|
| `qos` | The QoS class actually in effect for the thread |
| `hashes` | The number of fully timed hash intervals |
| `span_s` | The time from the thread's first to last completion in the window |
| `hs` | The thread's rate |
| `first`, `last` | `[nonce, hash]` pairs for the first and last completion in the window |
| `cpus` | A histogram mapping each logical CPU to the number of timed hashes that finished on it |

## Correctness checks

A reported rate is accepted only if the hashes behind it are correct. Five checks guard this.

1. **Upstream reference preflight.** `python3 -m rxlab.bench reference --mode light|fast` runs `verify --nonces 1000` for v1 and, on a v2.x library, v2 (4 threads, or fewer on a smaller machine; seed 0) and exits with status 1 on a mismatch. The expected strings live in `bench.UPSTREAM_REFERENCE`. A test checks that they appear verbatim in upstream's `src/tests/benchmark.cpp`, which prints them only for 1000 nonces with seed 0:
   - v1: `10b649a3f15c7c7f88277812f2e74b337a0f20ce909af09199cccb960771cfa1`
   - v2: `b85d79e080b10b6ad28c2e6c993601a1361917dba979e03a0a8f7248aaf4ba52`
2. **Every reported hash is recomputed.** rxbench reports three kinds of hash:
   - in each `trial` and `sample_end` record, thread 0's check hash of nonce 0, computed with that run's own VM before the barrier;
   - in the same records, each thread's first and last `[nonce, hash]` in the window;
   - in each full-dataset `init` record, the fast-mode check hash.

   `bench.Verifier` recomputes all of them in light mode through `rxlab.binding`, using 4 Python threads and memoising on `(version, nonce)`. It raises `VerificationError` on any mismatch. A RandomX hash depends only on the key, the input and the version, never on the mode, JIT, AES or batch flags, so a light-mode recomputation checks every configuration. Verification starts only after the timed commands have finished. In the experiments and the tuner it runs after the rxbench process has exited, so the verifier's own CPU load never overlaps a timed window. Every result file records `hashes_checked`, `distinct_inputs`, `mismatches` and `method`.
3. **Batch-pipeline integrity.** Each thread walks a range of consecutive nonces that no other thread uses. So `last − first == hashes` for each thread proves that the `first/next/last` pipeline neither drops nor repeats an input. The verified hashes prove that each output is attributed to the right nonce.
4. **Dataset digests.** `tune.check_digests` requires every init of the same slice size to produce the same digest, whatever the thread count, and raises `VerificationError` otherwise.
5. **Cross-library check.** In the library arm of `bench_v1_vs_v2`, v1 hashes produced by the v1.2.1 library are checked against v1 hashes from the v2.0.1 library.

### What has been verified so far

- **Upstream reference.** `rxbench verify` reproduces both upstream reference results in light and fast mode, for v1 and v2, on the v2.0.1 library (`test_upstream_reference_result`, a slow test). The same check was run by hand in light mode for the interpreter/soft-AES/single-call path and for v1 on the v1.2.1 library.
- **Other configs.** `test_verify_xor_matches_binding` checks that the XOR over nonces 0–15 matches `rxlab.binding` for four configs, one of which is v2 with the interpreter, soft AES and single calls.
- **Input format.** rxbench's 76-byte template is byte-identical to upstream's `blockTemplate_`, and the nonce offset (39) and seed encoding match upstream.
- **Smoke-run hashes.** Every hash reported by the `--smoke` runs was recomputed with 0 mismatches. In one complete set of smoke runs, the counts were 16, 12, 29, 52, 5 and 46 hashes for thread_scaling, mode_matrix, qos_placement, v1_vs_v2, sustained and tune. The tune count varies from run to run, because it depends on which candidates survive the halving and on whether the winner is the baseline. The v1_vs_v2 count includes v1.2.1-library hashes checked against the v2.0.1 library.
- **Full-run hashes.** The full (non-smoke) bench_thread_scaling run ([results/bench_thread_scaling.json](../results/bench_thread_scaling.json)) recomputed all 1008 hashes reported by its 72 trials, with 0 mismatches.
- **Dataset digests.** Digests are identical across 1–4 init threads on a 1/256 slice (dataset_init smoke run), and between 1 and 3 threads on a 1/1024 slice (`test_dataset_init_digest_is_thread_invariant`).
- **Batch pipeline.** No nonce is lost (`test_light_sweep_is_verified` asserts `hashes == last − first` per thread).
- **CPU topology.** The IORegistry maps logical CPUs 0–5 to the E-cluster and 6–9 to the P-cluster, and every result file, smoke or full, records this mapping.
- **Tests.** `pytest -m "not slow"` runs 30 tests. `pytest -m slow` runs 12 more: the upstream reference in both modes for both versions, a verified fast-mode sweep with a full-init check, and each experiment's `--smoke` run.

## Thread placement on macOS

macOS has no hard thread affinity. The only per-thread lever is the QoS class, which each worker sets at start-up with `pthread_set_qos_class_self_np`. Background QoS keeps a thread on the E-cluster. Higher classes prefer the P-cluster but may spill onto E-cores. The harness therefore measures placement rather than assuming it:

- **Actual QoS class.** After setting its class, each thread reads back the class actually in effect (`qos_class_self`) and reports it as `per_thread[].qos`. In the recorded runs, threads left at `unset` read back as `default`.
- **CPU per hash.** After every hash, rxbench samples the logical CPU with `pthread_cpu_number_np`. Trials keep a per-thread histogram, and samples keep per-interval counts across all threads.
- **CPU-to-cluster map.** `bench.cpu_clusters()` reads the IORegistry device tree (`ioreg -a -l -p IODeviceTree -r -n cpus`) and maps each CPU node's `logical-cpu-id` to its `cluster-type` (`P` or `E`).
- **Cluster shares.** `bench.cluster_share()` turns a CPU histogram into P and E shares. `summarize_trials` reports these per thread as `thread_cluster_share`, and every result file stores the CPU-to-cluster map as `cpu_clusters`.

The cluster share counts hashes, not time: a hash counts toward the CPU it finished on.

In one set of smoke runs (measured on a loaded machine; not a benchmark result), background-QoS threads finished hashes only on E-cluster CPUs, while the user_interactive thread of the `P x1` layout finished 59% of its hashes on E-cores. QoS is a preference, so placement results mean something only on an idle machine.

Off macOS, `unset` is the only QoS class, and rxbench exits on any other. On Linux the CPU number comes from `sched_getcpu`.

## Contamination diagnostics

Every trial and every sample carries counters that make a disturbed window visible after the fact.

| Field | Scope | Source |
|---|---|---|
| `vm.minflt`, `vm.majflt` | process | `getrusage` |
| `vm.decompressions`, `vm.swapins` | system-wide | `host_statistics64(HOST_VM_INFO64)` |
| `thermal` | system | `NSProcessInfo.thermalState`: 0 nominal, 1 fair, 2 serious, 3 critical, −1 unknown. Trials record it once the workers are ready and again after they stop; samples record one value. |

`bench.paged(rec)` is true when the window saw a major fault or a swap-in. `bench.decompressed(rec)` is true when the memory compressor decompressed pages. Per config, `summarize_trials` counts `paging_trials`, `decompression_trials` and `incomplete_trials`. The sustained analysis counts `samples_with_paging` and `samples_with_decompressions`. These flags are diagnostics: they are counted and reported, and trials are never dropped because of them.

Every result JSON also embeds `sysinfo.describe()`:

- OS, architecture, Python version and logical CPU count;
- on macOS, the CPU brand, model, memory size, page size and power source;
- for each performance level, the core count, L1d size, L2 size and CPUs per L2.

`bench_dataset_init` records the power source again, and `bench_sustained` records it at the end of its run.

## Statistics

[rxlab/stats.py](../rxlab/stats.py) uses percentile bootstrap intervals at 95%, with 10,000 resamples and a fixed seed (12345), so the same trials always give the same intervals. Inputs must be non-empty and finite, and a `ValueError` is raised otherwise. `scipy.stats` is imported only inside the functions that run a rank test.

| Function | Used for | Point estimate and interval | Test |
|---|---|---|---|
| `summarize(values)` | per-config summaries | n, median, mean, sd, cv, min and max, plus a bootstrap interval of the median (`ci95`) | none |
| `compare(base, other)` | independent samples | `median(other) / median(base)`, with an interval from bootstrapping each sample independently | two-sided Mann–Whitney U |
| `compare_paired(base, other)` | paired trials | median of the per-pair ratios `other_i / base_i`, with an interval bootstrapped over pairs, plus `pairs_other_faster` | two-sided Wilcoxon signed-rank on the log ratios, skipped when there is one pair or all log ratios are zero |
| `window_mean(t, y, start, end)` | burst and sustained windows | time-weighted mean of the samples whose interval midpoint lies in `[start, end)` | none |

**Paired vs unpaired.** When two arms run back to back, as in the ABBA pairs of `bench_v1_vs_v2` or the interleaved final of the tuner, any slow drift is common to both members of a pair and cancels in the per-pair ratio, so the paired comparison is the primary one. The unpaired comparison treats that drift as noise. The tuner and `bench_v1_vs_v2` report both.

**Small samples.** With 2 or 3 values, the bootstrap interval of a median collapses to exactly `[min, max]`. That interval covers the true median only `1 − 2^(1−n)` of the time, and the rank tests cannot reach conventional significance at these sizes:

| Values per arm | Where | What `ci95` actually is | Smallest attainable two-sided p |
|---|---|---|---|
| 1 | tuner round 1 | `[x, x]` | no test |
| 2 | dataset_init; tuner round 2 | `[min, max]`, 50% coverage | no test (dataset_init reports summaries only) |
| 3 | thread_scaling, qos_placement, mode_matrix; tuner round 3; tuner init final (3 pairs) | `[min, max]`, 75% coverage | 3 vs 3 Mann–Whitney: 0.1; 3 Wilcoxon pairs: 0.25 |
| 6 pairs | tuner hashing final; v1_vs_v2 1-thread and library arms | bootstrap interval | Wilcoxon: 1/32 |
| 10 pairs | v1_vs_v2 best-thread arm | bootstrap interval | below 0.05 is attainable |

Read the n = 2 and n = 3 intervals as ranges, not as 95% intervals. Only the tuner's hashing final and `bench_v1_vs_v2` can reach p < 0.05.

## Autotuner

[rxlab/tune.py](../rxlab/tune.py) runs successive halving in a single rxbench process, so the dataset is built once, with N init threads. It verifies every hash from every round when it finishes.

### Hashing configuration

The candidate space is thread count × QoS placement × hashing API, in fast mode (v1 by default).

- **Thread counts:** `{P, P + max(1, ⌊E/3⌋), P + ⌊E/2⌋, N − 1, N, N + 2}`, where N is the number of logical CPUs. On 4 P + 6 E this gives 4, 6, 7, 9, 10 and 12.
- **Placements:** `unset` and all-`ui`. Above P threads the tuner also tries `ui*P+bg` and `ui*P+ut`, which put P threads at user_interactive and the rest at background or utility.
- **API:** batch (`first/next/last`) and single-call.

That gives 44 candidates on 4 P + 6 E. Each round runs every survivor in interleaved order, ranks them by median H/s and keeps the top ⌈k/η⌉, with η = 3. The last round keeps one.

| Round | Trials | Warm-up | Measure | Candidates on 4 P + 6 E |
|---|---|---|---|---|
| 1 | 1 | 1.0 s | 3.0 s | 44 |
| 2 | 2 | 1.0 s | 4.0 s | 15 |
| 3 | 3 | 1.5 s | 6.0 s | 5 |
| final | 6 pairs | 1.5 s | 8.0 s | winner vs baseline |

The final runs in fresh trials, so the reported ratio carries no selection bias. It pits the winner against the baseline: all logical CPUs, QoS `unset`, batch API, same mode. Interleaving makes trial i of each config a pair. The result holds both the paired and the independent comparison, the two summaries, and `winner_is_baseline`.

### Dataset-init threads

The init tuner runs the same procedure over init thread counts 1..N, where lower median time wins and η = 2:

1. Round 1 runs 1 trial of each thread count on 1/32 of the dataset and keeps the fastest ⌈k/2⌉.
2. Round 2 runs 2 trials on 1/8 of the dataset and again keeps ⌈k/2⌉. The fastest survivor is the winner. On N = 10 the field shrinks from 10 to 5 to 3.
3. The winner then meets N threads in 3 pairs of full inits, reported as a paired time ratio (winner / baseline). If the winner is N itself, only N runs.

`check_digests` then checks every init record from all rounds.

`--smoke` shrinks the tuner to 1 or 2 threads, light mode and the batch API only (6 candidates), with sub-second windows and dataset slices of 1/512 and 1/128.

## Experiments

Each experiment accepts `--smoke` (a run of 30 s or less in light mode, written to `data/cache/smoke/`), `--estimate` (prints its estimated seconds and exits) and `--tag`. A full run writes `results/bench_<name>.json` and `figures/bench_<name>.png`. Each JSON holds the machine description, the CPU-to-cluster map, the rxbench `setup` records (cache and dataset build times), the verification report, the settings, the analysis and every raw trial, init or sample record.

| Experiment | Question | Configs (4 P + 6 E) | Trials × (warm-up + measure) | Comparison | Estimate |
|---|---|---|---|---|---|
| [bench_thread_scaling](../experiments/bench_thread_scaling.py) | hashrate against thread count, v1 and v2 | 1–12 threads × v1/v2, fast mode, JIT, QoS `unset` (24 configs) | 3 × (2.0 + 5.0 s) | per-config summaries | 536 s |
| [bench_qos_placement](../experiments/bench_qos_placement.py) | P-only vs E-only vs mixed placement | 16 layouts, v1, fast mode | 3 × (1.5 + 5.0 s) | per-config and per-thread summaries, P+E additivity | 336 s |
| [bench_mode_matrix](../experiments/bench_mode_matrix.py) | what each implementation choice is worth (v1) | 2^4 factorial at 1 thread and at the best thread count (32 configs) | 3 × (1.5 + 4.0 s) | unpaired, one factor flipped | 567 s |
| [bench_v1_vs_v2](../experiments/bench_v1_vs_v2.py) | v2 vs v1; v1 through v1.2.1 vs v2.0.1 | 3 two-arm comparisons | 10 / 6 / 6 ABBA pairs × (1.5 + 5.0 s) | paired, with unpaired also reported | 319 s |
| [bench_dataset_init](../experiments/bench_dataset_init.py) | init time against thread count | 1–10 threads, full dataset | 2 trials | per-thread-count summaries, speedup | 237 s |
| [bench_tune](../experiments/bench_tune.py) | best configuration overall | 44 candidates, then the init tuner | see [Autotuner](#autotuner) | paired final | 653 s |
| [bench_sustained](../experiments/bench_sustained.py) | burst vs sustained rate | best config, 1200 s, 10 s samples | one run | window means, xmrig score emulation | 1210 s |

The estimates come from each script's `--estimate` and assume an idle machine. They are built from:

- a fixed 10 s per rxbench process, an idle-M4 allowance for cache build, dataset build and verification;
- configs × trials × (warm-up + measurement + 0.3 s) for each sweep;
- for bench_dataset_init, a guessed 30 s single-thread full init, divided by min(t, N/2) at t threads, plus 1.5 s per full init;
- for bench_tune, a guessed 60 s for the init tuner;
- for bench_sustained, the run length.

### bench_thread_scaling

- **Configs.** 1 to 12 threads for v1 and v2. On a 10-CPU machine this includes two oversubscribed points. All 24 configs run interleaved in one process.
- **QoS.** It is left `unset`, which is what a plain miner gets from the scheduler.
- **Analysis.** For each version it reports the best thread count (highest median), its speedup over 1 thread, and the per-thread rate curve. The figure marks the P-core count and the all-CPU point.
- **Downstream use.** `bench_mode_matrix` and `bench_v1_vs_v2` read the best v1 thread count from this experiment's result file. They fall back to all logical CPUs if it is missing or came from a smoke run.

### bench_qos_placement

On 4 P + 6 E, the layouts are:

| Layout | QoS | Purpose |
|---|---|---|
| `P x1` … `P x4` | `ui` | P-cluster scaling curve |
| `E x1` … `E x6` | `bg` | E-cluster scaling curve |
| `P x4 + E x2`, `P x4 + E x6` | `ui*4+bg` | P+E mixes |
| `P x4 + utility x6` | `ui*4+ut` | E threads at a class other than background |
| `all ui x10`, `unset x10`, `unset x4` | `ui`, `unset` | uniform-class references |

- **E-cluster curve.** It tests whether the E-cluster's shared L2 limits how many 2 MiB scratchpads it can serve.
- **Additivity.** `median(P x4 + E x6) / (median(P x4) + median(E x6))` tests whether P and E throughput add.
- **Per-cluster thread rates.** Thread rates are pooled by cluster, using only threads with at least 90% of their sampled hashes on one cluster.
- **No E-cores.** On a CPU without E-cores, `best_e_only` is `None`.

### bench_mode_matrix

- **Configs.** A full factorial of fast/light × JIT/interpreter × hard/soft AES × batch/single for v1, at 1 thread and at the best thread count, run interleaved.
- **Effects.** Each factor's effect is the ratio of the config with that one factor flipped to the reference config (fast, JIT, hard AES, batch) at the same thread count. It is reported with a bootstrap interval and a Mann–Whitney test (`stats.compare`).
- **Figure.** A dot plot with intervals on a log axis.
- **Smoke run.** The light-mode reference and its three one-factor flips, at 1 thread.

### bench_v1_vs_v2

The experiment runs three comparisons, each as pairs:

- **v2 vs v1, best thread count** (10 pairs): both versions on the v2.0.1 library in one process, sharing one dataset.
- **v2 vs v1, 1 thread** (6 pairs): the same, at 1 thread.
- **Library** (6 pairs, best thread count): v1 through the v1.2.1 library, which is what Monero runs, vs v1 through the v2.0.1 library. This comparison uses two rxbench processes kept alive side by side, each with its own dataset.

Each pair runs both arms back to back. Pairs 0, 2, 4, … run A then B, and pairs 1, 3, 5, … run B then A, so the sequence is A B B A A B …. Each arm is a separate one-trial sweep, so threads and VMs are recreated for every trial. Because the order alternates, order effects and linear drift bias successive per-pair ratios B/A in opposite directions and cancel across pairs. The result holds the paired and unpaired comparisons and every per-pair ratio. The program sizes (256 for v1, 384 for v2) are recorded next to the results.

### bench_dataset_init

- **Thread counts.** Full 2080 MiB inits with 1 to N threads, interleaved within each of 2 trials, after one untimed warm-up init of 1/64 of the dataset.
- **Timing.** Each init zeroes its range first, as described in [Dataset-init timing](#dataset-init-timing).
- **Correctness.** All digests must be identical, and the fast-mode check hash of every full init must match light mode.
- **Analysis.** Median time and interval, items/s, speedup over 1 thread and parallel efficiency per thread count.
- **Amortised cost.** The best time is expressed as a share of one key epoch. The RandomX README gives a new key every 2048 blocks, and Monero's block target is 120 s, so an epoch is 2048 × 120 = 245,760 s.

### bench_tune

This experiment runs `rxlab.tune` (see [Autotuner](#autotuner)) and plots three panels: the halving rounds, the hashing winner against the baseline, and the init-thread winner against all CPUs.

### bench_sustained

- **Config.** The experiment hashes one configuration continuously for 1200 s and samples every 10 s. The configuration is the `bench_tune` winner. If there is no tuner result, it is the best thread count from `bench_thread_scaling`, and failing that, all logical CPUs.
- **Samples.** Each sample holds the aggregate rate, the share of hashes that finished on P-cores, the thermal state and the contamination counters.
- **Burst.** The time-weighted mean over the first 90 s.
- **Sustained.** The time-weighted mean over the last 300 s.
- **Haircut.** `1 − sustained / burst`.
- **Other outputs.** The thermal states seen, the time of the first non-nominal state, contamination counts, the power source at the end of the run and the emulated xmrig scores.
- **Options.** `--minutes M` overrides the run length.

### Run order and heat

`run_measurements.sh` runs the experiments in this order:

1. thread_scaling
2. qos_placement
3. mode_matrix
4. v1_vs_v2
5. dataset_init
6. tune
7. sustained

There is a 20 s pause after each of the first six experiments and a 180 s cool-down before the sustained run. The order follows the data dependencies: thread_scaling supplies the best thread count, and tune supplies the sustained configuration. The 20-minute sustained run goes last, after the cool-down.

Heat is controlled at two levels, because the machine is fanless:

- **Within an experiment**, trials are interleaved or paired, so drift spreads across configs or cancels in per-pair ratios.
- **Between experiments**, nothing equalises heat soak. Absolute rates from different experiments are therefore not directly comparable (see [Known limitations](#known-limitations-and-open-questions)). Each trial's `thermal` field records the thermal state.

## xmrig `--bench` scoring emulation

xmrig `--bench=N` (N = 250K, 500K, or 1M to 10M) scores a fixed amount of work. The score is N divided by the time from when the workers start until the last worker finishes. This description comes from reading xmrig's source (`master` at `b2ca72480c58`):

- `BenchClient::onBenchDone` computes `dt = ts − m_readyTime`.
- `BenchState::done` keeps the latest finish time over all workers.
- `BenchConfig::getSize` allows only 1M to 10M, 250K and 500K.
- `m_readyTime` is set in `BenchState::start`, which `Workers::start` calls right after launching the worker threads. The CPU backend receives its job only through `Miner::onDatasetReady` → `handleJobChange`.

So dataset init is outside xmrig's clock, while worker thread start, VM creation and xmrig's self-test are inside it.

`bench_sustained.fixed_work_score(t_end, hashes, n)` emulates this score from the sustained run's cumulative hash trace:

1. Find the sample in which the cumulative count reaches `n`.
2. Interpolate linearly within that sample to get the time `t` at which the count reached `n`.
3. Report `n / t`. A size the run never reaches is reported as `null`.

Scores are computed for 250K, 500K, 1M and 10M hashes and drawn on the sustained figure. This shows how much of a published xmrig figure comes from burst. The trace starts after VM creation, so the emulation leaves out VM creation and self-test, which xmrig's clock includes. That is a fixed overhead, and it matters less as N grows. `test_xmrig_fixed_work_score` checks the emulation against values worked out by hand from the definition.

## Running

The harness needs:

- macOS on Apple Silicon for bench_qos_placement and the tuner, because rxbench accepts QoS classes other than `unset` only on macOS;
- clang;
- git and cmake, to fetch and build RandomX;
- Python 3.9 or later with numpy, scipy and matplotlib.

```
scripts/build_randomx.sh                  # fetch and build RandomX v1.2.1 and v2.0.1 into third_party/
tools/build_rxbench.sh                    # build/rxbench-v1.2.1 and build/rxbench-v2.0.1
experiments/run_measurements.sh           # full measurement run; idle machine, AC power, Low Power Mode off
experiments/run_measurements.sh --smoke   # every experiment's --smoke run
python3 experiments/bench_qos_placement.py --smoke     # any single experiment
python3 experiments/bench_qos_placement.py --estimate  # its estimated runtime in seconds
python3 -m rxlab.bench reference --mode fast           # upstream reference check, v1 and v2
python3 -m rxlab.bench sweep v=1,mode=fast,jit=1,aes=hard,batch=1,threads=4,qos=ui --trials 5 --out my_sweep   # writes results/my_sweep.json
python3 -m rxlab.tune --smoke
build/rxbench-v2.0.1 verify --nonces 1000 v=2,mode=light,threads=4   # prints upstream's v2 reference
python3 -m pytest tests/test_stats.py tests/test_bench.py -m "not slow"
python3 -m pytest tests/test_stats.py tests/test_bench.py -m slow
```

`run_measurements.sh` goes through these steps:

1. Build any missing RandomX library, and build rxbench whenever its binary is missing or older than `tools/rxbench.c`.
2. Hold a `caffeinate -i` assertion for the lifetime of the script.
3. Print the machine description and the 1-minute load average, which should be well below 1. Warn if Low Power Mode is on.
4. Run the upstream-reference preflight in light mode, and also in fast mode except under `--smoke`. Exit non-zero on a mismatch.
5. Print each experiment's estimate and the total.
6. Run the experiments with the pauses and cool-down described above.

The environment variables `PYTHON`, `PAUSE` (default 20) and `COOLDOWN` (default 180) override the defaults. `--smoke` sets `PAUSE` and `COOLDOWN` to 0.

| Part | Estimated seconds |
|---|---|
| thread_scaling | 536 |
| qos_placement | 336 |
| mode_matrix | 567 |
| v1_vs_v2 | 319 |
| dataset_init | 237 |
| tune | 653 |
| sustained | 1210 |
| pauses (6 × 20 s) | 120 |
| cool-down | 180 |
| **total** | **4158 (about 69 min), plus about a minute of preflight** |

**Smoke mode uses light mode.** Building the 2080 MiB dataset with 4 threads took 11.5 to 25 s (measured on a loaded machine; not a benchmark result), up to 25 s of the 30 s smoke budget before any hashing. So every `--smoke` run uses light mode (the 256 MiB cache only), 4 init threads and tiny time budgets, and writes to the git-ignored `data/cache/smoke/`. The fast-mode path is covered instead by the slow tests and by the fast-mode reference preflight. Downstream experiments ignore smoke results when they look up the best thread count or the tuned configuration.

## Development notes

These are the notable bugs found while building the harness, and how each was fixed.

- **Second dataset allocation.** `init` originally allocated its own 2080 MiB dataset next to the one used for hashing, which broke the one-dataset-per-process design. It now reuses the process dataset and tracks whether the dataset is valid with a flag.
- **Start-up deadlock in the Python wrapper.** The wrapper waited for an `end` record that `serve` prints only when it exits. It now reads just the `start` line.
- **Division by zero in the tuner's smoke run.** A light-mode trial on the overloaded machine timed zero hashes, so a ratio became non-finite and `stats` raised `ValueError: non-finite value`. This was fixed at the source: rxbench stretches the window until each thread has a fully timed hash, and flags a failure with `complete: false`.
- **Near-zero rates after dataset init.** Single-thread fast-mode trials immediately after dataset init ran at near-zero rates, while later trials in the same process ran normally (measured on a loaded machine; not a benchmark result). The cause was not established. Memory pressure is suspected, because the system was decompressing compressed pages at the time. Per-trial fault, decompression and swap-in counters were added so such trials show up in the measurement run. The observation was never saved to a file and cannot be re-checked.
- **Unreported decompression counter.** rxbench recorded decompressions, but `paged()` ignored them and no summary reported them. `bench.decompressed()`, `decompression_trials` and `samples_with_decompressions` now report them. On the loaded machine every smoke trial was flagged, because the counter is system-wide.
- **Silent truncation in rxbench.** Three limits cut input without warning:
  - A `serve` line kept only its first 256 tokens, so a sweep of more than about 245 configs would have silently run fewer.
  - Config and QoS strings were cut to fit fixed buffers.
  - `--threads-list` stopped at 64 entries.

  Each case now exits with an error message (`copy_arg` plus explicit checks).
- **Slow first init in a process.** The first dataset init in a process was slower than later ones: 0.34 s vs 0.13 s for 1/256 of the dataset at 1 thread (measured on a loaded machine; not a benchmark result). `bench_dataset_init` now runs an untimed warm-up init first.
- **Smoke budget.** A 4-thread dataset build took 11.5 to 25 s (measured on a loaded machine; not a benchmark result), so the smoke runs moved to light mode.
- **Bootstrap seed test.** A test expected the bootstrap interval of a median of 12 values to change with the seed. It did not, because bootstrap medians land on order statistics. The seed-dependence test now uses the mean.
- **Crash without E-cores.** `bench_qos_placement.analyse` called `max()` on an empty curve on a CPU with no E-cores. `best_e_only` is now `None` in that case.
- **bash 3.2 and `set -u`.** macOS bash 3.2 treats an empty array as unbound under `set -u`, which broke `run_measurements.sh`. The smoke flag is now a plain string.
- **Fresh checkout.** `third_party/` is git-ignored, and `build_rxbench.sh` exits when the libraries are missing, so `run_measurements.sh` could not start from a fresh checkout. The runner now calls `scripts/build_randomx.sh <tag>` for any missing library before it builds rxbench.
- **Slow import.** The `scipy.stats` import in `stats.py` was deferred into the functions that use it. The import took 1.5 to 2 s (measured on a loaded machine; not a benchmark result).
- **Figures.** The mode-matrix bars on a log axis had meaningless lengths, so the chart became a dot plot with intervals. The palette's second and third colours fail a colour-vision adjacency check, so the benchmark figures use only the first two palette colours and grey.
- **Test coverage.** Tests were added for paths that had been untested or checked only by hand: the xmrig score emulation, the contamination flags, the small-sample interval, and the v2 interpreter + soft-AES + single-call path of `verify`.

## Known limitations and open questions

### Limitations

- **Small-sample intervals are ranges, not 95% intervals.** thread_scaling, qos_placement and mode_matrix use 3 trials per config, and dataset_init uses 2. At n = 3 the bootstrap "ci95" of the median is exactly `[min, max]`, with 75% coverage, and at n = 2 it has 50% coverage. With 3 vs 3 trials, mode_matrix's Mann–Whitney p can never go below 0.1, however large the effect. The figures still label these intervals "95% CI". More trials would lengthen the run past its current estimate of about 69 minutes.
- **mode_matrix uses the unpaired comparison.** Its trials are interleaved, so they could be paired by trial index, which would cancel drift. It uses `stats.compare` instead.
- **The tuner's finals are not order-balanced.** In both the hashing final and the init final, the baseline runs before the winner in every pair. `bench_v1_vs_v2` uses ABBA order, but the tuner does not alternate, so any order effect biases its ratios in one direction.
- **Heat soak between experiments.** On a fanless machine, a 20 s pause does not return the machine to the same thermal state. Interleaving and pairing protect ratios within an experiment, but absolute rates from different experiments are not comparable. For example, the tuner's baseline (v1, all CPUs, QoS `unset`, batch) is the same configuration as thread_scaling's v1 all-CPU point, but it runs later in the sequence, after more heat soak. `thermalState` is recorded, but it is a coarse four-level signal.
- **Contamination flags use system-wide counters.** Decompressions and swap-ins count activity from every process, so `paged` and `decompressed` mean something only on an idle machine. The flags are reported, and trials are never excluded because of them. `run_measurements.sh` prints the load average and warns about Low Power Mode, but it does not refuse to run.
- **Runtime estimates are partly guessed.** The dataset_init estimate rests on a guessed 30 s single-thread full init on an idle M4, and the tune estimate on a guessed 60 s of init tuning. Every estimate's 10 s per-process allowance is meant to cover verification too, but the light-mode verification pass after each experiment is itself estimated at 10 to 20 s, so the totals are probably low.
- **Linux is untested.** rxbench and `build_rxbench.sh` have Linux code paths (`sched_getcpu`, an `$ORIGIN` rpath), but nothing has been run there:
  - Off macOS, rxbench rejects every QoS class except `unset`. That breaks both `bench_qos_placement` and the tuner, whose candidate set always includes `ui` placements.
  - The thermal state reads −1, decompression and swap-in counters stay 0, and `cpu_clusters()` returns an empty map.
- **Byte-identical datasets are shown only for slices.** The development runs checked slices of 1/256 and 1/1024. Full-size inits are compared across thread counts only by the measurement-phase dataset_init and tune runs (`check_digests`). Otherwise, a full init's correctness rests on one fast-vs-light hash.
- **The xmrig emulation omits VM creation and self-test,** both of which xmrig's clock includes.
- **Placement is counted by hashes, not time.** The CPU number is sampled when a hash completes.
- **Not measured:** large pages, battery vs AC power (the power source is only recorded) and Low Power Mode.

### Open questions for the measurement run

- Does the E-cluster's shared 4 MiB L2 cap the useful E-thread count near two, given 2 MiB scratchpads? The E-cluster curve in `bench_qos_placement` answers this.
- Do P and E throughput add, or do they contend for memory bandwidth or the system-level cache? See `additivity` in `bench_qos_placement`.
- Background QoS is also a priority policy, and possibly a clock policy, not only a placement hint. Does that make "E-only via background" understate the E-cluster? The check is to compare its per-thread E rates with those of the utility-QoS E threads in the `P x4 + utility x6` layout.
- How large is the fanless burst-to-sustained haircut, and when does `thermalState` leave nominal? `bench_sustained` answers both.
- Is the near-zero-rate-after-init effect (see [Development notes](#development-notes)) caused by memory pressure, and does it vanish on an idle machine? The `vm` counters will show this.
- Is v1 any faster or slower through the v2.0.1 library than through v1.2.1? The library arm of `bench_v1_vs_v2` answers this.
