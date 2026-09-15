"""python3 -m rxlab.spec: RandomX light-mode hashes in pure Python, with a timing breakdown.

  python3 -m rxlab.spec --key "test key 000" --text "This is a test" [--v2]
  python3 -m rxlab.spec --key-hex 7797... --input 1010e1...
  python3 -m rxlab.spec --vectors [1a 1f ...]      official vectors (src/tests/tests.cpp)

--fresh-cache fills the 256 MiB cache with Argon2d in memory instead of using
the data/cache memo. --check (single hash) also computes the hash with the C
library, which is loaded only then. --json prints a JSON report instead of text.
"""
import argparse
import functools
import json
import sys

from . import light

_PHASES = [
    ("scratchpad", "AesGenerator1R scratchpad fill"),
    ("programs", "program generation"),
    ("dataset_items", "Dataset items (SuperscalarHash)"),
    ("vm", "VM execution"),
    ("final", "AesHash1R + Blake2b"),
]


def _parse(argv):
    p = argparse.ArgumentParser(prog="python3 -m rxlab.spec", description=__doc__.splitlines()[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    key = p.add_mutually_exclusive_group()
    key.add_argument("--key", help="key as UTF-8 text")
    key.add_argument("--key-hex", help="key as hex")
    data = p.add_mutually_exclusive_group()
    data.add_argument("--input", help="input as hex")
    data.add_argument("--text", help="input as UTF-8 text")
    p.add_argument("--v2", action="store_true", help="RandomX v2 (default: v1)")
    p.add_argument("--vectors", nargs="*", metavar="ID",
                   help="hash the official test vectors (all, or these ids) instead")
    p.add_argument("--fresh-cache", action="store_true",
                   help="fill the cache now, in memory, even if data/cache holds a memo")
    p.add_argument("--check", action="store_true", help="compare with the C library's hash")
    p.add_argument("--json", action="store_true", help="print a JSON report")
    args = p.parse_args(argv)
    if args.vectors is None:
        if (args.key is None and args.key_hex is None) or (args.input is None and args.text is None):
            p.error("give --key or --key-hex, and --input or --text (or --vectors)")
    else:
        unknown = set(args.vectors) - {v[0] for v in light.VECTORS}
        if unknown:
            p.error("unknown vector ids: %s" % " ".join(sorted(unknown)))
    return args


def _c_hash(key, data, v2):
    from rxlab import binding

    flags = binding.recommended_flags()
    with binding.Cache(key, flags=flags) as cache, \
            binding.VM(flags | (binding.Flags.V2 if v2 else 0), cache=cache) as vm:
        return vm.hash(data).hex()


def _verdict(run):
    if "match" not in run:
        return ""
    return "  ok" if run["match"] else "  MISMATCH, expected %s" % run["expected"]


def _setup_line(key, setup):
    shown = repr(key.decode()) if key.isascii() and key.decode().isprintable() else key.hex()
    return ("key %s: cache from %s in %.2f s (%.2f s CPU); SuperscalarHash programs %.2f s"
            % (shown, setup["cache"], setup["cache_seconds"], setup["cache_cpu_seconds"],
               setup["superscalar_seconds"]))


def _phase_lines(run):
    s = run["seconds"]
    lines = []
    for phase, name in _PHASES:
        extra = ""
        if phase == "dataset_items" and run["items"]:
            extra = "  (%d items, %.3f ms each)" % (run["items"], 1e3 * s[phase] / run["items"])
        lines.append("  %-34s %8.2f s%s" % (name, s[phase], extra))
    lines.append("  %-34s %8.2f s (%.2f s CPU)" % ("hash total", run["wall_seconds"],
                                                  run["cpu_seconds"]))
    return lines


def _single(args, emit):
    key = args.key.encode() if args.key is not None else bytes.fromhex(args.key_hex)
    data = args.text.encode() if args.text is not None else bytes.fromhex(args.input)
    hasher = light.LightHasher(key, fresh=args.fresh_cache)
    report = {"key": key.hex(), "input": data.hex(), "version": "v2" if args.v2 else "v1",
              "setup": hasher.setup}
    report.update(hasher.timed_hash(data, args.v2))
    if args.check:
        report["expected"] = _c_hash(key, data, args.v2)
        report["match"] = report["expected"] == report["hash"]
    emit(report["hash"] + _verdict(report))
    emit(_setup_line(key, hasher.setup))
    emit("RandomX %s, light mode, pure Python, %d-byte input:" % (report["version"], len(data)))
    for line in _phase_lines(report):
        emit(line)
    return report, report.get("match", True)


def _vectors(args, emit):
    wanted = set(args.vectors) or {v[0] for v in light.VECTORS}
    keys = []
    for v in light.VECTORS:
        if v[0] in wanted and v[1] not in keys:
            keys.append(v[1])
    groups, ok = [], True
    for key in keys:
        hasher = light.LightHasher(key, fresh=args.fresh_cache)
        emit(_setup_line(key, hasher.setup))
        runs = []
        for vid, _, data, *expected in (v for v in light.VECTORS if v[1] == key and v[0] in wanted):
            for v2, want in enumerate(expected):
                if want is None:
                    continue
                run = hasher.timed_hash(data, bool(v2))
                run.update(id=vid, version="v%d" % (v2 + 1), expected=want, match=run["hash"] == want)
                emit("%s %s %s%s" % (vid, run["version"], run["hash"], _verdict(run)))
                for line in _phase_lines(run):
                    emit(line)
                if vid == "1a" and v2:
                    got = light.commitment(data, bytes.fromhex(run["hash"])).hex()
                    run["commitment"] = {"hash": got, "expected": light.COMMITMENT_1A_V2,
                                         "match": got == light.COMMITMENT_1A_V2}
                    emit("1a v2 commitment %s%s" % (got, _verdict(run["commitment"])))
                    ok &= run["commitment"]["match"]
                ok &= run["match"]
                runs.append(run)
        groups.append({"key": key.hex(), "setup": hasher.setup, "hashes": runs})
        del hasher  # one 256 MiB cache at a time
    reproduced = ["%s-%s" % (r["id"], r["version"]) for g in groups for r in g["hashes"] if r["match"]]
    return {"keys": groups, "reproduced": reproduced, "all_match": ok}, ok


def main(argv=None):
    args = _parse(argv)
    emit = (lambda line: None) if args.json else functools.partial(print, flush=True)
    report, ok = (_single if args.vectors is None else _vectors)(args, emit)
    if args.json:
        from rxlab import sysinfo

        report["machine"] = sysinfo.describe()
        print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
