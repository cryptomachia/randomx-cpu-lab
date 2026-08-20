"""RandomX parameters (configuration.h, v1.2.1 / v2.0.1) and shared helpers."""

ARGON_MEMORY = 262144  # KiB -> 256 MiB cache
ARGON_ITERATIONS = 3
ARGON_LANES = 1
ARGON_SALT = b"RandomX\x03"
CACHE_ACCESSES = 8
SUPERSCALAR_LATENCY = 170
DATASET_BASE_SIZE = 2147483648
DATASET_EXTRA_SIZE = 33554368
DATASET_ITEM_SIZE = 64
PROGRAM_SIZE_V1 = 256
PROGRAM_SIZE_V2 = 384
PROGRAM_ITERATIONS = 2048
PROGRAM_COUNT = 8
SCRATCHPAD_L3 = 2097152
SCRATCHPAD_L2 = 262144
SCRATCHPAD_L1 = 16384
JUMP_BITS = 8
JUMP_OFFSET = 8

CACHE_LINE_COUNT = ARGON_MEMORY * 1024 // 64
DATASET_ITEM_COUNT = (DATASET_BASE_SIZE + DATASET_EXTRA_SIZE) // DATASET_ITEM_SIZE

# Opcode frequencies out of 256 (v1). The decoder maps opcode bytes to
# instructions by walking this table in order.
FREQUENCIES = [
    ("IADD_RS", 16), ("IADD_M", 7), ("ISUB_R", 16), ("ISUB_M", 7),
    ("IMUL_R", 16), ("IMUL_M", 4), ("IMULH_R", 4), ("IMULH_M", 1),
    ("ISMULH_R", 4), ("ISMULH_M", 1), ("IMUL_RCP", 8), ("INEG_R", 2),
    ("IXOR_R", 15), ("IXOR_M", 5), ("IROR_R", 8), ("IROL_R", 2),
    ("ISWAP_R", 4), ("FSWAP_R", 4), ("FADD_R", 16), ("FADD_M", 5),
    ("FSUB_R", 16), ("FSUB_M", 5), ("FSCAL_R", 6), ("FMUL_R", 32),
    ("FDIV_M", 4), ("FSQRT_R", 6), ("CBRANCH", 25), ("CFROUND", 1),
    ("ISTORE", 16), ("NOP", 0),
]
assert sum(f for _, f in FREQUENCIES) == 256

MASK64 = (1 << 64) - 1


def reciprocal(divisor):
    """randomx_reciprocal: floor(2**x / divisor) for the largest x keeping it < 2**64."""
    if divisor == 0:
        raise ZeroDivisionError("divisor must be non-zero")
    p2exp63 = 1 << 63
    q, r = divmod(p2exp63, divisor)
    shift = divisor.bit_length()
    return ((q << shift) + ((r << shift) // divisor)) & MASK64


def is_zero_or_power_of_2(x):
    return x & (x - 1) == 0


def signext32(x):
    """Sign-extend a 32-bit value to 64 bits (as an unsigned 64-bit int)."""
    x &= 0xFFFFFFFF
    return (x | 0xFFFFFFFF00000000) if x & 0x80000000 else x
