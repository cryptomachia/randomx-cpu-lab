"""The whole RandomX hash (specs.md chapter 2) on top of a pluggable Dataset.

`item_source.item(i)` must return the eight 64-bit words of Dataset item i.
CDatasetItemSource serves them from the reference C library, so the Python VM
can be checked in full-memory mode independently of the Python Dataset code.
"""
import ctypes
import hashlib
import struct

from rxlab import binding

from .aes import AesGenerator1R, aes_hash_1r
from .params import PROGRAM_COUNT, SCRATCHPAD_L3
from .program import generate
from .vm import VirtualMachine

_ITEM = struct.Struct("<8Q")


def hash512(data):
    return hashlib.blake2b(data, digest_size=64).digest()


def hash256(data):
    return hashlib.blake2b(data, digest_size=32).digest()


def randomx_hash(data, item_source, v2=False, observer=None, stats=None):
    """RandomX(K, H) for the key behind `item_source` and input `data`; 32 bytes.

    observer(index, register_file, vm), if given, runs after each program;
    a Counter passed as `stats` collects the dynamic instruction mix (vm.py)."""
    gen1 = AesGenerator1R(hash512(data))
    scratchpad = bytearray(gen1.generate(SCRATCHPAD_L3))
    vm = VirtualMachine(scratchpad, item_source, v2, stats)
    seed = gen1.state
    for i in range(PROGRAM_COUNT):
        register_file = vm.execute(generate(seed, v2))
        if observer:
            observer(i, register_file, vm)
        seed = hash512(register_file)
    return hash256(register_file[:192] + aes_hash_1r(bytes(scratchpad)))


class CDatasetItemSource:
    """Dataset items read from the C library's dataset memory (one 2080 MiB buffer).

    lazy=False initialises the whole Dataset up front with `threads` threads;
    lazy=True allocates it but builds each item on request with
    randomx_init_dataset(start=i, count=1), so only touched pages get memory."""

    def __init__(self, key, threads=4, lazy=False):
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
