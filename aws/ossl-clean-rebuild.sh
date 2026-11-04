#!/bin/bash
# Clean rebuild of the native stack against a chosen OpenSSL.
# Two things the naive attempt got wrong:
#  - the picotls/picoquic build dir caches the resolved OpenSSL, so --force
#    alone keeps the system one; the dir has to go.
#  - build.sh relinks via uv, which needs VIRTUAL_ENV exported.
set -uo pipefail

PREFIX="${1:-/usr/local/openssl-3.5.5}"
BENCH="$HOME/gmarzot-bench"
SRC="$BENCH/src/aiopquic"

export PATH="$HOME/.local/bin:$PATH"
export VIRTUAL_ENV="$BENCH/venv"
export OPENSSL_ROOT_DIR="$PREFIX"
export OPENSSL_INCLUDE_DIR="$PREFIX/include"
export OPENSSL_CRYPTO_LIBRARY="$PREFIX/lib64/libcrypto.so"
export OPENSSL_SSL_LIBRARY="$PREFIX/lib64/libssl.so"
export CMAKE_PREFIX_PATH="$PREFIX"
export LD_LIBRARY_PATH="$PREFIX/lib64:${LD_LIBRARY_PATH:-}"
export AIOPQUIC_PERF=1

git config --global --add safe.directory '*' 2>/dev/null || true

echo "=== wiping cached native build ==="
rm -rf "$SRC/third_party/picoquic/build"

echo "=== native rebuild against $PREFIX ==="
cd "$SRC"
./build.sh --native >"$BENCH/logs/ossl-clean.log" 2>&1
echo "native exit=$?"
grep -E "OpenSSL_VERSION|OpenSSL_INCLUDE_DIR|OpenSSL_LIBRARIES" "$BENCH/logs/ossl-clean.log" | tail -3

echo "=== reinstall extension ==="
uv pip install --python "$BENCH/venv/bin/python" --no-build-isolation --reinstall . \
  >>"$BENCH/logs/ossl-clean.log" 2>&1
echo "install exit=$?"

echo "=== versions ==="
"$BENCH/venv/bin/python" -m aiomoqt.versions
