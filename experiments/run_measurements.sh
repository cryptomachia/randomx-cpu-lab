#!/usr/bin/env bash
# Measurement phase: every bench_* experiment, in order, on an idle machine.
#
#   experiments/run_measurements.sh           # full run (~70 min), writes results/bench_*.json, figures/bench_*.png
#   experiments/run_measurements.sh --smoke   # every experiment's --smoke run (outputs in data/cache/smoke/)
#
# Close other apps, plug in (the run is also valid on battery, but the power
# source is recorded), and turn Low Power Mode off. The Mac is kept awake with
# caffeinate for the duration. Order: correctness preflight, then the
# experiments whose outputs later ones read (thread scaling -> best thread
# count; tune -> sustained config), and the 20-minute sustained run last, after
# a cool-down. Env: PYTHON, PAUSE (s between experiments, default 20),
# COOLDOWN (s before the sustained run, default 180).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
PAUSE="${PAUSE:-20}"
COOLDOWN="${COOLDOWN:-180}"
SMOKE=""
if [ "${1:-}" = "--smoke" ]; then
  SMOKE="--smoke"; PAUSE=0; COOLDOWN=0
fi
EXPERIMENTS=(thread_scaling qos_placement mode_matrix v1_vs_v2 dataset_init tune)
cd "$ROOT"

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

for tag in v1.2.1 v2.0.1; do
  if [ ! -x "build/rxbench-$tag" ] || [ tools/rxbench.c -nt "build/rxbench-$tag" ]; then
    tools/build_rxbench.sh "$tag"
  fi
done

if command -v caffeinate >/dev/null; then
  caffeinate -i -w $$ &
fi

say "machine"
"$PY" -m rxlab.sysinfo
load="$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')"
say "1-minute load average: ${load:-unknown} (should be well below 1 for timing runs)"
if pmset -g 2>/dev/null | grep -Eq 'lowpowermode +1'; then
  say "WARNING: Low Power Mode is on; results will not represent the machine"
fi

say "preflight: upstream reference results (fast and light mode, v1 and v2)"
"$PY" -m rxlab.bench reference --mode light
[ -n "$SMOKE" ] || "$PY" -m rxlab.bench reference --mode fast

total=$((COOLDOWN + PAUSE * ${#EXPERIMENTS[@]}))
for e in "${EXPERIMENTS[@]}" sustained; do
  s="$("$PY" "experiments/bench_$e.py" --estimate $SMOKE)"
  printf '  %-16s ~%5d s\n' "$e" "$s"
  total=$((total + s))
done
say "estimated total runtime: ~$((total / 60)) min (estimates assume an idle machine)"

start=$SECONDS
for e in "${EXPERIMENTS[@]}"; do
  say "running bench_$e"
  "$PY" "experiments/bench_$e.py" $SMOKE
  say "bench_$e done ($(( (SECONDS - start) / 60 )) min elapsed); pausing ${PAUSE}s"
  sleep "$PAUSE"
done

say "cool-down ${COOLDOWN}s before the sustained run"
sleep "$COOLDOWN"
say "running bench_sustained"
"$PY" experiments/bench_sustained.py $SMOKE
say "all done in $(( (SECONDS - start) / 60 )) min"
