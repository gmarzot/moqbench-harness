#!/bin/bash
# Rebuild aiopquic against a current OpenSSL. QUIC does an AEAD seal plus
# header protection per packet, so libcrypto's per-operation cost is per-packet
# cost -- and 3.0.x carries the provider/fetch overhead that 3.5 recovers.
# Also clears git's safe.directory refusal so _build_info gets real revisions.
set -euo pipefail

VER="${1:-3.5.5}"
PREFIX="/usr/local/openssl-${VER}"
BENCH="$HOME/gmarzot-bench"
export PATH="$HOME/.local/bin:$PATH"

git config --global --add safe.directory '*' 2>/dev/null || true

if [ ! -x "$PREFIX/bin/openssl" ]; then
  echo "=== building openssl ${VER} ==="
  cd /usr/local/src 2>/dev/null || { mkdir -p /usr/local/src && cd /usr/local/src; }
  if [ ! -f "openssl-${VER}.tar.gz" ]; then
    curl -fsSL -o "openssl-${VER}.tar.gz" \
      "https://github.com/openssl/openssl/releases/download/openssl-${VER}/openssl-${VER}.tar.gz"
  fi
  rm -rf "openssl-${VER}"
  tar xf "openssl-${VER}.tar.gz"
  cd "openssl-${VER}"
  ./Configure --prefix="$PREFIX" --openssldir="$PREFIX/ssl" \
    shared enable-ec_nistp_64_gcc_128 -O3 >/dev/null
  make -j"$(nproc)" >/dev/null 2>&1
  make install_sw >/dev/null 2>&1
fi
"$PREFIX/bin/openssl" version

echo "=== rebuilding aiopquic against ${PREFIX} ==="
export OPENSSL_ROOT_DIR="$PREFIX"
export AIOPQUIC_PERF=1
cd "$BENCH/src/aiopquic"
./build.sh --force >"$BENCH/logs/openssl-rebuild.log" 2>&1 || {
  echo "build.sh --force failed; tail:"; tail -25 "$BENCH/logs/openssl-rebuild.log"; exit 1; }
uv pip install --python "$BENCH/venv/bin/python" --no-build-isolation --reinstall . \
  >>"$BENCH/logs/openssl-rebuild.log" 2>&1

echo "=== versions after ==="
"$BENCH/venv/bin/python" -m aiomoqt.versions
