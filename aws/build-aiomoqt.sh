#!/bin/bash
# Build aiopquic (picoquic native + extension) and aiomoqt into ~/gmarzot-bench/venv.
# Uses a uv-managed CPython so no system python3-dev / ensurepip is required.
#
# Optional: OPENSSL_ROOT_DIR=/usr/local/openssl-3.5.5 to link a current OpenSSL.
# Ubuntu 22.04 ships 3.0.2, which measures slower on this workload; QUIC
# does an AEAD seal plus header protection per packet, so libcrypto's
# per-operation cost is per-packet cost.
set -euo pipefail

BENCH="$HOME/gmarzot-bench"
VENV="$BENCH/venv"
export PATH="$HOME/.local/bin:$PATH"

# Without this every git call in an rsynced tree fails "dubious ownership" and
# BOTH setuptools_scm (-> version 0.0.0) and _build_info (-> submodule revs
# "unknown") silently degrade. Provenance that fails quietly is the worst kind.
git config --global --add safe.directory '*' 2>/dev/null || true

echo "=== 1. uv ==="
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv --version

echo "=== 2. venv (CPython 3.14) ==="
uv python install 3.14
uv venv --python 3.14 "$VENV"
"$VENV/bin/python" -V
export VIRTUAL_ENV="$VENV"          # build.sh relinks via uv, which needs this

echo "=== 3. build deps ==="
# setuptools_scm is required: with --no-build-isolation the backend cannot
# compute a version without it and falls back to 0.0.0.
uv pip install --python "$VENV/bin/python" --no-build-isolation \
  setuptools wheel cython setuptools_scm

echo "=== 4. aiopquic native (picotls + picoquic) ==="
cd "$BENCH/src/aiopquic"
export AIOPQUIC_PERF=1
# The native build dir caches the resolved OpenSSL, so --force alone keeps the
# previous one. Wipe it whenever OPENSSL_ROOT_DIR is in play.
[ -n "${OPENSSL_ROOT_DIR:-}" ] && rm -rf third_party/picoquic/build
./build.sh --native
if [ -n "${OPENSSL_ROOT_DIR:-}" ]; then
  grep -E "OpenSSL_VERSION|OpenSSL_INCLUDE_DIR" /dev/null 2>/dev/null || true
  echo "  requested OpenSSL: $OPENSSL_ROOT_DIR"
fi

echo "=== 5. aiopquic extension ==="
# setuptools' own build/ dir caches the linked .so; without removing it the
# RUNPATH for a custom OpenSSL never gets baked and the extension silently
# resolves to the system libcrypto (same SONAME, so the loader reuses it).
rm -rf build src/aiopquic/_binding/*.so
uv pip install --python "$VENV/bin/python" --no-build-isolation --no-cache .

echo "=== 6. aiomoqt ==="
cd "$BENCH/src/aiomoqt"
uv pip install --python "$VENV/bin/python" certifi sortedcontainers
uv pip install --python "$VENV/bin/python" --no-deps --no-build-isolation --no-cache .

echo "=== 7. verify ==="
"$VENV/bin/python" -m aiomoqt.versions
"$VENV/bin/python" -m aiomoqt.tools.adaptive_bench --help >/dev/null && echo "adaptive_bench OK"
echo "=== BUILD COMPLETE on $(hostname) ==="
