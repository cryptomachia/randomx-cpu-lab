"""RandomX light mode in pure Python: RandomX(K, H) from the key and input alone.

The chain is argon2d (the 256 MiB cache, specs.md 7.1) -> DatasetItemSource
(each Dataset item built from the cache with SuperscalarHash when the VM asks
for it, 7.2-7.3) -> hasher.randomx_hash (the VM, chapters 2-5). The RandomX C
library is never loaded on this path; rxlab.binding is not even imported.
"""
import time

from . import argon2d
from .dataset import DatasetItemSource
from .hasher import hash256, randomx_hash

# Official hashes from src/tests/tests.cpp (v2.0.1): id, key, input, v1 hash, v2
# hash. tests.cpp publishes no v2 hash for 1f; its key is the 31 bytes that
# initCache reads from a 32-byte array.
VECTORS = [
    ("1a", b"test key 000", b"This is a test",
     "639183aae1bf4c9a35884cb46b09cad9175f04efd7684e7262a0ac1c2f0b4e3f",
     "22ec6b861b3eb23686b2efbad69513c967ecfce80983df66c9c5b4fbfb4cdb6f"),
    ("1b", b"test key 000", b"Lorem ipsum dolor sit amet",
     "300a0adb47603dedb42228ccb2b211104f4da45af709cd7547cd049e9489c969",
     "9e2c772c12fd48f93c14c97fdc89d556264d9100597023f44d9163e279012ecf"),
    ("1c", b"test key 000", b"sed do eiusmod tempor incididunt ut labore et dolore magna aliqua",
     "c36d4ed4191e617309867ed66a443be4075014e2b061bcdaf9ce7b721d2b77a8",
     "4d6b063a1a603751d525f18a171336a4002f2f06df6c17e4b25fe17e17796e42"),
    ("1d", b"test key 001", b"sed do eiusmod tempor incididunt ut labore et dolore magna aliqua",
     "e9ff4503201c0c2cca26d285c93ae883f9b1d30c9eb240b820756f2d5a7905fc",
     "97024134686ce27d362ea8d86d8ef16483ac272abdabd46ef13359400777fe5e"),
    ("1e", b"test key 001", bytes.fromhex(
        "0b0b98bea7e805e0010a2126d287a2a0cc833d312cb786385a7c2f9de69d25537f584a9bc9977b"
        "00000000666fd8753bf61a8631f12984e3fd44f4014eca629276817b56f32e9b68bd82f416"),
     "c56414121acda1713c2f2a819d8ae38aed7c80c35c2a769298d34f03833cd5f1",
     "c8e92c5f7c1946fecf06bc382b92e3111da38ee3e6a5ad90704e1a9d8aaf6e76"),
    ("1f", bytes.fromhex("7797373ea4633194640bf8d8c3b66724d6aa7bd2dc20e009df2f8f1710abe8"),
     bytes.fromhex("1010e1eaf8cf067b37b5f0ee031ab23ed1755e090a3af4415830145853e2be3e1f6821fed84dae58"
                   "d00e00da5214d6c1f2d0622e0abd51f9373d04e0b0f8e6d6514d90689721c4aac5a9bb0d"),
     "78af2a1864c42abce36d2e8983e13df99b2af0ce1362999af09fab004d4435a8", None),
]
# tests.cpp "Commitment test": the commitment of vector 1a's input and its v2 hash.
COMMITMENT_1A_V2 = "133be717399046b03ae82ce8ddd9d1ee4d3ea7fca03a50dec09b6848cbb98e18"


def commitment(data, digest):
    """randomx_calculate_commitment (v2 library API): Blake2b-256 of the input, then its hash."""
    return hash256(bytes(data) + bytes(digest))


class TimedItems:
    """An item source wrapper that counts item() calls and the wall seconds they take."""

    def __init__(self, source):
        self.source, self.calls, self.seconds = source, 0, 0.0

    def item(self, number):
        start = time.perf_counter()
        words = self.source.item(number)
        self.seconds += time.perf_counter() - start
        self.calls += 1
        return words


class LightHasher:
    """Light-mode RandomX for one key: the cache is built once, Dataset items on demand.

    The cache comes from `words` if given (uint64 words or raw bytes, e.g. the C
    library's, for tests); else fresh=True fills it in memory with Argon2d, and
    fresh=False uses the data/cache memo (argon2d.fill_cache), filling and saving
    it on first use. `setup` records where the cache came from and what it cost.
    """

    def __init__(self, key, fresh=False, words=None):
        self.key = bytes(key)
        wall, cpu = time.perf_counter(), time.process_time()
        if words is not None:
            origin = "given"
        elif fresh:
            origin, words = "argon2d", argon2d.cache_memory(self.key)
        else:
            origin = "memo" if argon2d.cache_path(self.key).exists() else "argon2d, memoised"
            words = argon2d.fill_cache(self.key)
        self.setup = {"cache": origin, "cache_seconds": time.perf_counter() - wall,
                      "cache_cpu_seconds": time.process_time() - cpu}
        wall = time.perf_counter()
        self.items = DatasetItemSource(words, self.key)
        self.setup["superscalar_seconds"] = time.perf_counter() - wall

    def hash(self, data, v2=False):
        """RandomX(key, data) as 32 bytes (v2=True: RandomX v2)."""
        return randomx_hash(data, self.items, v2)

    def timed_hash(self, data, v2=False):
        """hash() and where its time went: a dict with the hex digest, wall seconds per
        phase (randomx_hash's, with execute split into dataset_items and vm), the number
        of items built, and the hash's total wall and CPU seconds."""
        items = TimedItems(self.items)
        seconds = {}
        wall, cpu = time.perf_counter(), time.process_time()
        digest = randomx_hash(data, items, v2, timings=seconds)
        wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        seconds["dataset_items"] = items.seconds
        seconds["vm"] = seconds.pop("execute") - items.seconds
        return {"hash": digest.hex(), "seconds": seconds, "items": items.calls,
                "wall_seconds": wall, "cpu_seconds": cpu}


def light_hash(key, data, v2=False, fresh=False):
    """One-shot light-mode hash in pure Python (the first use of a key fills its cache)."""
    return LightHasher(key, fresh).hash(data, v2)
