"""Dataset items computed from the cache (specs.md §7.2-7.3), i.e. light mode.

item() runs a Python function generated once per key: the 8 SuperscalarHash
programs unrolled into straight-line integer code (the same idea as the C
library's JIT for dataset init). reference_item() follows the spec text with
the superscalar interpreter; both must agree.
"""
import numpy as np

from rxlab.spec import params, superscalar
from rxlab.spec.blake2gen import BlakeGenerator
from rxlab.spec.params import MASK64

CACHE_WORDS = params.CACHE_LINE_COUNT * 8
MUL0 = 6364136223846793005
XORS = (9298411001130361340, 12065312585734608966, 9306329213124626780, 5281919268842080866,
        10536153434571861004, 3398623926847679864, 9549104520008361294)


def initial_registers(number):
    """Step 1 of §7.3: r0 = (itemNumber + 1) * MUL0, r1..r7 = r0 ^ constant."""
    r0 = ((number + 1) * MUL0) & MASK64
    return [r0] + [r0 ^ c for c in XORS]


def _as_words(cache):
    if isinstance(cache, (bytes, bytearray, memoryview)):
        words = np.frombuffer(cache, dtype="<u8")
    else:
        words = np.asarray(cache).reshape(-1).view(np.ndarray)
    if words.dtype.kind != "u" or words.dtype.itemsize != 8 or words.size != CACHE_WORDS:
        raise ValueError(f"expected {CACHE_WORDS} uint64 cache words, "
                         f"got {words.size} of {words.dtype}")
    return words


def _compile(programs, words):
    src = ["def item(number):", "    r0, r1, r2, r3, r4, r5, r6, r7 = initial_registers(number)",
           "    address = number"]
    for program in programs:
        src.append("    base = address % LINES * 8")
        src.append("    line = words[base:base + 8].tolist()")
        src += ["    " + s for s in superscalar.to_python(program)]
        src += [f"    r{q} ^= line[{q}]" for q in range(8)]
        src.append(f"    address = r{program.address_register}")
    src.append("    return (r0, r1, r2, r3, r4, r5, r6, r7)")
    namespace = {"M": MASK64, "S": 1 << 63, "LINES": params.CACHE_LINE_COUNT, "words": words,
                 "initial_registers": initial_registers}
    exec("\n".join(src), namespace)
    return namespace["item"]


class DatasetItemSource:
    """64-byte dataset items for `key`, each computed on demand from the cache.

    cache_words: the 256 MiB cache filled from the same `key`, as uint64 words
    (argon2d.fill_cache) or raw bytes (e.g. the C randomx_get_cache_memory).
    """

    def __init__(self, cache_words, key):
        self.words = _as_words(cache_words)
        gen = BlakeGenerator(key)
        self.programs = [superscalar.generate(gen) for _ in range(params.CACHE_ACCESSES)]
        # In the C cache order: every IMUL_RCP's reciprocal, program by program.
        self.reciprocals = [params.reciprocal(ins.imm32) for p in self.programs
                            for ins in p.instructions if ins.opcode == superscalar.IMUL_RCP]
        self._item = _compile(self.programs, self.words)

    def cache_line(self, index):
        """The 8 cache words mixed in for `index` (taken modulo the cache line count)."""
        base = index % params.CACHE_LINE_COUNT * 8
        return self.words[base:base + 8].tolist()

    def item(self, number):
        """Dataset item `number` as a tuple of 8 ints (little-endian words r0..r7)."""
        return self._item(number)

    def reference_item(self, number):
        """§7.3 step by step with the superscalar interpreter; slow, for checking item()."""
        r = initial_registers(number)
        cache_index = number
        for program in self.programs:
            line = self.cache_line(cache_index)
            superscalar.execute(program, r)
            r = [x ^ y for x, y in zip(r, line)]
            cache_index = r[program.address_register]
        return tuple(r)
