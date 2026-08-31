#!/usr/bin/env bash
# Build build/rxbench-<tag> against third_party/build-<tag>/librandomx for each
# tag (default: v1.2.1 v2.0.1). Run scripts/build_randomx.sh first.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAGS=("${@:-v1.2.1 v2.0.1}")
CC="${CC:-clang}"
mkdir -p "$ROOT/build"

for tag in ${TAGS[@]}; do
  lib="$ROOT/third_party/build-$tag"
  [ -d "$lib" ] || { echo "missing $lib; run scripts/build_randomx.sh $tag" >&2; exit 1; }
  v2=0
  [[ "$tag" == v1.* ]] || v2=1
  if [ "$(uname)" = Darwin ]; then
    extra=(-Wl,-rpath,@executable_path/../third_party/build-"$tag" -framework Foundation -lobjc)
  else
    extra=(-Wl,-rpath,"\$ORIGIN/../third_party/build-$tag" -pthread)
  fi
  "$CC" -O2 -std=c11 -Wall -Wextra \
    -DRXBENCH_TAG="\"$tag\"" -DRXBENCH_V2="$v2" \
    -I "$ROOT/third_party/randomx-$tag/src" \
    "$ROOT/tools/rxbench.c" -L "$lib" -lrandomx "${extra[@]}" \
    -o "$ROOT/build/rxbench-$tag"
  echo "built build/rxbench-$tag"
done
