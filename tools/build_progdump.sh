#!/usr/bin/env bash
# Build build/progdump against the RandomX v2.0.1 sources, linking the object
# files of third_party/build-v2.0.1 statically (progdump subclasses library
# internals, so it needs the exact build of those sources). v2.0.1 computes
# both v1 and v2 programs. Run scripts/build_randomx.sh first.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/third_party/randomx-v2.0.1/src"
OBJ="$ROOT/third_party/build-v2.0.1/CMakeFiles/randomx.dir/src"
CXX="${CXX:-c++}"
[ -d "$OBJ" ] || { echo "missing $OBJ; run scripts/build_randomx.sh v2.0.1" >&2; exit 1; }

case "$(uname -m)" in
  arm64|aarch64) arch=(-march=armv8-a+crypto) ;;  # never -march=native: Apple clang drops +crypto
  x86_64|amd64) arch=(-maes -mssse3) ;;
  *) arch=() ;;
esac

mkdir -p "$ROOT/build"
objs=()
while IFS= read -r o; do objs+=("$o"); done < <(find "$OBJ" -name '*.o' | sort)
"$CXX" -O2 -std=c++17 -Wall -Wno-unused-function "${arch[@]}" -isystem "$SRC" \
  "$ROOT/tools/progdump.cpp" "${objs[@]}" -pthread -o "$ROOT/build/progdump"
echo "built build/progdump"
