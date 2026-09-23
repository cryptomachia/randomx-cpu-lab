"""tools/fpcr_bench.c builds and reports every field; the experiment's constants are right."""
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))

pytestmark = pytest.mark.skipif(
    shutil.which("cc") is None or platform.machine() not in ("arm64", "aarch64", "x86_64"),
    reason="needs a C compiler on arm64 or x86-64")


def test_fpcr_bench_reports_all_fields():
    import fpcr_cost

    out = subprocess.run([str(fpcr_cost.build()), "100000", "1"], check=True,
                         capture_output=True, text=True).stdout
    res = json.loads(out)
    for key in ("base_ns_per_iter", "same_ns_per_iter", "toggle_ns_per_iter",
                "same_write_ns", "toggle_write_ns"):
        assert key in res
    assert res["base_ns_per_iter"] > 0 and res["toggle_ns_per_iter"] > 0
    assert res["iterations"] == 100000 and res["repeats"] == 1


def test_iterations_per_hash():
    import fpcr_cost

    assert fpcr_cost.ITERATIONS_PER_HASH == 8 * 2048
