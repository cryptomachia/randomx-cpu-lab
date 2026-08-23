"""ctypes binding for the reference RandomX library (tevador/RandomX).

Every foreign call releases the GIL, so plain Python threads that each own a
VM hash in parallel. Nothing here is mining code: it computes hashes for
measurement and verification only.
"""
import ctypes
import enum
import os
import sys
import threading
from pathlib import Path

HASH_SIZE = 32
DATASET_ITEM_SIZE = 64

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TAG = "v2.0.1"


class Flags(enum.IntFlag):
    DEFAULT = 0
    LARGE_PAGES = 1
    HARD_AES = 2
    FULL_MEM = 4
    JIT = 8
    SECURE = 16
    ARGON2_SSSE3 = 32
    ARGON2_AVX2 = 64
    V2 = 128  # only understood by v2.x libraries


def library_path(tag=DEFAULT_TAG):
    """Locate librandomx for a tag; RXLAB_LIB overrides."""
    override = os.environ.get("RXLAB_LIB")
    if override:
        return Path(override)
    ext = ".dylib" if sys.platform == "darwin" else ".so"
    return ROOT / "third_party" / f"build-{tag}" / f"librandomx{ext}"


_libs = {}
_libs_lock = threading.Lock()

# randomx_calculate_hash saves and restores the floating-point environment, but
# the batch calls (first/next/last) return with the rounding mode the last
# CFROUND selected. Host code on the same thread -- Python floats included --
# would then round toward -inf, +inf or zero, so hash_many restores it.
_libc = ctypes.CDLL(None)
_FENV_BYTES = 64  # >= sizeof(fenv_t) on every supported platform


def _save_fenv():
    env = ctypes.create_string_buffer(_FENV_BYTES)
    if _libc.fegetenv(env) != 0:
        raise OSError("fegetenv failed")
    return env


def load(tag=DEFAULT_TAG):
    with _libs_lock:
        if tag in _libs:
            return _libs[tag]
        path = library_path(tag)
        if not path.exists():
            raise FileNotFoundError(f"{path} missing; run scripts/build_randomx.sh")
        lib = ctypes.CDLL(str(path))
        vp, sz, ul = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong
        sigs = {
            "randomx_get_flags": ([], ctypes.c_int),
            "randomx_alloc_cache": ([ctypes.c_int], vp),
            "randomx_init_cache": ([vp, vp, sz], None),
            "randomx_release_cache": ([vp], None),
            "randomx_alloc_dataset": ([ctypes.c_int], vp),
            "randomx_dataset_item_count": ([], ul),
            "randomx_init_dataset": ([vp, vp, ul, ul], None),
            "randomx_get_dataset_memory": ([vp], vp),
            "randomx_release_dataset": ([vp], None),
            "randomx_create_vm": ([ctypes.c_int, vp, vp], vp),
            "randomx_vm_set_cache": ([vp, vp], None),
            "randomx_vm_set_dataset": ([vp, vp], None),
            "randomx_destroy_vm": ([vp], None),
            "randomx_calculate_hash": ([vp, vp, sz, vp], None),
            "randomx_calculate_hash_first": ([vp, vp, sz], None),
            "randomx_calculate_hash_next": ([vp, vp, sz, vp], None),
            "randomx_calculate_hash_last": ([vp, vp], None),
        }
        for name, (args, res) in sigs.items():
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, res
        if hasattr(lib, "randomx_get_cache_memory"):  # v2.x only
            lib.randomx_get_cache_memory.argtypes = [vp]
            lib.randomx_get_cache_memory.restype = vp
        _libs[tag] = lib
        return lib


def recommended_flags(tag=DEFAULT_TAG):
    return Flags(load(tag).randomx_get_flags())


class Cache:
    """256 MiB Argon2d cache keyed by `key` (light mode, and dataset source)."""

    def __init__(self, key, flags=None, tag=DEFAULT_TAG):
        self.lib = load(tag)
        self.flags = recommended_flags(tag) if flags is None else Flags(flags)
        self.ptr = self.lib.randomx_alloc_cache(int(self.flags))
        if not self.ptr:
            raise MemoryError(f"randomx_alloc_cache failed for {self.flags!r}")
        key = bytes(key)
        self.lib.randomx_init_cache(self.ptr, key, len(key))
        self.key = key

    def memory(self):
        """Raw cache bytes (v2.x libraries only)."""
        if not hasattr(self.lib, "randomx_get_cache_memory"):
            raise NotImplementedError("randomx_get_cache_memory needs a v2.x library")
        return self.lib.randomx_get_cache_memory(self.ptr)

    def close(self):
        if self.ptr:
            self.lib.randomx_release_cache(self.ptr)
            self.ptr = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Dataset:
    """2080 MiB dataset expanded from a cache; `threads` split the init work."""

    def __init__(self, cache, threads=1, flags=None):
        self.lib = cache.lib
        self.flags = cache.flags if flags is None else Flags(flags)
        self.ptr = self.lib.randomx_alloc_dataset(int(self.flags))
        if not self.ptr:
            raise MemoryError("randomx_alloc_dataset failed")
        count = self.item_count = self.lib.randomx_dataset_item_count()
        threads = max(1, int(threads))
        bounds = [count * i // threads for i in range(threads + 1)]
        workers = [
            threading.Thread(
                target=self.lib.randomx_init_dataset,
                args=(self.ptr, cache.ptr, lo, hi - lo),
            )
            for lo, hi in zip(bounds, bounds[1:])
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    def close(self):
        if self.ptr:
            self.lib.randomx_release_dataset(self.ptr)
            self.ptr = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class VM:
    """One RandomX virtual machine. Not thread-safe; use one per thread."""

    def __init__(self, flags, cache=None, dataset=None):
        src = cache or dataset
        self.lib = src.lib
        self.flags = Flags(flags)
        self.ptr = self.lib.randomx_create_vm(
            int(self.flags),
            cache.ptr if cache else None,
            dataset.ptr if dataset else None,
        )
        if not self.ptr:
            raise RuntimeError(f"randomx_create_vm failed for {self.flags!r}")
        self._out = ctypes.create_string_buffer(HASH_SIZE)

    def hash(self, data):
        data = bytes(data)
        self.lib.randomx_calculate_hash(self.ptr, data, len(data), self._out)
        return self._out.raw

    def hash_many(self, inputs):
        """Pipelined hashing via the first/next/last API; yields one hash per input."""
        it = iter(inputs)
        try:
            prev = bytes(next(it))
        except StopIteration:
            return
        env = _save_fenv()
        self.lib.randomx_calculate_hash_first(self.ptr, prev, len(prev))
        for cur in it:
            cur = bytes(cur)
            self.lib.randomx_calculate_hash_next(self.ptr, cur, len(cur), self._out)
            _libc.fesetenv(env)
            yield self._out.raw
        self.lib.randomx_calculate_hash_last(self.ptr, self._out)
        _libc.fesetenv(env)
        yield self._out.raw

    def close(self):
        if self.ptr:
            self.lib.randomx_destroy_vm(self.ptr)
            self.ptr = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def light_hash(key, data, flags=None, tag=DEFAULT_TAG):
    """One-shot light-mode hash; convenient for tests, slow for anything else."""
    with Cache(key, flags=flags, tag=tag) as cache:
        with VM(cache.flags if flags is None else flags, cache=cache) as vm:
            return vm.hash(data)
