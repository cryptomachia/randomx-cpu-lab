"""The pure-Python light mode (rxlab.spec.light): key -> Argon2d cache -> Dataset items -> VM.

Fast: Python Dataset items equal the C library's on sampled item numbers (the
Python side reads the C cache, so this isolates SuperscalarHash and item
assembly from Argon2d). Slow: official vectors from nothing but Python, in a
child process that is shown never to load the C library.
"""
import ctypes
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from rxlab import binding
from rxlab.spec import light, params
from rxlab.spec.dataset import DatasetItemSource
from rxlab.spec.hasher import CDatasetItemSource
from test_binding_vectors import VECTORS

ROOT = Path(__file__).resolve().parent.parent
CACHE_BYTES = params.CACHE_LINE_COUNT * 64
needs_c_library = pytest.mark.skipif(not binding.library_path().exists(),
                                     reason="RandomX library missing; run scripts/build_randomx.sh")
KEYS = list(dict.fromkeys(v[1] for v in light.VECTORS))


def c_cache_bytes(cache):
    """A copy of the 256 MiB cache the C library built (binding.Cache)."""
    try:
        return ctypes.string_at(cache.memory(), CACHE_BYTES)
    except NotImplementedError:
        pytest.skip("randomx_get_cache_memory needs a v2.x library")


def sample_items(key, count):
    """Both ends, the first items that wrap around the cache lines, and random item numbers."""
    rng = random.Random(key)
    lines, last = params.CACHE_LINE_COUNT, params.DATASET_ITEM_COUNT - 1
    return [0, 1, lines - 1, lines, lines + 1, last] + [rng.randrange(last + 1) for _ in range(count)]


def test_vector_table_matches_the_binding_vectors():
    ours = {(key, data): (h1, h2) for _, key, data, h1, h2 in light.VECTORS}
    assert all(ours[key, data] == (h1, h2) for key, data, h1, h2 in VECTORS)


def test_commitment_official_vector():
    _, _, data, _, v2_hash = light.VECTORS[0]
    assert light.commitment(data, bytes.fromhex(v2_hash)).hex() == light.COMMITMENT_1A_V2


@needs_c_library
@pytest.mark.parametrize("key", KEYS, ids=["key000", "key001", "key1f"])
def test_python_items_match_c_items(key):
    """1,006 items per key: DatasetItemSource.item() == randomx_init_dataset's item."""
    with CDatasetItemSource(key, lazy=True) as c_items:
        py_items = DatasetItemSource(c_cache_bytes(c_items.cache), key)
        for number in sample_items(key, 1000):
            assert py_items.item(number) == c_items.item(number), number


@pytest.mark.slow
@needs_c_library
@pytest.mark.parametrize("v2", [False, True], ids=["v1", "v2"])
def test_light_hasher_on_the_c_cache(v2):
    """Vector 1a through LightHasher with Python items built from the C cache."""
    _, key, data, h1, h2 = light.VECTORS[0]
    with binding.Cache(key) as cache:
        hasher = light.LightHasher(key, words=c_cache_bytes(cache))
    run = hasher.timed_hash(data, v2)
    assert run["hash"] == (h2 if v2 else h1)
    assert run["items"] == params.PROGRAM_COUNT * params.PROGRAM_ITERATIONS
    assert set(run["seconds"]) == {"scratchpad", "programs", "dataset_items", "vm", "final"}
    assert sum(run["seconds"].values()) <= run["wall_seconds"]


CHILD = r"""
import contextlib, ctypes, io, json, sys
from rxlab.spec.__main__ import main

def loaded_libraries():
    if sys.platform == "darwin":
        dyld = ctypes.CDLL(None)
        dyld._dyld_get_image_name.restype = ctypes.c_char_p
        return [dyld._dyld_get_image_name(i).decode() for i in range(dyld._dyld_image_count())]
    with open("/proc/self/maps") as fh:
        return sorted({line.split()[-1] for line in fh if "/" in line})

out = io.StringIO()
with contextlib.redirect_stdout(out):
    status = main(sys.argv[1:])
print(json.dumps({"status": status, "report": json.loads(out.getvalue()),
                  "modules": sorted(sys.modules), "libraries": loaded_libraries()}))
"""


@pytest.mark.slow
def test_vector_1a_in_pure_python(tmp_path):
    """Official vector 1a (v1, v2 and the v2 commitment) from a fresh Argon2d fill in
    Python, in a child process that never imports rxlab.binding nor loads librandomx
    (RXLAB_LIB points at a missing file, so any attempt to load it would fail)."""
    env = dict(os.environ, RXLAB_LIB=str(tmp_path / "missing-librandomx"))
    child = subprocess.run([sys.executable, "-c", CHILD, "--vectors", "1a", "--fresh-cache", "--json"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=3600)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    report = result["report"]
    (group,) = report["keys"]
    assert group["setup"]["cache"] == "argon2d"
    assert report["reproduced"] == ["1a-v1", "1a-v2"] and report["all_match"]
    assert [r["hash"] for r in group["hashes"]] == [VECTORS[0][2], VECTORS[0][3]]
    assert group["hashes"][1]["commitment"]["hash"] == light.COMMITMENT_1A_V2
    assert result["status"] == 0
    assert "rxlab.binding" not in result["modules"]
    assert result["libraries"] and not [p for p in result["libraries"] if "randomx" in p.lower()]
