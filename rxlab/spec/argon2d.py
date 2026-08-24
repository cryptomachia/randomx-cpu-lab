"""Argon2d (RFC 9106) in numpy, and the RandomX cache it fills (specs.md §7.1).

A 1 KiB block is 128 little-endian uint64 words. The compression function G
runs a BLAKE2b round with BlaMka multiplications over the block's 8 rows and
then its 8 columns. Each half of a round is 32 independent quarter-rounds, so
here each half is one numpy vector of 32 lanes and a block costs ~170 tiny
ufunc calls. Blocks form one sequential chain (block i needs block i-1 and a
reference block chosen by i-1's first word), so nothing is batched across
blocks: per-call overhead is the whole cost of a fill.
"""
import hashlib
import os
import struct
from pathlib import Path

import numpy as np
from numpy import add, bitwise_and, bitwise_or, bitwise_xor, left_shift, multiply, right_shift

from rxlab.spec import params

BLOCK_WORDS = 128
BLOCK_BYTES = 8 * BLOCK_WORDS
SYNC_POINTS = 4
ARGON2D = 0
VERSION = 0x13
CACHE_WORDS = params.ARGON_MEMORY * BLOCK_WORDS
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"


def blake2b_long(data, outlen):
    """H' (RFC 9106 §3.3): BLAKE2b stretched to `outlen` bytes."""
    data = struct.pack("<I", outlen) + bytes(data)
    if outlen <= 64:
        return hashlib.blake2b(data, digest_size=outlen).digest()
    v = hashlib.blake2b(data).digest()
    out = [v[:32]]
    while outlen - 32 * len(out) > 64:
        v = hashlib.blake2b(v).digest()
        out.append(v[:32])
    out.append(hashlib.blake2b(v, digest_size=outlen - 32 * len(out)).digest())
    return b"".join(out)


def initial_hash(password, salt, lanes, tag_len, m_cost, t_cost, secret=b"", ad=b"",
                 version=VERSION):
    """H0 (RFC 9106 §3.2 step 1), the 64-byte digest every lane is seeded from."""
    h = hashlib.blake2b(digest_size=64)
    h.update(struct.pack("<6I", lanes, tag_len, m_cost, t_cost, version, ARGON2D))
    for field in (password, salt, secret, ad):
        field = bytes(field)
        h.update(struct.pack("<I", len(field)) + field)
    return h.digest()


def _half_round_layouts():
    """Word indices feeding (a, b, c, d) x 32 lanes in each of G's four half-rounds.

    Half-rounds run in the order: rows/columns-of-4x4, rows/diagonals,
    columns/columns-of-4x4, columns/diagonals, as in fill_block (argon2_ref.c).
    """
    q = np.arange(4)[:, None, None]  # a, b, c, d
    i = np.arange(8)[None, :, None]  # which row (or column) of the block
    k = np.arange(4)[None, None, :]  # which quarter-round within it
    layouts = []
    for on_columns in (False, True):
        for diagonal in (0, 1):
            v = 4 * q + (k + q * diagonal) % 4  # index into the 16-word BLAKE2b state
            if on_columns:
                words = 2 * i + 16 * (v // 2) + v % 2
            else:
                words = 16 * i + v
            layouts.append(words.reshape(4, 32))
    return layouts


class _Compressor:
    """G(X, Y) of RFC 9106 §3.5 with preallocated buffers, applied in place."""

    def __init__(self):
        layouts = [x.ravel() for x in _half_round_layouts()]
        inverse = [np.argsort(x) for x in layouts]
        # The state moves between half-rounds by one gather each (take with
        # mode="clip": the indices are permutations, so skip bounds checks); the
        # last one restores the natural word order.
        self.gathers = [layouts[0]] + [inverse[h][layouts[h + 1]] for h in range(3)]
        self.restore = inverse[3]
        self.bufs = (np.empty(BLOCK_WORDS, np.uint64), np.empty(BLOCK_WORDS, np.uint64))
        self.lanes = [tuple(b[32 * j:32 * (j + 1)] for j in range(4)) for b in self.bufs]
        self.r = np.empty(BLOCK_WORDS, np.uint64)
        self.keep = np.empty(BLOCK_WORDS, np.uint64)
        self.t = np.empty(32, np.uint64)
        self.u = np.empty(32, np.uint64)
        self.lo = np.full(32, 0xFFFFFFFF, np.uint64)
        self.shift = {n: np.full(32, n, np.uint64) for n in (1, 16, 24, 32, 40, 48, 63)}

    def _quarter_rounds(self, a, b, c, d):
        t, u, lo, shift = self.t, self.u, self.lo, self.shift
        for x, y, z, n in ((a, b, d, 32), (c, d, b, 24), (a, b, d, 16), (c, d, b, 63)):
            bitwise_and(x, lo, t)
            bitwise_and(y, lo, u)
            multiply(t, u, t)
            add(t, t, t)
            add(x, y, x)
            add(x, t, x)  # x = x + y + 2 * lo32(x) * lo32(y)   (BlaMka)
            bitwise_xor(z, x, z)
            right_shift(z, shift[n], t)
            left_shift(z, shift[64 - n], z)
            bitwise_or(z, t, z)  # z = rotr64(z ^ x, n)

    def __call__(self, prev, ref, out, with_xor):
        """out = G(prev, ref), or out ^= G(prev, ref) when `with_xor` (passes > 0, v1.3)."""
        r = bitwise_xor(prev, ref, self.r)
        keep = bitwise_xor(r, out, self.keep) if with_xor else r
        src = r
        for h in range(4):
            dst = self.bufs[h & 1]
            src.take(self.gathers[h], out=dst, mode="clip")
            self._quarter_rounds(*self.lanes[h & 1])
            src = dst
        src.take(self.restore, out=self.bufs[0], mode="clip")
        bitwise_xor(keep, self.bufs[0], out)


class Argon2d:
    """Argon2d memory filled block by block in the reference order (argon2_core.c)."""

    def __init__(self, password, salt, t_cost, m_cost, lanes=1, tag_len=32, secret=b"",
                 ad=b"", version=VERSION):
        self.t_cost, self.lanes, self.tag_len, self.version = t_cost, lanes, tag_len, version
        blocks = max(m_cost, 2 * SYNC_POINTS * lanes)
        self.segment_length = blocks // (lanes * SYNC_POINTS)
        self.lane_length = self.segment_length * SYNC_POINTS
        self.memory = np.zeros((lanes * self.lane_length, BLOCK_WORDS), np.uint64)
        self.h0 = initial_hash(password, salt, lanes, tag_len, m_cost, t_cost, secret, ad,
                               version)
        for lane in range(lanes):
            for i in (0, 1):
                block = blake2b_long(self.h0 + struct.pack("<II", i, lane), BLOCK_BYTES)
                self.memory[lane * self.lane_length + i] = np.frombuffer(block, "<u8")
        self._compress = _Compressor()

    def _reference(self, pass_no, slice_no, lane, index, rand):
        """Absolute index of the reference block (RFC 9106 §3.4.1.2 and §3.4.2)."""
        seg, lane_len = self.segment_length, self.lane_length
        if pass_no == 0 and slice_no == 0:
            ref_lane = lane
        else:
            ref_lane = (rand >> 32) % self.lanes
        # Blocks this one may reference: finished segments, plus the part of the
        # current segment already built when staying in the same lane.
        area = slice_no * seg if pass_no == 0 else lane_len - seg
        if ref_lane == lane:
            area += index - 1
        elif index == 0:
            area -= 1
        j1 = rand & 0xFFFFFFFF
        offset = area - 1 - ((area * ((j1 * j1) >> 32)) >> 32)
        start = 0 if pass_no == 0 or slice_no == SYNC_POINTS - 1 else (slice_no + 1) * seg
        return ref_lane * lane_len + (start + offset) % lane_len

    def fill_segment(self, pass_no, slice_no, lane, stop=None):
        """Build blocks [start, stop) of one segment; `stop` defaults to its end."""
        lane_len = self.lane_length
        memory, words = self.memory, self.memory.reshape(-1)
        compress = self._compress
        with_xor = pass_no > 0 and self.version != 0x10  # v1.0 overwrote instead
        base = lane * lane_len + slice_no * self.segment_length
        first = 2 if pass_no == 0 and slice_no == 0 else 0
        for index in range(first, self.segment_length if stop is None else stop):
            cur = base + index
            prev = cur - 1 if cur % lane_len else cur + lane_len - 1
            rand = words.item(prev * BLOCK_WORDS)
            ref = self._reference(pass_no, slice_no, lane, index, rand)
            compress(memory[prev], memory[ref], memory[cur], with_xor)

    def fill_pass(self, pass_no):
        for slice_no in range(SYNC_POINTS):
            for lane in range(self.lanes):
                self.fill_segment(pass_no, slice_no, lane)

    def fill(self):
        for pass_no in range(self.t_cost):
            self.fill_pass(pass_no)
        return self

    def tag(self):
        """H' over the XOR of every lane's last block (RFC 9106 §3.2 steps 7-8)."""
        last = np.bitwise_xor.reduce(self.memory[self.lane_length - 1::self.lane_length])
        return blake2b_long(last.astype("<u8").tobytes(), self.tag_len)


def randomx_argon2d(key):
    """The Argon2d instance behind the RandomX cache (specs.md Table 7.1.1)."""
    return Argon2d(key, params.ARGON_SALT, params.ARGON_ITERATIONS, params.ARGON_MEMORY,
                   lanes=params.ARGON_LANES, tag_len=0)


def cache_memory(key):
    """Fill the 256 MiB RandomX cache for `key`: 786,432 compressions, ~1 min of CPU."""
    return randomx_argon2d(key).fill().memory.reshape(-1)


def cache_path(key):
    return CACHE_DIR / f"cache-{hashlib.sha256(bytes(key)).hexdigest()[:16]}.npy"


def fill_cache(key):
    """cache_memory(key) as read-only uint64 words, memoised under data/cache/."""
    path = cache_path(key)
    if not path.exists():
        words = cache_memory(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".partial")
        with open(tmp, "wb") as fh:
            np.save(fh, words)
        os.replace(tmp, path)
    words = np.load(path, mmap_mode="r")
    if words.shape != (CACHE_WORDS,) or words.dtype != np.uint64:
        raise ValueError(f"{path} is not a RandomX cache; delete it")
    return words
