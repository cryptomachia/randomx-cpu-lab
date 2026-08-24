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
| `rxlab/spec/` | pure-Python RandomX written from `doc/specs.md`: AES generators, program decoding, IEEE-754 rounding modes, the VM |
| `tools/golden/` | C++ harnesses that dump reference values from the C library |
| `data/golden/` | those dumps; the Python tests compare against them |

The Python VM reproduces the official v1 test vectors when it reads dataset
items from the C library's dataset (`CDatasetItemSource`). The cache and
dataset half (Argon2d, SuperscalarHash) is next.

## Setup

```sh
pip install --user cmake numpy scipy matplotlib gmpy2 pytest
scripts/build_randomx.sh        # clones + builds tevador/RandomX v1.2.1 and v2.0.1
python3 -m pytest -q -m "not slow"
```

Do not build with `-DARCH=native` on Apple Silicon: Apple clang's
`-march=native` does not define `__ARM_FEATURE_CRYPTO`, the library compiles
with `HAVE_AES=0`, and every hardware-AES VM fails to allocate.
