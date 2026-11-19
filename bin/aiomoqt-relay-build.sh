#!/usr/bin/env bash
# Build the aiomoqt-relay image (the fabric's peer_image) on a fabric deployment's
# Docker host, or for a VM fabric on its first relay host (then: moqbench fabric deploy).
#
#   ./bin/aiomoqt-relay-build.sh <deployment> [ref]   # ref: branch, tag or commit; default peer_pip's
#
# The image is the lab image (build/lab-node.Dockerfile, built first when the host
# lacks it) plus aiomoqt from the deployment's peer_pip (build/fabric-peer.Dockerfile).
# A git+ peer_pip is resolved to its commit and installed from that commit, so a
# branch that moved never reuses a cached layer. The image is tagged peer_image;
# image labels and .testbed/builds/<image>-<tag>.json record the pip spec, the
# resolved commit and the aiomoqt and aiopquic versions inside it.
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
DEP=${1:?usage: aiomoqt-relay-build.sh <deployment> [ref]}
REF_ARG=${2:-}
[[ -z $REF_ARG || $REF_ARG =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "bad ref" >&2; exit 2; }
read -r HOST KEY DOCKER < <(PYTHONPATH="$ROOT/lib" python3 "$ROOT/lib/fabric/buildhost.py" "$DEP")
[[ -n ${HOST:-} ]] || exit 1
DOCKER=${DOCKER/_/ }
read -r LAB PEER PIP AIOMOQT AIOPQUIC < <(PYTHONPATH="$ROOT/lib" python3 -c '
import sys, config
f = config.load_deployment(sys.argv[1]).fabric
print(f.lab_image, f.peer_image, f.peer_pip, f.aiomoqt, f.aiopquic)' "$DEP")
SSHK=(-o BatchMode=yes)
[[ $KEY == - ]] || SSHK+=(-i "$KEY" -o StrictHostKeyChecking=accept-new)

run() {
  if [[ $HOST == local ]]; then (cd ~ && bash -c "$1"); else ssh "${SSHK[@]}" "$HOST" "bash -c $(printf %q "$1")"; fi
}
push() {  # <local dir> <remote dir>
  if [[ $HOST == local ]]; then mkdir -p ~/"$2" && cp -a "$1"/. ~/"$2"/
  else tar -C "$1" -cf - . | ssh "${SSHK[@]}" "$HOST" "mkdir -p $2 && tar -C $2 -xf -"; fi
}

SPEC=$PIP SHA=
if [[ $PIP == git+* ]]; then
  URL=${PIP#git+}; URL=${URL%@*}; REF=${PIP##*@}
  [[ $PIP == *@* && $REF != "$PIP" ]] || REF=HEAD
  [[ -n $REF_ARG ]] && REF=$REF_ARG
  if [[ $REF =~ ^[0-9a-f]{40}$ ]]; then SHA=$REF
  else SHA=$(git ls-remote "$URL" "$REF" "refs/heads/$REF" "refs/tags/$REF" | awk 'NR == 1 {print $1}'); fi
  [[ ${#SHA} == 40 ]] || { echo "could not resolve $REF in $URL" >&2; exit 1; }
  SPEC="git+$URL@$SHA"
elif [[ -n $REF_ARG ]]; then
  echo "peer_pip $PIP is not a git+ spec; a ref does not apply" >&2; exit 2
fi

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
cp "$ROOT/build/lab-node.Dockerfile" "$ROOT/build/fabric-peer.Dockerfile" "$STAGE/"
S=moqbench-build/stage-aiomoqt-relay
push "$STAGE" "$S"

echo "== $HOST: $PEER from $SPEC"
run "set -euo pipefail; cd $S
  command -v docker >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -q docker.io >/dev/null
  $DOCKER image inspect $LAB >/dev/null 2>&1 || $DOCKER build -q -t $LAB --build-arg AIOMOQT=$AIOMOQT \
    --build-arg AIOPQUIC=$AIOPQUIC -f lab-node.Dockerfile . >/dev/null
  $DOCKER build -q -t $PEER --build-arg BASE=$LAB --build-arg SPEC='$SPEC' \
    --label org.moqbench.peer_pip='$PIP' --label org.moqbench.revision='$SHA' -f fabric-peer.Dockerfile . >/dev/null"
CHECK=$(run "$DOCKER run --rm $PEER sh -c 'aiomoqt-relay --help | grep -q -- --peer-port && python -m pip show aiomoqt aiopquic | grep ^Version: | cut -d\" \" -f2 | tr \"\\n\" \" \"'")
read -r VER_AIOMOQT VER_AIOPQUIC <<< "$CHECK"
[[ -n ${VER_AIOMOQT:-} ]] || { echo "$PEER: aiomoqt-relay has no --peer-port" >&2; exit 1; }
IMAGE_ID=$(run "$DOCKER image inspect $PEER --format '{{.Id}}'")
mkdir -p "$ROOT/.testbed/builds"
OUT="$ROOT/.testbed/builds/${PEER//[\/:]/-}.json"
PIP="$PIP" SPEC="$SPEC" SHA="$SHA" IMAGE_TAG="$PEER" IMAGE_ID="$IMAGE_ID" LAB="$LAB" \
VER_AIOMOQT="$VER_AIOMOQT" VER_AIOPQUIC="$VER_AIOPQUIC" python3 - "$OUT" <<'PY'
import datetime, json, os, pathlib, sys
doc = dict(peer_pip=os.environ['PIP'], spec=os.environ['SPEC'], revision=os.environ['SHA'] or None,
           image=os.environ['IMAGE_TAG'], image_id=os.environ['IMAGE_ID'], base=os.environ['LAB'],
           aiomoqt=os.environ['VER_AIOMOQT'], aiopquic=os.environ['VER_AIOPQUIC'],
           built_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
pathlib.Path(sys.argv[1]).write_text(json.dumps(doc, indent=2) + '\n')
PY
echo "Built $PEER (aiomoqt $VER_AIOMOQT, aiopquic $VER_AIOPQUIC); provenance in ${OUT#"$ROOT"/}"
