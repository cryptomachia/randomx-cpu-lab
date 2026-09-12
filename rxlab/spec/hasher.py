"""The whole RandomX hash (specs.md chapter 2) on top of a pluggable Dataset.

`item_source.item(i)` must return the eight 64-bit words of Dataset item i.
CDatasetItemSource serves them from the reference C library, so the Python VM
can be checked in full-memory mode independently of the Python Dataset code;
rxlab.spec.light serves them from the Python cache, with no C at all.
"""
import ctypes
import hashlib
import struct
import time

from .aes import AesGenerator1R, aes_hash_1r
from .params import PROGRAM_COUNT, SCRATCHPAD_L3
from .program import generate
from .vm import VirtualMachine

_ITEM = struct.Struct("<8Q")


def hash512(data):
    return hashlib.blake2b(data, digest_size=64).digest()


def hash256(data):
    return hashlib.blake2b(data, digest_size=32).digest()


class _Laps:
    """Adds the wall seconds since the previous lap to timings[phase]; no-op without a dict."""

    def __init__(self, timings):
        self.timings = timings
        self.mark = time.perf_counter()

    def __call__(self, phase):
        if self.timings is not None:
            now = time.perf_counter()
            self.timings[phase] = self.timings.get(phase, 0.0) + now - self.mark
            self.mark = now


def randomx_hash(data, item_source, v2=False, observer=None, stats=None, timings=None):
    """RandomX(K, H) for the key behind `item_source` and input `data`; 32 bytes.

    observer(index, register_file, vm), if given, runs after each program;
    a Counter passed as `stats` collects the dynamic instruction mix (vm.py);
    a dict passed as `timings` accumulates wall seconds per phase: scratchpad
    (AesGenerator1R fill), programs (generation), execute (the VM, item_source
    included), observer, and final (AesHash1R and Blake2b)."""
    lap = _Laps(timings)
    gen1 = AesGenerator1R(hash512(data))
    scratchpad = bytearray(gen1.generate(SCRATCHPAD_L3))
    lap("scratchpad")
    vm = VirtualMachine(scratchpad, item_source, v2, stats)
    seed = gen1.state
    for i in range(PROGRAM_COUNT):
        program = generate(seed, v2)
        lap("programs")
        register_file = vm.execute(program)
        lap("execute")
        if observer:
            observer(i, register_file, vm)
            lap("observer")
        seed = hash512(register_file)
    digest = hash256(register_file[:192] + aes_hash_1r(bytes(scratchpad)))
    lap("final")
    return digest


class CDatasetItemSource:
    """Dataset items read from the C library's dataset memory (one 2080 MiB buffer).

    lazy=False initialises the whole Dataset up front with `threads` threads;
    lazy=True allocates it but builds each item on request with
    randomx_init_dataset(start=i, count=1), so only touched pages get memory."""

    def __init__(self, key, threads=4, lazy=False):
        # Imported here, not at the top: the pure-Python path (light.py) must
        # never import the C wrapper.
        from rxlab import binding

        self.cache = binding.Cache(key)
        self.lib = self.cache.lib
        self.lazy = lazy
        if lazy:
            self.dataset = None
            self.ptr = self.lib.randomx_alloc_dataset(int(self.cache.flags))
            if not self.ptr:
                raise MemoryError("randomx_alloc_dataset failed")
        else:
            self.dataset = binding.Dataset(self.cache, threads=threads)
            self.ptr = self.dataset.ptr
            self.cache.close()
        size = self.lib.randomx_dataset_item_count() * binding.DATASET_ITEM_SIZE
        memory = self.lib.randomx_get_dataset_memory(self.ptr)
        self.memory = (ctypes.c_char * size).from_address(memory)

    def item(self, i):
        if self.lazy:
            self.lib.randomx_init_dataset(self.ptr, self.cache.ptr, i, 1)
        return _ITEM.unpack_from(self.memory, 64 * i)

    def close(self):
        self.memory = None
        if self.dataset:
            self.dataset.close()
        elif self.ptr:
            self.lib.randomx_release_dataset(self.ptr)
        self.ptr = None
        self.cache.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
