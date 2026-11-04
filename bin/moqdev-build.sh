#!/usr/bin/env bash
# Build pristine upstream main for Ubuntu 22.04; never build with laptop-native ISA.
# MOQDEV_REF=<commit> repeats a build. FETCH=0 uses an already fetched revision.
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
SRC=${MOQDEV_SRC:-$ROOT/../moq-dev}
REF=${MOQDEV_REF:-origin/main}
CPU=${MOQDEV_CPU:-x86-64-v3}
JOBS=${BUILD_JOBS:-4}
[[ $CPU =~ ^[a-zA-Z0-9_-]+$ && $JOBS =~ ^[1-9][0-9]*$ ]] || exit 2
if [[ ! -d $SRC ]]; then git clone https://github.com/moq-dev/moq.git "$SRC"; fi
[[ ${FETCH:-1} == 0 ]] || git -C "$SRC" fetch origin main
REV=$(git -C "$SRC" rev-parse --verify "$REF^{commit}")
WORK=$ROOT/.testbed/moqdev-build/$REV
OUT=$ROOT/patches/bin/moqdev
mkdir -p "$WORK/source" "$WORK/target" "$OUT"
git -C "$SRC" archive "$REV" | tar -x -C "$WORK/source"
RUST=$(sed -n 's/^channel = "\([^"]*\)"/\1/p' "$WORK/source/rust-toolchain.toml")
[[ $RUST =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Expected a pinned Rust version' >&2; exit 1; }
IMAGE=moqbench-moqdev-builder:$RUST
docker build --build-arg "RUST_VERSION=$RUST" -t "$IMAGE" -f "$ROOT/build/moqdev.Dockerfile" "$ROOT/build"
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$WORK/source:/src:ro" -v "$WORK/target:/target" \
  -e CARGO_HOME=/target/cargo-home -e CARGO_TARGET_DIR=/target/cargo \
  -e "RUSTUP_TOOLCHAIN=$RUST" -e "CARGO_BUILD_JOBS=$JOBS" \
  -e "RUSTFLAGS=-C target-cpu=$CPU -C force-frame-pointers=yes" \
  -e "CFLAGS=-O3 -march=$CPU" -e "CXXFLAGS=-O3 -march=$CPU" \
  -e CARGO_PROFILE_RELEASE_DEBUG=1 -e CARGO_PROFILE_RELEASE_STRIP=none \
  "$IMAGE" cargo build --locked --release -p moq-relay --no-default-features --features quic,jemalloc \
  2>&1 | tee "$WORK/build.log"
install -m 755 "$WORK/target/cargo/release/moq-relay" "$OUT/moq-relay-dev.new"
# Run the loader/help in the deployment's glibc environment before publishing.
docker run --rm --user "$(id -u):$(id -g)" -v "$OUT:/out:ro" "$IMAGE" \
  /out/moq-relay-dev.new --help > "$WORK/help.txt"
mv "$OUT/moq-relay-dev.new" "$OUT/moq-relay-dev"
REV="$REV" CPU="$CPU" RUST="$RUST" IMAGE_ID=$(docker image inspect "$IMAGE" --format '{{.Id}}') \
python3 - "$OUT" "$WORK/source/Cargo.lock" <<'PY'
import datetime, hashlib, json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
manifest = dict(repository='https://github.com/moq-dev/moq', revision=os.environ['REV'],
    built_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    rust=os.environ['RUST'], cpu=os.environ['CPU'], builder_image=os.environ['IMAGE_ID'],
    os='Ubuntu 22.04', features=['quic', 'jemalloc'], default_features=False,
    profile='release: opt-level=3, fat LTO, codegen-units=1, debug=1, unstripped, frame pointers',
    cargo_lock_sha256=hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest(),
    binary_sha256=hashlib.sha256((out/'moq-relay-dev').read_bytes()).hexdigest())
(out/'build.json').write_text(json.dumps(manifest, indent=2)+'\n')
PY
cp "$WORK/help.txt" "$OUT/help.txt"
echo "Built $OUT/moq-relay-dev ($REV); build.json records provenance."
