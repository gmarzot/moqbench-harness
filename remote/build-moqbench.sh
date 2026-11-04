#!/usr/bin/env bash
# Build Quicr moq-bench (moqbench_pub / moqbench_sub / moqbench_meeting, on
# libquicr) into ~/Projects/moq-bench. Runs on the host; testbed-provision.sh feeds it:
#
#   ssh host 'bash -s' -- <moq-bench rev> <libquicr rev, or - for as pinned> < remote/build-moqbench.sh
#
# The libquicr override exists because the draft matters and moq-bench does not
# pin it consistently: revisions up to 2026-06 float on libquicr#main, which has
# since moved to draft-18, so building one of them today silently produces a
# draft-18 client, which a draft-16 relay will refuse.
set -euo pipefail
REV=$1 QUICR=${2:--}
DEST=$HOME/Projects/moq-bench
SRC=$HOME/src/moq-bench
STAMP="moq-bench=$REV libquicr=$QUICR"

if [[ -x $DEST/bin/moqbench_sub && $(cat "$DEST/REVISION" 2>/dev/null) == "$STAMP" ]]; then
  echo "  $(hostname): $STAMP already built"; exit 0
fi

# C++20 needs GCC 12; 22.04 defaults to 11 but carries 12 alongside.
sudo cloud-init status --wait >/dev/null 2>&1 || true
# python3-venv: libquicr's CMake builds its draft parser in a virtualenv.
sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -q \
  g++-12 cmake git libssl-dev pkg-config python3-venv >/dev/null

[[ -d $SRC/.git ]] || git clone -q https://github.com/Quicr/moq-bench.git "$SRC"
git -C "$SRC" fetch -q origin
git -C "$SRC" checkout -q -f "$REV"
[[ $QUICR != - ]] && sed -i -E "s|gh:Quicr/libquicr#[^\")]*|gh:Quicr/libquicr#$QUICR|" "$SRC/CMakeLists.txt"
grep -o 'gh:Quicr/libquicr#[^")]*' "$SRC/CMakeLists.txt" | sed 's/^/  using /'

rm -rf "$SRC/build"
CC=gcc-12 CXX=g++-12 cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release >/tmp/moqbench-cmake.log 2>&1 \
  || { tail -20 /tmp/moqbench-cmake.log; exit 1; }
cmake --build "$SRC/build" -j "$(nproc)" >/tmp/moqbench-build.log 2>&1 \
  || { tail -30 /tmp/moqbench-build.log; exit 1; }

mkdir -p "$DEST/bin"
install -m 0755 "$SRC"/build/moqbench_{pub,sub,meeting} "$DEST/bin/"
cp -r "$SRC/examples" "$SRC/scripts" "$DEST/"
echo "$STAMP" > "$DEST/REVISION"
echo "  $(hostname): built $STAMP"
