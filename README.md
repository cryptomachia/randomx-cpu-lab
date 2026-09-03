# randomx-cpu-lab

Is there a better way to solve RandomX — Monero's proof-of-work — on a CPU?

RandomX was designed so that the answer is "no": every hash runs randomly
generated programs over a 2 MiB scratchpad and a 2 GiB dataset, so a general
purpose CPU is already the machine it targets. This repo tests that claim
empirically instead of taking it on faith:

- **Where does CPU throughput actually come from?** Measure every knob the
  reference library exposes (JIT, hardware AES, fast vs light mode, batch
  hashing, thread placement) on real hardware, with repeated trials and
  confidence intervals.
- **Do the classic "shortcut" ideas work?** Smarter nonce orders, skipping
  expensive nonces, early rejection, precomputation — each gets a direct test.
- **What does RandomX v2 change?** tevador/RandomX v2.0.1 ships next to v1.

## Layout

| path | what |
| --- | --- |
| `rxlab/binding.py` | ctypes wrapper for `librandomx` (cache, dataset, VM, batch API) |
| `rxlab/spec/` | pure-Python RandomX written from `doc/specs.md`: Argon2d, SuperscalarHash, dataset items, AES generators, program decoding, IEEE-754 rounding modes, the VM |
| `tools/golden/` | C++ harnesses that dump reference values from the C library |
| `data/golden/` | those dumps; the Python tests compare against them |
| `tools/rxbench.c` | native benchmark driver: one dataset init, then a sweep of configurations, one JSON line per trial |
| `rxlab/bench.py`, `rxlab/stats.py`, `rxlab/tune.py` | sweep runner with checksum verification, bootstrap statistics, successive-halving autotuner |
| `experiments/bench_*.py` | thread scaling, mode matrix, QoS placement, v1 vs v2, dataset init, sustained (thermal) runs |

## Setup

```sh
pip install --user cmake numpy scipy matplotlib gmpy2 pytest
scripts/build_randomx.sh        # clones + builds tevador/RandomX v1.2.1 and v2.0.1
tools/build_rxbench.sh          # builds build/rxbench-<tag>
python3 -m pytest -q -m "not slow"
```

Measurements: `experiments/run_measurements.sh` runs the whole benchmark suite
(about an hour). Run it on an idle machine on AC power; every experiment also
takes `--smoke` for a 30-second check.

Do not build with `-DARCH=native` on Apple Silicon: Apple clang's
`-march=native` does not define `__ARM_FEATURE_CRYPTO`, the library compiles
with `HAVE_AES=0`, and every hardware-AES VM fails to allocate.
