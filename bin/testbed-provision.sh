#!/usr/bin/env bash
# Provision a testbed after `./bin/testbed.sh <deployment> up`. Idempotent — safe to re-run,
# and the thing to run again after resizing to a bigger shape.
#
#   ./bin/testbed-provision.sh                 # hosts of the selected testbed
#   ./bin/testbed-provision.sh 203.0.113.10 198.51.100.10 # explicit relay, client public IPs
#   TOOLS="aiomoqt" ./bin/testbed-provision.sh # skip the moq-bench build
#   RELAYS="moqx myrelay" ./bin/testbed-provision.sh  # relays to deploy (default moqx)
#   RELAYS=none ./bin/testbed-provision.sh     # load generators only
#
# What lands where:
#   relay    the RELAYS, through ./bin/relay-deploy.sh (moqx from its GitHub
#            release), + every publisher
#   clients  aiomoqt, moq-bench
#   probes   the same load tools as the clients, for off-box publish and probing
#
# TOOLS picks the optional load generators (none for neither).
#   aiomoqt   aiomoqt from PyPI ($AIOMOQT_VERSION): adaptive_bench, load_sim, ...
#             or a local build: AIOMOQT_WHEEL=path/to/aiomoqt-*.whl
#             or git refs built on each host: AIOMOQT_SRC=<ref> [AIOPQUIC_SRC=<ref>]
#             (AIOMOQT_SRC=1 takes the dev heads origin/gmarzot-0.15.0 and -0.8.0)
#   moqbench  Quicr moq-bench on libquicr, built for MOQBENCH_DRAFT (16 or 18)
#
# SRC=user@host[:dir] also pulls a whole prebuilt tree (dir, default Projects
# under that login) from a host that has one: relays built from source plus moxygen's moqperf tools, which have no
# public release (moqperf runs and relay-smoke.sh need them). It authorizes the
# relay VM's key on that host, so it only happens when SRC is set.
#
# Artifacts are NOT baked into the image: the relay binaries change often
# (they carry local patches), so they are pushed from a source of truth and
# re-pushed on demand rather than frozen into cloud-init.
#
# Without SRC, moxygen's moqtest_server and moqperf_test_client come from its
# GitHub release (MOXYGEN_RELEASE, default v0.3.7) for each host's architecture,
# with libgflags2.2 and libjemalloc2; moqperf-ramp runs need them.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
# Explicit hosts as arguments, relay first; otherwise the selected testbed.
if [[ $# -ge 2 ]]; then export RELAY_HOST=$1 CLIENT_HOSTS="${*:2}"; fi
source ./lib/hosts.sh
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

TOOLS=" ${TOOLS:-aiomoqt moqbench} "
RELAYS=${RELAYS:-moqx}
if [[ $RELAYS == 0 ]]; then RELAYS=none; fi
has() { [[ $TOOLS == *" $1 "* ]]; }
LOADHOSTS=("${CLIENTS[@]}" ${PROBES[@]+"${PROBES[@]}"})
# Clients pull the tooling from the relay. Same-region that is the private
# address; cross-region there is no shared private network, so RELAY_PRIV is
# overridable and set to the relay's public address instead.
RELAY_PRIV=${RELAY_PRIV:-$RPRIV}
# A host to pull a prebuilt tree from (above); none by default. Datacenter to
# datacenter beats uploading ~1 GB over a home link (11 s vs many minutes).
SRC=${SRC:-}

# aiomoqt 0.11+ carries load_sim; adaptive_bench's --role split needs 0.12+.
# aiopquic ships manylinux_2_34 wheels, which 22.04's glibc 2.35 satisfies, so
# the default install needs no compilation. A native installer can override it.
AIOMOQT_VERSION=${AIOMOQT_VERSION:-0.13.3}
# A wheel built from a branch not on PyPI yet (uv build --wheel) installs in
# its place; the version in its file name is the one checked.
AIOMOQT_WHEEL=${AIOMOQT_WHEEL:-}
# Optional host-native installer: receives the staged aiomoqt wheel as $1.
AIOMOQT_INSTALL_SCRIPT=${AIOMOQT_INSTALL_SCRIPT:-}
if [[ -n $AIOMOQT_INSTALL_SCRIPT ]]; then
  [[ -f $AIOMOQT_INSTALL_SCRIPT && -n $AIOMOQT_WHEEL ]] || {
    echo "AIOMOQT_INSTALL_SCRIPT requires an existing script and AIOMOQT_WHEEL" >&2; exit 2;
  }
fi
AIOMOQT_SPEC="aiomoqt==$AIOMOQT_VERSION"
# Source builds replace the pin: aiopquic's build.sh, then aiomoqt as a regular
# (not editable) package in the same venv, so an aiomoqt/ directory where the
# tools run cannot shadow it.
AIOMOQT_SRC=${AIOMOQT_SRC:-}
[[ $AIOMOQT_SRC == 1 ]] && AIOMOQT_SRC=origin/gmarzot-0.15.0
AIOPQUIC_SRC=${AIOPQUIC_SRC:-origin/gmarzot-0.8.0}
if [[ -n $AIOMOQT_SRC && -n $AIOMOQT_WHEEL ]]; then
  echo "AIOMOQT_SRC and AIOMOQT_WHEEL are exclusive" >&2; exit 2
fi
MOXYGEN_REPO=${MOXYGEN_REPO:-openmoq/moxygen}
MOXYGEN_RELEASE=${MOXYGEN_RELEASE:-v0.3.7}
if [[ -n $AIOMOQT_WHEEL ]]; then
  [[ -f $AIOMOQT_WHEEL ]] || { echo "AIOMOQT_WHEEL: no file $AIOMOQT_WHEEL" >&2; exit 2; }
  AIOMOQT_VERSION=$(basename "$AIOMOQT_WHEEL" | cut -d- -f2)
  AIOMOQT_SPEC=/tmp/$(basename "$AIOMOQT_WHEEL")
fi
# moq-bench for the MoQT draft the relays speak (16 here). 16 is the last
# moq-bench before its draft-18 move (2026-03-18), with libquicr pinned to the
# draft-16 tag, since that revision floats on libquicr#main. 18 is upstream
# HEAD as of 2026-10-20, whose own pin is already draft-18.
MOQBENCH_DRAFT=${MOQBENCH_DRAFT:-16}
case $MOQBENCH_DRAFT in
  16) MOQBENCH_REV=${MOQBENCH_REV:-a3b3c82b26d8b3f2da35cc5bb01bc7871695dc77}
      LIBQUICR_REV=${LIBQUICR_REV:-9fa8254f9b39f2f50fb369c914c79e6f3f07fdf6} ;;  # tag draft-16
  18) MOQBENCH_REV=${MOQBENCH_REV:-4a0587756854270f6b9db5aebf7ce4c0baa39561}
      LIBQUICR_REV=${LIBQUICR_REV:--} ;;
  *)  echo "MOQBENCH_DRAFT must be 16 or 18" >&2; exit 2 ;;
esac

echo "relay=$RELAY (priv ${RELAY_PRIV:-?})  clients=${CLIENTS[*]}  probes=${PROBES[*]:-none}"
echo "relays: $RELAYS  tools:${TOOLS} prebuilt tree: ${SRC:-none}"

# 0. The base packages cloud-init installs at first boot (iperf3, perf,
#    numactl, node_exporter). A first boot that loses the dpkg lock race to
#    the image's own apt run skips them silently, which surfaces much later as
#    "iperf3 not installed"; install them wherever they are missing.
for H in "$RELAY" "${LOADHOSTS[@]}"; do
  $SSH ubuntu@"$H" 'command -v iperf3 >/dev/null && exit 0
    sudo cloud-init status --wait >/dev/null 2>&1
    export DEBIAN_FRONTEND=noninteractive
    sudo -E apt-get -o DPkg::Lock::Timeout=300 update -q >/dev/null &&
    sudo -E apt-get -o DPkg::Lock::Timeout=300 install -y -q iperf3 numactl \
      linux-tools-common linux-tools-generic prometheus-node-exporter >/dev/null &&
    echo "  $(hostname): installed the base packages first boot missed" ||
    echo "  $(hostname): FAILED to install base packages" >&2' &
done; wait

# 1. aiomoqt everywhere (host calibration + independent load path).
#    Installed as the login user so uv's Python is readable; installing as
#    root leaves the venv unusable by anyone else. Reinstalled when the pinned
#    version changes, not just when the venv is missing.
aiomoqt_jobs=()
has aiomoqt && for H in "$RELAY" "${LOADHOSTS[@]}"; do
  { [[ -z $AIOMOQT_WHEEL ]] || scp -q -i "$KEY" "$AIOMOQT_WHEEL" ubuntu@"$H":"$AIOMOQT_SPEC"
  if [[ -n $AIOMOQT_INSTALL_SCRIPT ]]; then
    scp -q -i "$KEY" "$AIOMOQT_INSTALL_SCRIPT" ubuntu@"$H":/tmp/testbed-aiomoqt-install.sh
    printf -v native_cmd 'bash /tmp/testbed-aiomoqt-install.sh %q' "$AIOMOQT_SPEC"
    $SSH ubuntu@"$H" "$native_cmd" || exit 1
  fi
  $SSH ubuntu@"$H" "WANT=$AIOMOQT_VERSION SPEC=$AIOMOQT_SPEC PQ_REF=$AIOPQUIC_SRC MOQ_REF=$AIOMOQT_SRC"'; set -e
    export PATH=$HOME/.local/bin:$PATH
    command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh >/dev/null 2>&1
    export PATH=$HOME/.local/bin:$PATH
    mkdir -p ~/Projects/bench
    PY=~/Projects/aiomoqt/.venv/bin/python
    HAVE=$($PY -c "import importlib.metadata as m;print(m.version(\"aiomoqt\"))" 2>/dev/null || true)
    if [ -n "$MOQ_REF" ]; then
      K=~/Projects/src; mkdir -p $K
      sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -q \
        build-essential cmake pkg-config libssl-dev git >/dev/null
      for r in aiopquic aiomoqt; do
        [ -d $K/$r ] || git clone -q https://github.com/gmarzot/$r $K/$r
        git -C $K/$r fetch -q origin
      done
      git -C $K/aiopquic checkout -q -f --detach "$PQ_REF"
      git -C $K/aiopquic submodule update -q --init --recursive
      git -C $K/aiomoqt checkout -q -f --detach "$MOQ_REF"
      [ -x $PY ] || uv venv --python 3.13 ~/Projects/aiomoqt/.venv >/dev/null 2>&1
      uv pip install --python $PY -q pip cython==3.3.0 setuptools==84.2.1 setuptools-scm==10.3.4 \
        wheel build certifi sortedcontainers
      (cd $K/aiopquic && PATH=$(dirname $PY):$PATH VIRTUAL_ENV=$(dirname $(dirname $PY)) PYTHON=$PY \
        ./build.sh) > $K/build.log 2>&1 || { tail -20 $K/build.log; exit 1; }
      uv pip install --python $PY -q --no-deps --reinstall $K/aiomoqt
      echo "  $(hostname): aiopquic $(git -C $K/aiopquic log --oneline -1 | cut -c1-50)"
      echo "  $(hostname): aiomoqt  $(git -C $K/aiomoqt log --oneline -1 | cut -c1-50)"
    elif [ "$HAVE" != "$WANT" ]; then
      [ -x $PY ] || uv venv --python 3.13 ~/Projects/aiomoqt/.venv >/dev/null 2>&1
      uv pip install --python $PY "$SPEC" >/dev/null 2>&1
    fi
    [ -f ~/Projects/bench/cert.pem ] || openssl req -x509 -newkey ec \
      -pkeyopt ec_paramgen_curve:prime256v1 -keyout ~/Projects/bench/key.pem \
      -out ~/Projects/bench/cert.pem -days 30 -nodes -subj "/CN=localhost" \
      -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
    echo "  $(hostname): aiomoqt $($PY -c "import importlib.metadata as m;print(m.version(\"aiomoqt\"))" 2>/dev/null || echo MISSING)"'; } &
  aiomoqt_jobs+=("$!")
done
aiomoqt_failed=0
for pid in "${aiomoqt_jobs[@]}"; do wait "$pid" || aiomoqt_failed=1; done
(( aiomoqt_failed == 0 )) || { echo 'aiomoqt provisioning failed; stopping.' >&2; exit 1; }

# 2. The prebuilt tree from $SRC, only when SRC is set: this authorizes the
#    relay VM's key there.
if [[ -n $SRC ]]; then
$SSH ubuntu@"$RELAY" "set -e
  [ -f ~/.ssh/xfer ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/xfer
  cat ~/.ssh/xfer.pub" > /tmp/.oci_xfer.pub
SRC_HOST=${SRC%%:*}; SRC_DIR=Projects; [[ $SRC == *:* ]] && SRC_DIR=${SRC#*:}
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$SRC_HOST" \
  "grep -qxF \"\$(cat)\" ~/.ssh/authorized_keys 2>/dev/null || cat >> ~/.ssh/authorized_keys" < /tmp/.oci_xfer.pub
$SSH ubuntu@"$RELAY" "rsync -a -e 'ssh -i ~/.ssh/xfer -o StrictHostKeyChecking=accept-new' \
  $SRC_HOST:$SRC_DIR/ ~/Projects/ && du -sh ~/Projects/* | sed 's|.*Projects/|  |'"

# 3. Restore the locally patched artifacts. The rsync above pulls the source host's
#    CI-staged copies over the top of them, and the failure is quiet: a
#    moqtest_server without --announce_namespace publishes fine for moqx and cf
#    and silently serves zero subscribers on both moq-dev backends, which reads
#    as a relay difference rather than a stale binary.
LOCAL_MOQTEST=${LOCAL_MOQTEST:-$HOME/Projects/moq/openmoq/moqx/.scratch/standalone-build/moxygen/moqtest/moqtest_server}
if [[ -x $LOCAL_MOQTEST ]] && { $LOCAL_MOQTEST --help 2>&1 || true; } | grep -q announce_namespace; then
  scp -q -i "$KEY" "$LOCAL_MOQTEST" ubuntu@"$RELAY":~/Projects/moxygen/bin/moqtest_server
  echo "  pushed patched moqtest_server"
else
  echo "  WARNING: no patched moqtest_server at $LOCAL_MOQTEST - moq-dev runs will get 0 subs" >&2
fi
$SSH ubuntu@"$RELAY" 'pkill -x moq-relay-quinn; pkill -x moq-relay-quich; pkill -x moqx; pkill -x moq-relay-ietf; sleep 1' >/dev/null 2>&1
for b in quinn quiche; do
  src=/tmp/moq-relay-$b.new
  [[ -f $src ]] || continue
  scp -q -i "$KEY" "$src" ubuntu@"$RELAY":~/Projects/moq-dev/moq-relay-$b
  $SSH ubuntu@"$RELAY" "chmod +x ~/Projects/moq-dev/moq-relay-$b"
  echo "  pushed patched moq-relay-$b"
done
else
  echo "  no SRC: no prebuilt tree (moqperf runs and relay-smoke.sh need one)"
  # moxygen's release test tools, for moqperf-ramp.
  for H in "$RELAY" "${LOADHOSTS[@]}"; do
    $SSH ubuntu@"$H" "REPO=$MOXYGEN_REPO REL=$MOXYGEN_RELEASE"'; set -e
      export DEBIAN_FRONTEND=noninteractive
      sudo -E apt-get -o DPkg::Lock::Timeout=300 install -y -q libgflags2.2 libjemalloc2 >/dev/null
      case $(uname -m) in aarch64) A=arm64 ;; x86_64) A=amd64 ;; *) echo "no moxygen build for $(uname -m)" >&2; exit 1 ;; esac
      D=~/Projects/moxygen; mkdir -p $D/bin
      [ "$(cat $D/bin/RELEASE 2>/dev/null)" = "$REL-$A" ] || {
        T=$(mktemp -d)
        curl -fsSL "https://github.com/$REPO/releases/download/$REL/moxygen-linux-$A.tar.gz" | tar xzf - -C $T
        for b in moqtest_server moqperf_test_client; do
          install -m 755 "$(find $T -path "*/bin/$b" -type f | head -1)" $D/bin/$b
        done
        rm -rf $T; echo "$REL-$A" > $D/bin/RELEASE
      }
      $D/bin/moqperf_test_client --help 2>&1 | grep -q subscriber_ramp
      echo "  $(hostname): moxygen $REL-$A test tools"' || echo "  $H: moxygen test tools FAILED" >&2 &
  done; wait
fi

# 4. moq-bench, built once on the relay; step 5 copies it to every load host.
has moqbench && $SSH ubuntu@"$RELAY" "bash -s -- $MOQBENCH_REV $LIBQUICR_REV" < remote/build-moqbench.sh

# 5. Every load host gets the load tools, pulled over the free private network.
PULL=""
[[ -n $SRC ]] && PULL+=" ubuntu@$RELAY_PRIV:~/Projects/moxygen ubuntu@$RELAY_PRIV:~/Projects/bench"
has moqbench && PULL+=" ubuntu@$RELAY_PRIV:~/Projects/moq-bench"
if [[ -n $PULL ]]; then
$SSH ubuntu@"$RELAY" "[ -f ~/.ssh/xfer ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/xfer"
PUB=$($SSH ubuntu@"$RELAY" 'cat ~/.ssh/xfer.pub')
$SSH ubuntu@"$RELAY" "grep -qxF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys"
$SSH ubuntu@"$RELAY" 'cat ~/.ssh/xfer' > /tmp/.oci_xfer.key && chmod 600 /tmp/.oci_xfer.key
for C in "${LOADHOSTS[@]}"; do
  scp -q -i "$KEY" /tmp/.oci_xfer.key ubuntu@"$C":~/.ssh/xfer
  $SSH ubuntu@"$C" "chmod 600 ~/.ssh/xfer; mkdir -p ~/Projects
    rsync -a -e 'ssh -i ~/.ssh/xfer -o StrictHostKeyChecking=accept-new' $PULL ~/Projects/
    echo \"  \$(hostname): \$(ls ~/Projects | tr '\\n' ' ')\"" &
done; wait
rm -f /tmp/.oci_xfer.key /tmp/.oci_xfer.pub
fi

# 6. The relays under test. One deployed from a binary built elsewhere takes
#    the binary as an argument: ./bin/relay-deploy.sh <relay> <binary>.
for R in $RELAYS; do
  [[ $R == none ]] || ./bin/relay-deploy.sh "$R"
done

echo
echo "provisioned. verify with: SUBS=20 ./bin/relay-load.sh adaptive moqx 30"
