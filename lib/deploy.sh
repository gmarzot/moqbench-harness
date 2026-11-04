# Installing a relay build on the relay host, the same way for every relay.
# Sourced by bin/relay-deploy.sh, after lib/run.sh; a relays.local.sh installer
# uses the same two functions.
#
# A build is staged beside its final place as <path>.new, with paths relative
# to $RROOT (moqx/bin/moqx, myrelay/my-relay). deploy_install checks that it loads
# there, keeps the binary it replaces as <path>.prev, and records a label and
# the sha256 in <path>.rev, which versions.txt reports. deploy_check refuses to
# replace a binary that no deploy put there, one copied in by hand, unless
# FORCE=1.
#
# Caller provides: SSH, RUSER, RROOT.

# deploy_check <host> <path> <label>: 0 to install, 1 already installed, 2 refused.
deploy_check() {
  local host=$1 path=$2 label=$3 state have_rev have_sha cur_sha
  state=$($SSH -n "$RUSER@$host" "cd $RROOT && mkdir -p \$(dirname $path) &&
    r=\$(sed -n 1p $path.rev 2>/dev/null); s=\$(sed -n 2p $path.rev 2>/dev/null)
    c=\$(sha256sum $path 2>/dev/null | cut -d' ' -f1); echo \"\${r:--} \${s:--} \${c:--}\"") || return 2
  read -r have_rev have_sha cur_sha <<< "$state"
  if [[ $cur_sha != - && $cur_sha != "$have_sha" && ${FORCE:-0} != 1 ]]; then
    echo "  $path on the relay host (sha ${cur_sha:0:16}) was not put there by a deploy; keeping it." >&2
    echo "  FORCE=1 replaces it (kept as $path.prev)." >&2
    return 2
  fi
  if [[ $label == "$have_rev" && $cur_sha == "$have_sha" ]]; then echo "  $path: $label already installed"; return 1; fi
  return 0
}

# deploy_install <host> <path> <label>: put the staged <path>.new in place. One
# that cannot load there (a missing library, a newer glibc than the host's) is
# removed and the installed one kept, so that shows now rather than when a run
# starts.
deploy_install() {
  $SSH "$RUSER@$1" "bash -s -- $RROOT $2 $3" <<'INSTALL'
set -euo pipefail
cd "$1"; p=$2; label=$3
out=$(timeout 10 "./$p.new" --help 2>&1) && rc=0 || rc=$?
if [[ $rc == 126 || $rc == 127 ]] || grep -qE "error while loading shared libraries|GLIBC.* not found" <<< "$out"; then
  echo "  $p.new does not run on $(hostname):" >&2; head -3 <<< "$out" >&2
  rm -f "$p.new"; exit 1
fi
[[ -e $p ]] && ln -f "$p" "$p.prev"
mv -f "$p.new" "$p"
printf '%s\n%s\n' "$label" "$(sha256sum "$p" | cut -d' ' -f1)" > "$p.rev"
echo "  $(hostname): $p $label installed"
if pgrep -x "$(basename "$p" | cut -c1-15)" >/dev/null; then
  echo "  $(basename "$p") is still running the previous build until restarted"
fi
INSTALL
}
