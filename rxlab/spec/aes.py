"""AES rounds and the RandomX AES generators and hash (specs.md chapter 3).

Built from first principles: the S-box comes from the GF(2^8) inverse plus the
FIPS-197 affine map, and each round uses the four classic 32-bit T-tables. A
16-byte AES state is handled as four little-endian 32-bit words, word j holding
column j (its low byte is row 0), which is the byte order x86 AESENC/AESDEC
and ARM AESE/AESMC use for a 128-bit register.
"""
import struct

_WORDS16 = struct.Struct("<16I")
_WORDS4 = struct.Struct("<4I")


def _xtime(b):
    b <<= 1
    return b ^ 0x11B if b & 0x100 else b


def _gmul(a, b):
    """Multiply in GF(2^8) modulo x^8 + x^4 + x^3 + x + 1."""
    p = 0
    while b:
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _rotl8(x, n):
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _build_sbox():
    inverse = [0] * 256
    for a in range(1, 256):
        inverse[a] = next(b for b in range(1, 256) if _gmul(a, b) == 1)
    sbox = []
    for b in inverse:
        sbox.append(b ^ _rotl8(b, 1) ^ _rotl8(b, 2) ^ _rotl8(b, 3) ^ _rotl8(b, 4) ^ 0x63)
    inv = [0] * 256
    for i, s in enumerate(sbox):
        inv[s] = i
    return tuple(sbox), tuple(inv)


SBOX, INV_SBOX = _build_sbox()


def _column(b0, b1, b2, b3):
    return b0 | b1 << 8 | b2 << 16 | b3 << 24


def _rotations(t0):
    rot = [t0]
    for _ in range(3):
        rot.append(tuple(((w << 8) | (w >> 24)) & 0xFFFFFFFF for w in rot[-1]))
    return rot


# TE0[x] is the MixColumns image of the column (S[x], 0, 0, 0); TE1..TE3 are the
# same column rotated down by one to three rows. TD* do the same for the inverse.
TE0, TE1, TE2, TE3 = _rotations(tuple(
    _column(_gmul(s, 2), s, s, _gmul(s, 3)) for s in SBOX))
TD0, TD1, TD2, TD3 = _rotations(tuple(
    _column(_gmul(s, 14), _gmul(s, 9), _gmul(s, 13), _gmul(s, 11)) for s in INV_SBOX))


def enc_round(s0, s1, s2, s3, k0, k1, k2, k3):
    """One AES encryption round: ShiftRows, SubBytes, MixColumns, XOR key."""
    return (
        TE0[s0 & 255] ^ TE1[s1 >> 8 & 255] ^ TE2[s2 >> 16 & 255] ^ TE3[s3 >> 24] ^ k0,
        TE0[s1 & 255] ^ TE1[s2 >> 8 & 255] ^ TE2[s3 >> 16 & 255] ^ TE3[s0 >> 24] ^ k1,
        TE0[s2 & 255] ^ TE1[s3 >> 8 & 255] ^ TE2[s0 >> 16 & 255] ^ TE3[s1 >> 24] ^ k2,
        TE0[s3 & 255] ^ TE1[s0 >> 8 & 255] ^ TE2[s1 >> 16 & 255] ^ TE3[s2 >> 24] ^ k3,
    )


def dec_round(s0, s1, s2, s3, k0, k1, k2, k3):
    """One AES decryption round: InvShiftRows, InvSubBytes, InvMixColumns, XOR key."""
    return (
        TD0[s0 & 255] ^ TD1[s3 >> 8 & 255] ^ TD2[s2 >> 16 & 255] ^ TD3[s1 >> 24] ^ k0,
        TD0[s1 & 255] ^ TD1[s0 >> 8 & 255] ^ TD2[s3 >> 16 & 255] ^ TD3[s2 >> 24] ^ k1,
        TD0[s2 & 255] ^ TD1[s1 >> 8 & 255] ^ TD2[s0 >> 16 & 255] ^ TD3[s3 >> 24] ^ k2,
        TD0[s3 & 255] ^ TD1[s2 >> 8 & 255] ^ TD2[s1 >> 16 & 255] ^ TD3[s0 >> 24] ^ k3,
    )


def encrypt_block(block, key):
    """AES encryption round on 16-byte strings (AESENC semantics)."""
    return _WORDS4.pack(*enc_round(*_WORDS4.unpack(block), *_WORDS4.unpack(key)))


def decrypt_block(block, key):
    """AES decryption round on 16-byte strings (AESDEC semantics)."""
    return _WORDS4.pack(*dec_round(*_WORDS4.unpack(block), *_WORDS4.unpack(key)))


def _keys(hexes):
    return [_WORDS4.unpack(bytes.fromhex(h)) for h in hexes]


# specs.md 3.2: key0..key3 = Hash512("RandomX AesGenerator1R keys")
GEN1R_KEYS = _keys([
    "53a5ac6d096671622b55b5db1749f4b4",
    "07af7c6d0d716a8478d325174edca10d",
    "f162123fc67e949f4f79c0f445e3203e",
    "3581ef6a7c31bab1884c311654911649",
])
# specs.md 3.3: key0..key7 = Hash512("RandomX AesGenerator4R keys 0-3") || (... "4-7")
GEN4R_KEYS = _keys([
    "ddaa2164db3d83d12b6d542f3fd2e599",
    "50340eb2553f91b6539df706e5cddfa5",
    "04d93e5caf7b5e519f67a40abf021c17",
    "63376285085d8fe7853767cd91d2ded8",
    "736f82b5a6a7d6e36d8b513db4ff9e22",
    "f36b56c7d9b3109c4e4d02e9d2b772b2",
    "e7c973f28ba365f70a66a92ba7ef3bf6",
    "09d67c7ade395891fdd1060c2d76b0c0",
])
# specs.md 3.4: initial state = Hash512("RandomX AesHash1R state"),
# extra keys = Hash256("RandomX AesHash1R xkeys")
HASH1R_STATE = _keys([
    "0d2cb592de56a89f47db82ccad3a98d7",
    "6e998d3398b7c7155a129ef55780e7ac",
    "1700776ad0c762ae6b507950e47ca0e8",
    "0c240a638d82ad070500a1794849997e",
])
HASH1R_XKEYS = _keys([
    "8983faf69f94248bbf56dc9001028906",
    "d163b2613ce0f451c64310ee9bf918ed",
])


class AesGenerator1R:
    """specs.md 3.2: one AES round per 16 output bytes, in four columns."""

    def __init__(self, seed):
        if len(seed) != 64:
            raise ValueError("AesGenerator1R needs a 64-byte seed")
        self.words = list(_WORDS16.unpack(seed))

    @property
    def state(self):
        return _WORDS16.pack(*self.words)

    def generate(self, size):
        """Return `size` bytes (a multiple of 64); the state advances in place."""
        if size % 64:
            raise ValueError("output size must be a multiple of 64")
        (k00, k01, k02, k03), (k10, k11, k12, k13), (k20, k21, k22, k23), \
            (k30, k31, k32, k33) = GEN1R_KEYS
        w = self.words
        c0, c1, c2, c3 = tuple(w[0:4]), tuple(w[4:8]), tuple(w[8:12]), tuple(w[12:16])
        out = []
        for _ in range(size // 64):
            c0 = dec_round(*c0, k00, k01, k02, k03)
            c1 = enc_round(*c1, k10, k11, k12, k13)
            c2 = dec_round(*c2, k20, k21, k22, k23)
            c3 = enc_round(*c3, k30, k31, k32, k33)
            out += c0
            out += c1
            out += c2
            out += c3
        self.words = list(c0 + c1 + c2 + c3)
        return struct.pack("<%dI" % len(out), *out)


class AesGenerator4R:
    """specs.md 3.3: four AES rounds per 16 output bytes; columns 0-1 use keys
    0-3, columns 2-3 use keys 4-7."""

    def __init__(self, seed):
        if len(seed) != 64:
            raise ValueError("AesGenerator4R needs a 64-byte seed")
        self.words = list(_WORDS16.unpack(seed))

    @property
    def state(self):
        return _WORDS16.pack(*self.words)

    def generate(self, size):
        """Return `size` bytes (a multiple of 64); the state advances in place."""
        if size % 64:
            raise ValueError("output size must be a multiple of 64")
        lo, hi = GEN4R_KEYS[:4], GEN4R_KEYS[4:]
        w = self.words
        c0, c1, c2, c3 = tuple(w[0:4]), tuple(w[4:8]), tuple(w[8:12]), tuple(w[12:16])
        out = []
        for _ in range(size // 64):
            for ka, kb in zip(lo, hi):
                c0 = dec_round(*c0, *ka)
                c1 = enc_round(*c1, *ka)
                c2 = dec_round(*c2, *kb)
                c3 = enc_round(*c3, *kb)
            out += c0
            out += c1
            out += c2
            out += c3
        self.words = list(c0 + c1 + c2 + c3)
        return struct.pack("<%dI" % len(out), *out)


def aes_hash_1r(data):
    """specs.md 3.4: 64-byte fingerprint of `data` (length a multiple of 64)."""
    if len(data) % 64:
        raise ValueError("input size must be a multiple of 64")
    c0, c1, c2, c3 = HASH1R_STATE
    words = struct.unpack("<%dI" % (len(data) // 4), data)
    for i in range(0, len(words), 16):
        c0 = enc_round(*c0, *words[i:i + 4])
        c1 = dec_round(*c1, *words[i + 4:i + 8])
        c2 = enc_round(*c2, *words[i + 8:i + 12])
        c3 = dec_round(*c3, *words[i + 12:i + 16])
    for xk in HASH1R_XKEYS:
        c0 = enc_round(*c0, *xk)
        c1 = dec_round(*c1, *xk)
        c2 = enc_round(*c2, *xk)
        c3 = dec_round(*c3, *xk)
    return _WORDS16.pack(*c0, *c1, *c2, *c3)
