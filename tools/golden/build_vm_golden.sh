#!/usr/bin/env bash
# Build tools/golden/vm_golden.cpp against the reference RandomX library
# (v2.0.1, see scripts/build_randomx.sh) and regenerate data/golden/vm_*.json.
# The binary goes to build/golden/ (git-ignored).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TAG="${TAG:-v2.0.1}"
SRC="$ROOT/third_party/randomx-$TAG/src"
LIBDIR="$ROOT/third_party/build-$TAG"
OUT="$ROOT/build/golden"

[ -d "$SRC" ] && ls "$LIBDIR"/librandomx.* >/dev/null 2>&1 || {
  echo "RandomX $TAG sources or library missing; run scripts/build_randomx.sh" >&2
  exit 1
}

# Same target flags as the library build; never -march=native on Apple Silicon.
case "$(uname -m)" in
  arm64 | aarch64) ARCH=(-march=armv8-a+crypto) ;;
  x86_64 | amd64) ARCH=(-maes -mssse3) ;;
  *) ARCH=() ;;
esac

mkdir -p "$OUT" "$ROOT/data/golden"
c++ -std=c++14 -O2 "${ARCH[@]}" -I"$SRC" "$ROOT/tools/golden/vm_golden.cpp" \
  -L"$LIBDIR" -lrandomx -Wl,-rpath,"$LIBDIR" -o "$OUT/vm_golden"
"$OUT/vm_golden" "$ROOT/data/golden"
