"""BlakeGenerator (specs.md §3.5): the PRNG that drives SuperscalarHash generation."""
import hashlib
import struct

MAX_SEED_SIZE = 60


class BlakeGenerator:
    """Hands out bytes and uint32s from a 64-byte state rehashed with Blake2b-512.

    The state starts as the seed (at most 60 bytes, zero-padded) followed by a
    little-endian 32-bit nonce, and is hashed before the first byte is used.
    A request that does not fit in the unused tail discards the tail and
    rehashes, exactly as blake2_generator.cpp does.
    """

    def __init__(self, seed, nonce=0):
        seed = bytes(seed)[:MAX_SEED_SIZE]
        self.state = seed.ljust(MAX_SEED_SIZE, b"\0") + struct.pack("<I", nonce & 0xFFFFFFFF)
        self.index = len(self.state)

    def _reserve(self, size):
        if self.index + size > len(self.state):
            self.state = hashlib.blake2b(self.state).digest()
            self.index = 0
        start = self.index
        self.index += size
        return start

    def get_byte(self):
        start = self._reserve(1)  # before reading self.state: it may rehash
        return self.state[start]

    def get_uint32(self):
        start = self._reserve(4)
        return int.from_bytes(self.state[start:start + 4], "little")
