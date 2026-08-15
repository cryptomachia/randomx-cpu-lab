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

## Setup

```sh
pip install --user cmake numpy scipy matplotlib pytest
scripts/build_randomx.sh        # clones + builds tevador/RandomX v1.2.1 and v2.0.1
python3 -m pytest -q
```

`rxlab.binding` wraps `librandomx` with ctypes. Foreign calls release the GIL,
so plain Python threads that each own a VM hash in parallel.
