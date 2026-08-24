#!/usr/bin/env bash
# Build tools/golden/cache_golden.cpp statically against the v2.0.1 RandomX
# object files and regenerate data/golden/cache_{argon2,superscalar,dataset}.json
# for the key "test key 000" (the key RandomX's own tests use) and
# cache_superscalar_keys.json (program digests for 256 more keys).
# Needs scripts/build_randomx.sh. data/golden/cache_rfc9106.json is transcribed
# from RFC 9106 and not rebuilt.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TAG=v2.0.1
SRC="$ROOT/third_party/randomx-$TAG/src"
OBJ="$ROOT/third_party/build-$TAG/CMakeFiles/randomx.dir"
[ -d "$OBJ" ] || { echo "missing $OBJ; run scripts/build_randomx.sh $TAG" >&2; exit 1; }

flags=(-O2 -std=c++11 -DNDEBUG)
case "$(uname -m)" in
  arm64|aarch64) flags+=(-march=armv8-a+crypto) ;;  # as the library; never -march=native
esac
[ "$(uname)" = Darwin ] || flags+=(-pthread)
objects=()
while IFS= read -r o; do objects+=("$o"); done < <(find "$OBJ" -name '*.o' | sort)
mkdir -p "$ROOT/build" "$ROOT/data/golden"
"${CXX:-c++}" "${flags[@]}" -I "$SRC" "$ROOT/tools/golden/cache_golden.cpp" "${objects[@]}" \
  -o "$ROOT/build/cache_golden"

for part in argon2 superscalar dataset keys; do
  name="cache_$part.json"
  [ "$part" = keys ] && name=cache_superscalar_keys.json
  "$ROOT/build/cache_golden" "$part" >"$ROOT/data/golden/$name.tmp"
  mv "$ROOT/data/golden/$name.tmp" "$ROOT/data/golden/$name"
  echo "wrote data/golden/$name"
done
