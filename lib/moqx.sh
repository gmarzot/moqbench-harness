# Helpers for installing a moqx release tarball. Sourced by bin/relay-deploy.sh;
# pure functions of their arguments (plus MOQX_ASSET, MOQX_SCRIPTS_REF).

# moqx_arch <uname -m>: the release's name for a host architecture.
moqx_arch() {
  case $1 in
    aarch64|arm64) echo arm64 ;;
    x86_64|amd64)  echo amd64 ;;
    *) echo "  no moqx build for architecture $1" >&2; return 1 ;;
  esac
}

# moqx_asset <arch>: the release tarball to download; MOQX_ASSET overrides.
moqx_asset() { echo "${MOQX_ASSET:-moqx-linux-$1.tar.gz}"; }

# moqx_elf_arch <file>: arm64 or amd64, from the ELF header's e_machine.
moqx_elf_arch() {
  local h
  h=$(od -An -tx1 -N20 "$1" 2>/dev/null | tr -d ' \n')
  [[ ${h:0:8} == 7f454c46 ]] || { echo "  $1 is not an ELF binary" >&2; return 1; }
  case ${h:36:4} in
    b700) echo arm64 ;;
    3e00) echo amd64 ;;
    *) echo "  $1: unknown ELF machine 0x${h:38:2}${h:36:2}" >&2; return 1 ;;
  esac
}

# moqx_script_refs <VERSION>: git refs to fetch moqx-run.sh and the perf config
# from, most specific first: MOQX_SCRIPTS_REF, VERSION itself, the commit a
# CI build's VERSION ends in, then the release it starts with.
moqx_script_refs() {
  local v=$1 refs=()
  [[ -n ${MOQX_SCRIPTS_REF:-} ]] && refs+=("$MOQX_SCRIPTS_REF")
  refs+=("$v")
  [[ $v =~ -([0-9a-f]{7,40})$ ]] && refs+=("${BASH_REMATCH[1]}")
  [[ $v =~ ^(v[0-9]+\.[0-9]+\.[0-9]+) ]] && refs+=("${BASH_REMATCH[1]}")
  printf '%s\n' "${refs[@]}" | awk '!seen[$0]++'
}
