#!/usr/bin/env bash
# Build the LAPS relay image on a fabric deployment's Docker host, or for a VM
# fabric on its first relay host (then: moqbench fabric deploy).
#
#   ./bin/laps-build.sh <deployment> [ref]   # ref: branch, tag or commit; default main
#
# Fetches Quicr/laps at ref into ~/moqbench-build/laps on the Docker host, with its
# submodules over https (libquicr's is an ssh URL upstream), applies every *.patch
# in PATCH_DIR (default patches/local/laps, ignored by git) with git apply, then
# builds upstream's Dockerfile twice: the builder stage, which carries lapsAdmin
# for the control sidecar, as $IMAGE:builder-<tag>, and the runtime image as
# $IMAGE:<tag>. The tag is <sha8>, or <sha8>-p<count> with patches; $IMAGE:latest
# and $IMAGE:builder-latest follow them. Image labels and
# .testbed/builds/<image>-<tag>.json record the ref, the resolved commit, the
# submodule commits, each patch's sha256 and the moq-peering commit the build used.
#
# LAPS fetches moq-peering from a branch at build time, so the builder stage is
# built without the layer cache; LAPS_BUILD_CACHE=1 reuses it.
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
DEP=${1:?usage: laps-build.sh <deployment> [ref]}
REF=${2:-main}
REPO=${LAPS_REPO_URL:-https://github.com/Quicr/laps.git}
PATCH_DIR=${PATCH_DIR:-$ROOT/patches/local/laps}
IMAGE=${IMAGE:-laps}
WORK=moqbench-build/laps
[[ $REF =~ ^[A-Za-z0-9._/-]+$ && $IMAGE =~ ^[a-z0-9._/-]+$ ]] || { echo "bad ref or image name" >&2; exit 2; }
read -r HOST KEY DOCKER < <(PYTHONPATH="$ROOT/lib" python3 "$ROOT/lib/fabric/buildhost.py" "$DEP")
[[ -n ${HOST:-} ]] || exit 1
DOCKER=${DOCKER/_/ }
SSHK=(-o BatchMode=yes)
[[ $KEY == - ]] || SSHK+=(-i "$KEY" -o StrictHostKeyChecking=accept-new)

run() {
  if [[ $HOST == local ]]; then (cd ~ && bash -c "$1"); else ssh "${SSHK[@]}" "$HOST" "bash -c $(printf %q "$1")"; fi
}
push() {  # <local dir> <remote dir>
  if [[ $HOST == local ]]; then mkdir -p ~/"$2" && cp -a "$1"/. ~/"$2"/
  else tar -C "$1" -cf - . | ssh "${SSHK[@]}" "$HOST" "mkdir -p $2 && tar -C $2 -xf -"; fi
}
HTTPS='-c url.https://github.com/.insteadOf=git@github.com:'

echo "== $HOST: fetching $REPO $REF"
run "command -v docker >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -q docker.io docker-buildx git >/dev/null"
read -r SHA DESCRIBE < <(run "set -e; mkdir -p moqbench-build
  [ -d $WORK/.git ] || git clone -q $REPO $WORK
  cd $WORK && git fetch -q origin '$REF' && git checkout -q --detach FETCH_HEAD && git reset -q --hard && git clean -qfdx
  git $HTTPS submodule -q sync --recursive && git $HTTPS submodule -q update --init --recursive --force
  git submodule -q foreach --recursive 'git reset -q --hard && git clean -qfdx'
  echo \$(git rev-parse HEAD) \$(git describe --tags --always)")
[[ ${#SHA} == 40 ]] || { echo "could not resolve $REF on $HOST" >&2; exit 1; }
SHA8=${SHA:0:8}
SUBMODULES=$(run "cd $WORK && git submodule status --recursive | awk '{print \$2 \"@\" substr(\$1, 1, 12)}' | tr '\n' ' '" | sed 's/^-//; s/ -/ /g')

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/patches"
PATCHES=()
if [[ -d $PATCH_DIR ]]; then
  while IFS= read -r p; do PATCHES+=("$p"); cp "$p" "$STAGE/patches/"; done < <(find "$PATCH_DIR" -maxdepth 1 -name '*.patch' | sort)
fi
TAG=$SHA8
(( ${#PATCHES[@]} )) && TAG=$SHA8-p${#PATCHES[@]}
PATCH_LABEL=$(for p in "${PATCHES[@]}"; do printf '%s:%s,' "$(basename "$p")" "$(sha256sum "$p" | cut -c1-16)"; done)
push "$STAGE" "moqbench-build/stage-laps-$TAG"

echo "== $IMAGE:$TAG from $SHA ($DESCRIBE), ${#PATCHES[@]} local patch(es)"
NOCACHE=--no-cache
[[ ${LAPS_BUILD_CACHE:-0} == 1 ]] && NOCACHE=
LABELS="--label org.moqbench.repo=$REPO --label org.moqbench.ref='$REF' --label org.moqbench.revision=$SHA \
  --label org.moqbench.submodules='$SUBMODULES' --label org.moqbench.patches='$PATCH_LABEL'"
run "set -euo pipefail; S=moqbench-build/stage-laps-$TAG; cd $WORK
  for p in \$(ls ~/\$S/patches/*.patch 2>/dev/null | sort); do git apply --whitespace=nowarn \"\$p\"; echo \"  applied \$(basename \$p)\"; done
  export DOCKER_BUILDKIT=1
  $DOCKER build -q $NOCACHE --target builder $LABELS -t $IMAGE:builder-$TAG -f Dockerfile . >/dev/null
  $DOCKER build -q $LABELS -t $IMAGE:$TAG -f Dockerfile . >/dev/null
  $DOCKER tag $IMAGE:$TAG $IMAGE:latest
  $DOCKER tag $IMAGE:builder-$TAG $IMAGE:builder-latest
  git reset -q --hard"
IMAGE_ID=$(run "$DOCKER image inspect $IMAGE:$TAG --format '{{.Id}}'")
ADMIN=$(run "$DOCKER run --rm --entrypoint ls $IMAGE:builder-$TAG /ws/build/src/lapsAdmin 2>/dev/null" || true)
PEERING=$(run "$DOCKER run --rm -i --entrypoint sh $IMAGE:builder-$TAG" <<'SH' || true
g=$(find / -path /proc -prune -o -type d -path "*moq-peering*-src/.git" -print 2>/dev/null | head -n1)
[ -n "$g" ] || exit 0
h=$(cat "$g/HEAD")
case $h in
  ref:*) r=${h#ref: }; if [ -f "$g/$r" ]; then cat "$g/$r"; else grep " $r\$" "$g/packed-refs" | cut -d" " -f1; fi ;;
  *) echo "$h" ;;
esac
SH
)
echo "  moq-peering ${PEERING:-unknown}"
mkdir -p "$ROOT/.testbed/builds"
OUT="$ROOT/.testbed/builds/${IMAGE//\//_}-$TAG.json"
REPO="$REPO" REF="$REF" SHA="$SHA" DESCRIBE="$DESCRIBE" IMAGE_TAG="$IMAGE:$TAG" IMAGE_ID="$IMAGE_ID" \
SUBMODULES="$SUBMODULES" ADMIN="$ADMIN" PEERING="$PEERING" python3 - "$OUT" "${PATCHES[@]}" <<'PY'
import datetime, hashlib, json, os, pathlib, sys
patches = [dict(name=pathlib.Path(p).name, sha256=hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest())
           for p in sys.argv[2:]]
doc = dict(repository=os.environ['REPO'], ref=os.environ['REF'], revision=os.environ['SHA'],
           describe=os.environ['DESCRIBE'], submodules=os.environ['SUBMODULES'].split(), patches=patches,
           image=os.environ['IMAGE_TAG'], builder_image=os.environ['IMAGE_TAG'].replace(':', ':builder-', 1),
           image_id=os.environ['IMAGE_ID'], admin_bin=os.environ['ADMIN'] or None,
           moq_peering=os.environ['PEERING'] or None,
           built_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
pathlib.Path(sys.argv[1]).write_text(json.dumps(doc, indent=2) + '\n')
PY
echo "Built $IMAGE:$TAG and $IMAGE:builder-$TAG; provenance in ${OUT#"$ROOT"/}"
