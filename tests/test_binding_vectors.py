"""Official RandomX test vectors (src/tests/tests.cpp, tests 1a-1e), v1 and v2."""
import pytest

from rxlab.binding import Cache, Flags, VM, recommended_flags

VECTORS = [
    # key, input, v1 hash, v2 hash
    (b"test key 000", b"This is a test",
     "639183aae1bf4c9a35884cb46b09cad9175f04efd7684e7262a0ac1c2f0b4e3f",
     "22ec6b861b3eb23686b2efbad69513c967ecfce80983df66c9c5b4fbfb4cdb6f"),
    (b"test key 000", b"Lorem ipsum dolor sit amet",
     "300a0adb47603dedb42228ccb2b211104f4da45af709cd7547cd049e9489c969",
     "9e2c772c12fd48f93c14c97fdc89d556264d9100597023f44d9163e279012ecf"),
    (b"test key 000", b"sed do eiusmod tempor incididunt ut labore et dolore magna aliqua",
     "c36d4ed4191e617309867ed66a443be4075014e2b061bcdaf9ce7b721d2b77a8",
     "4d6b063a1a603751d525f18a171336a4002f2f06df6c17e4b25fe17e17796e42"),
    (b"test key 001", b"sed do eiusmod tempor incididunt ut labore et dolore magna aliqua",
     "e9ff4503201c0c2cca26d285c93ae883f9b1d30c9eb240b820756f2d5a7905fc",
     "97024134686ce27d362ea8d86d8ef16483ac272abdabd46ef13359400777fe5e"),
    (b"test key 001", bytes.fromhex(
        "0b0b98bea7e805e0010a2126d287a2a0cc833d312cb786385a7c2f9de69d25537f584a9bc9977b"
        "00000000666fd8753bf61a8631f12984e3fd44f4014eca629276817b56f32e9b68bd82f416"),
     "c56414121acda1713c2f2a819d8ae38aed7c80c35c2a769298d34f03833cd5f1",
     "c8e92c5f7c1946fecf06bc382b92e3111da38ee3e6a5ad90704e1a9d8aaf6e76"),
]

MODES = {
    "interpreter": Flags.HARD_AES,
    "jit": Flags.HARD_AES | Flags.JIT,
}


@pytest.fixture(scope="module")
def caches():
    base = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    made = {k: Cache(k, flags=base) for k in {v[0] for v in VECTORS}}
    yield made
    for c in made.values():
        c.close()


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_official_vectors(caches, mode, version):
    flags = MODES[mode] | (recommended_flags() & Flags.SECURE)
    if version == "v2":
        flags |= Flags.V2
    for key, data, h1, h2 in VECTORS:
        with VM(flags, cache=caches[key]) as vm:
            assert vm.hash(data).hex() == (h2 if version == "v2" else h1)


def test_batch_api_matches_single(caches):
    flags = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    inputs = [b"nonce-%d" % i for i in range(16)]
    with VM(flags, cache=caches[b"test key 000"]) as vm:
        single = [vm.hash(x) for x in inputs]
        batched = list(vm.hash_many(inputs))
    assert batched == single
