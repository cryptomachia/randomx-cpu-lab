"""Pure-Python reference implementation of RandomX, written from doc/specs.md.

Slow by design: a light-mode hash takes seconds, and each new key's 256 MiB
cache tens of seconds more (`python3 -m rxlab.spec` prints the breakdown). It
exists to be read and to be checked against the C library, not to hash for real.
"""
