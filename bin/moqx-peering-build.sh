#!/usr/bin/env bash
# Build the moqx relay image with moq-peering on a fabric deployment's Docker host.
#
#   ./bin/moqx-peering-build.sh <deployment> [ref]   # ref: branch, tag or commit; default peering/moq-peering
#
# Fetches openmoq/moqx at ref into ~/moqbench-build/moqx on the Docker host, applies
# every *.patch in PATCH_DIR (default patches/local/moqx-peering, ignored by git)
# with git apply, then builds upstream's builder and relay stages and adds
# libmoqx_peering.so (build/moqx-peering.Dockerfile). The image is
# $IMAGE:<sha8>, or <sha8>-p<count> with patches, and $IMAGE:latest. Image labels
# and .testbed/builds/<image>-<tag>.json record the ref, the resolved commit and
# each patch's sha256; a patched build's version string ends in +p<count>.
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
DEP=${1:?usage: moqx-peering-build.sh <deployment> [ref]}
REF=${2:-peering/moq-peering}
REPO=${MOQX_REPO_URL:-https://github.com/openmoq/moqx.git}
PATCH_DIR=${PATCH_DIR:-$ROOT/patches/local/moqx-peering}
IMAGE=${IMAGE:-moqx-peering}
WORK=moqbench-build/moqx
[[ $REF =~ ^[A-Za-z0-9._/-]+$ && $IMAGE =~ ^[a-z0-9._/-]+$ ]] || { echo "bad ref or image name" >&2; exit 2; }
HOST=$(cd "$ROOT/lib" && python3 -c 'import sys, config; print(config.load_deployment(sys.argv[1]).host)' "$DEP")

run() {
  if [[ $HOST == local ]]; then (cd ~ && bash -c "$1"); else ssh -o BatchMode=yes "$HOST" "bash -c $(printf %q "$1")"; fi
}
push() {  # <local dir> <remote dir>
  if [[ $HOST == local ]]; then mkdir -p ~/"$2" && cp -a "$1"/. ~/"$2"/
  else tar -C "$1" -cf - . | ssh -o BatchMode=yes "$HOST" "mkdir -p $2 && tar -C $2 -xf -"; fi
}

echo "== $HOST: fetching $REPO $REF"
read -r SHA DESCRIBE < <(run "set -e; mkdir -p moqbench-build
  [ -d $WORK/.git ] || git clone -q --filter=blob:none $REPO $WORK
  cd $WORK && git fetch -q origin '$REF' && git checkout -q --detach FETCH_HEAD && git reset -q --hard && git clean -qfdx
  echo \$(git rev-parse HEAD) \$(git describe --tags --always)")
[[ ${#SHA} == 40 ]] || { echo "could not resolve $REF on $HOST" >&2; exit 1; }
SHA8=${SHA:0:8}

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/patches"
PATCHES=()
if [[ -d $PATCH_DIR ]]; then
  while IFS= read -r p; do PATCHES+=("$p"); cp "$p" "$STAGE/patches/"; done < <(find "$PATCH_DIR" -maxdepth 1 -name '*.patch' | sort)
fi
TAG=$SHA8 VERSION=$DESCRIBE
if (( ${#PATCHES[@]} )); then TAG=$SHA8-p${#PATCHES[@]} VERSION=$DESCRIBE+p${#PATCHES[@]}; fi
PATCH_LABEL=$(for p in "${PATCHES[@]}"; do printf '%s:%s,' "$(basename "$p")" "$(sha256sum "$p" | cut -c1-16)"; done)
cp "$ROOT/build/moqx-peering.Dockerfile" "$STAGE/"
push "$STAGE" "moqbench-build/stage-$TAG"

echo "== $IMAGE:$TAG from $SHA ($DESCRIBE), ${#PATCHES[@]} local patch(es)"
run "set -euo pipefail; S=moqbench-build/stage-$TAG; cd $WORK
  for p in \$(ls ~/\$S/patches/*.patch 2>/dev/null | sort); do git apply --whitespace=nowarn \"\$p\"; echo \"  applied \$(basename \$p)\"; done
  export DOCKER_BUILDKIT=1
  docker build -q --target builder --build-arg MOQX_VERSION_STRING='$VERSION' -t $IMAGE:builder-$TAG -f docker/Dockerfile . >/dev/null
  docker build -q --target relay --build-arg MOQX_VERSION_STRING='$VERSION' -t $IMAGE:relay-$TAG -f docker/Dockerfile . >/dev/null
  docker build -q --build-arg BUILDER=$IMAGE:builder-$TAG --build-arg RELAY=$IMAGE:relay-$TAG \
    --label org.moqbench.repo=$REPO --label org.moqbench.ref='$REF' --label org.moqbench.revision=$SHA \
    --label org.moqbench.patches='$PATCH_LABEL' -t $IMAGE:$TAG -f ~/\$S/moqx-peering.Dockerfile ~/\$S >/dev/null
  docker tag $IMAGE:$TAG $IMAGE:latest
  git reset -q --hard"
VERSION_OUT=$(run "docker run --rm --entrypoint /usr/local/bin/moqx $IMAGE:$TAG --version 2>&1 | head -1")
IMAGE_ID=$(run "docker image inspect $IMAGE:$TAG --format '{{.Id}}'")
mkdir -p "$ROOT/.testbed/builds"
OUT="$ROOT/.testbed/builds/${IMAGE//\//_}-$TAG.json"
REPO="$REPO" REF="$REF" SHA="$SHA" DESCRIBE="$DESCRIBE" IMAGE_TAG="$IMAGE:$TAG" IMAGE_ID="$IMAGE_ID" \
VERSION_OUT="$VERSION_OUT" python3 - "$OUT" "${PATCHES[@]}" <<'PY'
import datetime, hashlib, json, os, pathlib, sys
patches = [dict(name=pathlib.Path(p).name, sha256=hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest())
           for p in sys.argv[2:]]
doc = dict(repository=os.environ['REPO'], ref=os.environ['REF'], revision=os.environ['SHA'],
           describe=os.environ['DESCRIBE'], patches=patches, image=os.environ['IMAGE_TAG'],
           image_id=os.environ['IMAGE_ID'], version=os.environ['VERSION_OUT'],
           built_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
pathlib.Path(sys.argv[1]).write_text(json.dumps(doc, indent=2) + '\n')
PY
echo "Built $IMAGE:$TAG ($VERSION_OUT); provenance in ${OUT#"$ROOT"/}"
