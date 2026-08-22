"""IEEE-754 binary64 arithmetic under the four rounding modes of `fprc` (specs.md 4.3, 5.3).

Python floats always round to nearest-even, which is fprc = 0, so that mode uses
plain float arithmetic. The directed modes go through MPFR (gmpy2) in an exact
binary64 context: 53-bit precision, the binary64 exponent range and
subnormalization. Overflow, gradual underflow, denormals and the sign of an
exact zero therefore come out as an IEEE-754 FPU with FTZ/DAZ off produces
them, as in arm64 RandomX builds. x86 builds set FTZ/DAZ, which differs only
on denormals, and specs.md 5.3 rules those out for real programs.
"""
import math
import struct

import gmpy2

ROUND_NEAREST, ROUND_DOWN, ROUND_UP, ROUND_ZERO = range(4)  # fprc values, specs.md Table 4.3.1


def _context(rounding):
    ctx = gmpy2.ieee(64)
    ctx.round = rounding
    return ctx


# Indexed by fprc. gmpy2 numbers its rounding modes differently, so map by name.
_CONTEXTS = [_context(r) for r in (gmpy2.RoundToNearest, gmpy2.RoundDown,
                                   gmpy2.RoundUp, gmpy2.RoundToZero)]
ADD = [c.add for c in _CONTEXTS]
SUB = [c.sub for c in _CONTEXTS]
MUL = [c.mul for c in _CONTEXTS]
DIV = [c.div for c in _CONTEXTS]
SQRT = [c.sqrt for c in _CONTEXTS]


def add(x, y, mode=ROUND_NEAREST):
    return x + y if mode == ROUND_NEAREST else float(ADD[mode](x, y))


def sub(x, y, mode=ROUND_NEAREST):
    return x - y if mode == ROUND_NEAREST else float(SUB[mode](x, y))


def mul(x, y, mode=ROUND_NEAREST):
    return x * y if mode == ROUND_NEAREST else float(MUL[mode](x, y))


def div(x, y, mode=ROUND_NEAREST):
    """x / y; RandomX divisors are group-E values, never zero."""
    return x / y if mode == ROUND_NEAREST else float(DIV[mode](x, y))


def sqrt(x, mode=ROUND_NEAREST):
    return math.sqrt(x) if mode == ROUND_NEAREST else float(SQRT[mode](x))


_D = struct.Struct("<d")
_Q = struct.Struct("<Q")


def to_bits(x):
    return _Q.unpack(_D.pack(x))[0]


def from_bits(q):
    return _D.unpack(_Q.pack(q))[0]


FSCAL_MASK = 0x80F0000000000000
# Keeps the fraction and the low 4 exponent bits of a group E value (common.hpp).
DYNAMIC_MANTISSA_MASK = (1 << 56) - 1


def fscal(x):
    """FSCAL_R: XOR with 0x80F0000000000000, flipping the sign and the low 4 exponent bits.

    For normal x that is -x * 2**k with k odd in [-15, 15]. As a pure bit
    operation it is exact in every rounding mode and maps zero, denormals and
    infinities exactly as the hardware XOR does."""
    return from_bits(to_bits(x) ^ FSCAL_MASK)


def convert_e(v, emask):
    """specs.md 4.3.2: int32 -> group E value (positive, exponent and low fraction bits forced)."""
    return from_bits((to_bits(float(v)) & DYNAMIC_MANTISSA_MASK) | emask)

