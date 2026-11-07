#!/usr/bin/env bash
# Install a relay build on the selected testbed's relay host.
#
#   ./bin/relay-deploy.sh moqdev                        # local moqdev-build.sh output
#   ./bin/relay-deploy.sh moqx                          # latest moqx release
#   ./bin/relay-deploy.sh moqx v0.3.6                   # a given release
#   ./bin/relay-deploy.sh moqx moqx-linux-arm64.tar.gz  # a release or CI tarball on hand
#   ./bin/relay-deploy.sh <relay> <binary>              # any relay: one binary built elsewhere
#   ./bin/relay-deploy.sh <relay> [source]              # a relays.local.sh relay's own installer
#
# moqx comes from its GitHub releases: the generic Linux build for the relay
# host's architecture (MOQX_ASSET names another asset), which bundles its
# libraries and needs only glibc 2.34. A tarball built for another architecture
# is refused. moqx-run.sh and the perf config template come from the tarball
# when it carries them, else from the first git ref that has both: MOQX_SCRIPTS_REF,
# the tarball's VERSION, the commit VERSION ends in, the release it starts with.
# lib/run.sh launches it unchanged.
#
# A single binary lands at BIN[<relay>] under ~/Projects on the relay host
# (lib/run.sh's table, or relays.local.sh's). It must load on the host's Ubuntu
# 22.04, which is checked before it replaces anything.
#
# Every install keeps what it replaces (.prev), records what it put there (a
# .rev file that versions.txt reports), and will not replace a binary that was
# copied in by hand unless FORCE=1. A relay already running keeps the old build
# until restarted. REV=<label> names a build instead of the default label.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
SCP="scp -q -i $KEY -o StrictHostKeyChecking=accept-new"
source ./lib/run.sh
source ./lib/deploy.sh
source ./lib/moqx.sh

CFG=${1:?relay: moqdev | moqx | cf | quinn | quiche, or one relays.local.sh defines}
SOURCE=${2:-}
MOQX_REPO=${MOQX_REPO:-openmoq/moqx}
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT

# deploy_binary <path> <file>
deploy_binary() {
  local path=$1 file=$2 label rc=0
  label=${REV:-bin-$(sha256sum "$file" | cut -c1-12)}
  deploy_check "$RELAY" "$path" "$label" || rc=$?
  (( rc == 1 )) && return 0; (( rc == 0 )) || exit 1
  echo "  uploading $file"
  $SCP "$file" "$RUSER@$RELAY:$RROOT/$path.new"
  deploy_install "$RELAY" "$path" "$label"
}

# The release tree goes in whole: bin/, lib/ and the launcher swap in together,
# and the set they replace moves to moqx/.prev/. moqx/moqx is a wrapper, so the
# launch command in lib/run.sh is the same as for a source build; it clears a
# LD_LIBRARY_PATH meant for one, which would shadow the bundled libraries.
deploy_moqx() {
  local tag tarball label rc=0 f arch elf ref="" asset S=$WORK/stage
  mkdir -p "$S"
  arch=$(moqx_arch "$($SSH -n "$RUSER@$RELAY" uname -m)") || exit 1
  if [[ -f $SOURCE ]]; then
    tarball=$SOURCE
  elif [[ $SOURCE == */* ]]; then
    echo "  no moqx tarball at $SOURCE" >&2; exit 1
  else
    tag=$SOURCE asset=$(moqx_asset "$arch")
    [[ -n $tag ]] || tag=$(curl -fsSL "https://api.github.com/repos/$MOQX_REPO/releases/latest" |
      python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])' 2>/dev/null) ||
      { echo "  could not look up the latest release of $MOQX_REPO" >&2; exit 1; }
    tarball=$WORK/$asset
    echo "  moqx $tag: downloading $asset"
    curl -fsSL -o "$tarball" "https://github.com/$MOQX_REPO/releases/download/$tag/$asset" ||
      { echo "  no $asset in release $tag of $MOQX_REPO" >&2; exit 1; }
  fi
  tar xzf "$tarball" -C "$S"
  [[ -x $S/bin/moqx && -f $S/VERSION ]] || { echo "  $tarball is not a moqx release tarball" >&2; exit 1; }
  elf=$(moqx_elf_arch "$S/bin/moqx") || exit 1
  [[ $elf == "$arch" ]] || { echo "  $tarball is built for $elf; the relay host is $arch" >&2; exit 1; }
  tag=$(cat "$S/VERSION")
  if [[ -f $S/scripts/moqx-run.sh && -f $S/scripts/perf/config.bench.yaml ]]; then
    cp "$S/scripts/moqx-run.sh" "$S/scripts/perf/config.bench.yaml" "$S/"
    ref=tarball
  else
    for f in $(moqx_script_refs "$tag"); do
      if curl -fsSL -o "$S/moqx-run.sh" "https://raw.githubusercontent.com/$MOQX_REPO/$f/scripts/moqx-run.sh" &&
         curl -fsSL -o "$S/config.bench.yaml" "https://raw.githubusercontent.com/$MOQX_REPO/$f/scripts/perf/config.bench.yaml"; then
        ref=$f; break
      fi
    done
    [[ -n $ref ]] || { echo "  no moqx-run.sh and config.bench.yaml at any of: $(moqx_script_refs "$tag" | xargs)" >&2; exit 1; }
  fi
  # Only the launcher and config are installed from scripts/.
  rm -rf "$S/scripts"
  echo "  moqx scripts from $ref"
  chmod +x "$S/moqx-run.sh"
  printf '#!/bin/sh\nexec env -u LD_LIBRARY_PATH "$(dirname "$(readlink -f "$0")")/bin/moqx" "$@"\n' > "$S/moqx"
  chmod +x "$S/moqx"
  label=${REV:-$tag-$(sha256sum "$S/bin/moqx" | cut -c1-8)+scripts@$ref}
  deploy_check "$RELAY" moqx/bin/moqx "$label" || rc=$?
  (( rc == 1 )) && return 0; (( rc == 0 )) || exit 1
  echo "  moqx $label: installing"
  tar czf - -C "$S" . | $SSH "$RUSER@$RELAY" 'cat > /tmp/moqx-stage.tgz'
  $SSH "$RUSER@$RELAY" "bash -s -- $RROOT $label" <<'INSTALL'
set -euo pipefail
cd "$1/moqx"; label=$2
rm -rf .stage; mkdir .stage; tar xzf /tmp/moqx-stage.tgz -C .stage; rm -f /tmp/moqx-stage.tgz
if ! out=$(timeout 10 .stage/moqx --version 2>&1); then
  echo "  moqx $label does not run on $(hostname):" >&2; head -3 <<< "$out" >&2
  rm -rf .stage; exit 1
fi
rm -rf .prev; mkdir .prev
rmdir bin 2>/dev/null || true   # left empty by deploy_check on a first install
for f in bin lib VERSION moqx-run.sh config.bench.yaml moqx; do
  if [[ -e $f || -L $f ]]; then mv "$f" .prev/; fi
done
mv .stage/* .; rmdir .stage
printf '%s\n%s\n' "$label" "$(sha256sum bin/moqx | cut -d' ' -f1)" > bin/moqx.rev
echo "  $(hostname): moqx $label installed ($out)"
if pgrep -x moqx >/dev/null; then echo "  moqx is still running the previous build until restarted"; fi
INSTALL
}

echo "== $CFG -> relay host $RELAY (${TESTBED:-hosts from the environment})"
if [[ $CFG == moqdev ]]; then
  SOURCE=${SOURCE:-patches/bin/moqdev/moq-relay-dev}
  [[ -x $SOURCE ]] || { echo "Build first: ./bin/moqdev-build.sh (missing $SOURCE)" >&2; exit 2; }
  manifest=$(dirname "$SOURCE")/build.json
  [[ -f $manifest ]] || { echo "moqdev requires build.json beside the binary" >&2; exit 2; }
  label=$(python3 - "$SOURCE" "$manifest" <<'PYMANIFEST'
import hashlib, json, pathlib, sys
m = json.loads(pathlib.Path(sys.argv[2]).read_text())
sha = hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()
if sha != m['binary_sha256']:
    raise SystemExit('moqdev binary does not match build.json')
print('upstream-' + m['revision'][:12] + '-' + sha[:12])
PYMANIFEST
  )
  REV=${REV:-$label} deploy_binary "${BIN[moqdev]}" "$SOURCE"
  $SCP configs/moqdev-perf.toml "$RUSER@$RELAY:$RROOT/moq-dev/perf.toml.new"
  $SCP remote/moqdev-run.sh "$RUSER@$RELAY:$RROOT/moq-dev/run.sh.new"
  $SCP "$manifest" "$RUSER@$RELAY:$RROOT/moq-dev/build.json.new"
  $SSH "$RUSER@$RELAY" "set -e; cd $RROOT/moq-dev
    chmod +x run.sh.new
    for f in perf.toml run.sh build.json; do
      if [ -e \"\$f\" ]; then cp -p \"\$f\" \"\$f.prev\"; fi
      mv \"\$f.new\" \"\$f\"
    done"
  echo '  moqdev binary, performance config, launcher and build manifest installed; not started.'
elif [[ $CFG == moqx && ! ( -f $SOURCE && $SOURCE != *.tar.gz ) ]]; then
  deploy_moqx
elif [[ -f $SOURCE ]]; then
  [[ -n ${BIN[$CFG]:-} ]] || { echo "no install path for $CFG: set BIN[$CFG] in relays.local.sh" >&2; exit 2; }
  deploy_binary "${BIN[$CFG]}" "$SOURCE"
elif [[ -n ${PROC[$CFG]:-} ]] && declare -F local_deploy >/dev/null; then
  local_deploy "$CFG" "$RELAY" "$SOURCE"
else
  echo "$CFG: give a binary to install (./bin/relay-deploy.sh $CFG <binary>)" >&2; exit 2
fi
