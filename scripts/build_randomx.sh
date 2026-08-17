#!/usr/bin/env bash
# Fetch and build the reference RandomX library (tevador/RandomX) as shared
# libraries under third_party/. Two tags are built:
#   v1.2.1  - the version Monero mainnet runs today
#   v2.0.1  - RandomX v2; also computes v1 hashes when RANDOMX_FLAG_V2 is unset
#
# Do NOT pass -DARCH=native on Apple Silicon: Apple clang's -march=native does
# not define __ARM_FEATURE_CRYPTO, so the library compiles with HAVE_AES=0 and
# every hardware-AES VM fails to allocate. The default ARCH uses
# -march=armv8-a+crypto, which is what we want.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TP="$ROOT/third_party"
TAGS=("${@:-v1.2.1 v2.0.1}")
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

command -v cmake >/dev/null || { echo "cmake not found (pip install --user cmake)" >&2; exit 1; }
mkdir -p "$TP"

for tag in ${TAGS[@]}; do
  src="$TP/randomx-$tag"
  build="$TP/build-$tag"
  if [ ! -d "$src" ]; then
    git clone -q --depth 1 --branch "$tag" https://github.com/tevador/RandomX "$src"
  fi
  cmake -S "$src" -B "$build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON >"$build.log" 2>&1
  cmake --build "$build" -j "$JOBS" >>"$build.log" 2>&1
  echo "built $tag -> $(ls "$build"/librandomx.* | head -1)"
done
