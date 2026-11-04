#!/usr/bin/env bash
# Build a relay from source on the selected testbed's relay host and install it
# where lib/run.sh launches it.
#
#   ./bin/relay-build.sh moqdev [ref]   # github.com/moq-dev/moq, default main
#   ./bin/relay-build.sh cf [ref]       # github.com/cloudflare/moq-rs, default draft-18-dev
#
# The build runs on the relay host, for its own CPU (-C target-cpu=native), with
# the toolchain the source pins in rust-toolchain.toml (else stable), the
# project's release profile, frame pointers and debug info for profiling. The
# install goes through lib/deploy.sh, keeping the build it replaces; build.json
# beside the binary records the revision, toolchain and flags. moqdev also gets
# remote/moqdev-run.sh and configs/moqdev-perf.toml.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
SCP="scp -q -i $KEY -o StrictHostKeyChecking=accept-new"
source ./lib/run.sh
source ./lib/deploy.sh

CFG=${1:?relay: moqdev | cf}
case $CFG in
  moqdev) REPO=https://github.com/moq-dev/moq.git; REF=${2:-main}
          CARGO="-p moq-relay --no-default-features --features quic,jemalloc,io-uring"; OUT=moq-relay ;;
  cf)     REPO=https://github.com/cloudflare/moq-rs.git; REF=${2:-draft-18-dev}
          CARGO="--bin moq-relay-ietf"; OUT=moq-relay-ietf ;;
  *) echo "relay-build.sh builds moqdev or cf" >&2; exit 2 ;;
esac
DEST=${BIN[$CFG]}

echo "== $CFG: building $REPO $REF on the relay host $RELAY"
rev=$($SSH "$RUSER@$RELAY" "bash -s -- '$REPO' '$REF' '$CARGO' '$OUT' '$RROOT/$DEST'" <<'BUILD'
set -euo pipefail
repo=$1 ref=$2 cargo_args=$3 out=$4 dest=$5
log=/tmp/relay-build-$(basename "$repo" .git).log
if ! command -v cmake >/dev/null || ! command -v clang >/dev/null; then
  sudo NEEDRESTART_SUSPEND=1 DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    build-essential cmake clang pkg-config libssl-dev >"$log" 2>&1
fi
[ -x ~/.cargo/bin/rustup ] || curl -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal --default-toolchain none >>"$log" 2>&1
src=~/build/$(basename "$repo" .git)
[ -d "$src/.git" ] || git clone -q "$repo" "$src"
git -C "$src" fetch -q origin "$ref"
git -C "$src" checkout -q -f --detach FETCH_HEAD
cd "$src"
tc=$(sed -n 's/^channel = "\([^"]*\)"/\1/p' rust-toolchain.toml 2>/dev/null || true); tc=${tc:-stable}
~/.cargo/bin/rustup -q toolchain install --profile minimal "$tc" >>"$log" 2>&1
flags="-C target-cpu=native -C force-frame-pointers=yes"
RUSTFLAGS=$flags CARGO_PROFILE_RELEASE_DEBUG=1 CARGO_PROFILE_RELEASE_STRIP=none \
  ~/.cargo/bin/cargo "+$tc" build -q --locked --release $cargo_args >>"$log" 2>&1 ||
  { tail -20 "$log" >&2; exit 1; }
mkdir -p "$(dirname "$dest")"
install -m 755 "target/release/$out" "$dest.new"
r=$(git rev-parse HEAD)
python3 - "$(dirname "$dest")/build.json" "$repo" "$ref" "$r" "$(~/.cargo/bin/rustc "+$tc" --version)" "$flags" "$cargo_args" \
  "$(lscpu | sed -n 's/^Model name: *//p' | head -1)" "$(sha256sum "$dest.new" | cut -d' ' -f1)" <<'PY'
import datetime, json, sys
out, repo, ref, rev, rustc, flags, cargo, cpu, sha = sys.argv[1:]
json.dump(dict(repository=repo, ref=ref, revision=rev, rustc=rustc, rustflags=flags, cargo=cargo,
               built_on=cpu, built_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               binary_sha256=sha), open(out, 'w'), indent=2)
PY
echo "$r"
BUILD
)
label="$(basename "$REPO" .git)-${rev:0:12}"
rc=0; deploy_check "$RELAY" "$DEST" "$label" || rc=$?
(( rc == 1 )) && exit 0; (( rc == 0 )) || exit 1
deploy_install "$RELAY" "$DEST" "$label"
if [[ $CFG == moqdev ]]; then
  $SCP configs/moqdev-perf.toml "$RUSER@$RELAY:$RROOT/moq-dev/perf.toml"
  $SCP remote/moqdev-run.sh "$RUSER@$RELAY:$RROOT/moq-dev/run.sh"
  $SSH "$RUSER@$RELAY" "chmod +x $RROOT/moq-dev/run.sh"
fi
echo "  $CFG $label installed; not started."
