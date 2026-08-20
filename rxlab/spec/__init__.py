"""Pure-Python reference implementation of RandomX, written from doc/specs.md.

Slow by design (one light-mode hash takes minutes); it exists to be read and
to be checked against the C library, not to hash for real.
"""
