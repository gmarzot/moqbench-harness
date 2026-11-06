#!/usr/bin/env bash
# Run a sweep plan: one relay-load.sh point per line, against RELAY (default moqx).
#
#   ./bin/sweep.sh <plan> [name]      # e.g. scenarios/sweep-g3.plan
#
# A point is "<tool> <build> <shape> <max_per_box> <seconds> <run>":
#   tool   aio (adaptive_bench) | mp (moqperf-ramp)
#   build  pgo | reg: moqx from MOQX_PGO_TARBALL / MOQX_REG_TARBALL, else the
#          release (MOQX_RELEASE, default latest), redeployed when it changes;
#          src: the build already installed (./bin/relay-build.sh), for any relay
#   shape  480p | 720p | 1080p | 2160p (scenarios/shapes.json)
# KEY=VALUE lines set the environment of the points after them; # comments.
#
# Output: results/sweep-<name>/<run>/, relay-load.sh's layout plus
# sweep-point.json. name defaults to the plan's file name without sweep- and
# .plan. A run that already passed there is skipped, so a sweep resumes where it
# stopped; a failed one runs again.
# ./bin/sweep-report.py results/sweep-<name> writes the datasets.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
PLAN=${1:?usage: sweep.sh <plan> [name]}
NAME=${2:-$(basename "$PLAN" .plan)}; NAME=${NAME#sweep-}
SWEEP=results/sweep-$NAME
mkdir -p "$SWEEP"
cp "$PLAN" "$SWEEP/plan"

# deploy <build>: install that moqx build on the relay.
deploy() {
  local src
  [[ $1 != src ]] || { echo src > "$SWEEP/.build-src"; return 0; }
  [[ ${RELAY:-moqx} == moqx ]] || { echo "build $1 is moqx's; RELAY=$RELAY takes src" >&2; return 2; }
  case $1 in
    pgo) src=${MOQX_PGO_TARBALL:-} ;;
    reg) src=${MOQX_REG_TARBALL:-} ;;
    *) echo "unknown build: $1" >&2; return 2 ;;
  esac
  [[ -n $src ]] || { src=${MOQX_RELEASE:-}; echo "  $1: no local tarball; moqx release ${src:-latest}" >&2; }
  echo "$src" > "$SWEEP/.build-$1"
  ./bin/relay-deploy.sh moqx $src </dev/null
}

failed=() built=""
while read -r line; do
  line=${line%%#*}
  [[ -n ${line// } ]] || continue
  if [[ $line =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)=([^[:space:]]*)[[:space:]]*$ ]]; then
    export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"; continue
  fi
  read -r tool build shape max dur run <<< "$line"
  [[ -n ${run:-} && $max =~ ^[0-9]+$ && $dur =~ ^[0-9]+$ ]] || { echo "bad plan line: $line" >&2; exit 2; }
  case $tool in aio) t=adaptive ;; mp) t=moqperf-ramp ;; *) echo "unknown tool: $tool" >&2; exit 2 ;; esac
  out=$SWEEP/$run
  if grep -qs '^run_exit_status=0' "$out/run.txt"; then echo "=== $run: passed already, skipped"; continue; fi
  if [[ $build != "$built" ]]; then deploy "$build" || exit 1; built=$build; fi
  relay=${RELAY:-moqx}
  boxes=${#CLIENTS[@]}; [[ ${SUB_HOSTS:-clients} != client:* ]] || boxes=1
  echo "=== $(date -u +%T) $run: $t $relay $build $shape ${max}/host ${dur}s"
  mkdir -p "$out"
  python3 - "$out/sweep-point.json" "$relay" "$tool" "$build" "$shape" "$max" "$dur" "$run" "$boxes" \
    "$(cat "$SWEEP/.build-$build")" <<'PY'
import json, os, sys
out, relay, tool, build, shape, mx, dur, run, boxes, source = sys.argv[1:]
env = {k: os.environ[k] for k in ('PUB_HOST', 'DRAFT', 'THREADS', 'LATENCY_MS', 'SHORTFALL', 'STEP_SUBS',
                                  'DELIVERY_TIMEOUT', 'MP_THREADS', 'MP_RAMP', 'MOQDEV_RUNTIME', 'SUB_HOSTS') if k in os.environ}
json.dump(dict(run=run, relay=relay, tool=tool, build=build, shape=shape, max_per_box=int(mx), duration_s=int(dur),
               load_boxes=int(boxes), moqx_source=source or 'release', env=env), open(out, 'w'), indent=1)
PY
  OUTDIR=$out SHAPE=$shape SUBS=$(( max * boxes )) ./bin/relay-load.sh "$t" "$relay" "$dur" </dev/null ||
    failed+=("$run")
done < "$PLAN"
echo "=== $(date -u +%T) sweep $NAME done; ${#failed[@]} point(s) failed${failed:+: ${failed[*]}}"
echo "datasets: ./bin/sweep-report.py $SWEEP"
(( ${#failed[@]} == 0 ))
