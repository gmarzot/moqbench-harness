# Personal defaults from .env at the repo root (gitignored; see .env.example).
# Sourced by every command in bin/. Lines are NAME=value (optionally prefixed
# with "export", value optionally quoted); nothing in the file is executed.
# A variable already set in the environment wins, so a one-off
# TESTBED_ENV=other ./bin/testbed.sh ... still overrides .env.
load_dotenv() {
  local f=$1 line k v
  [[ -f $f ]] || return 0
  while IFS= read -r line || [[ -n $line ]]; do
    [[ $line =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    k=${BASH_REMATCH[2]} v=${BASH_REMATCH[3]}
    if [[ $v =~ ^\"(.*)\"[[:space:]]*(#.*)?$ || $v =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
      v=${BASH_REMATCH[1]}
    else
      v=${v%%[[:space:]]#*}; v=${v%"${v##*[![:space:]]}"}   # unquoted: drop a trailing comment
    fi
    [[ $v == "~/"* ]] && v=$HOME/${v#"~/"}   # paths like KEY=~/.ssh/id_ed25519
    [[ -n ${!k+set} ]] || export "$k=$v"
  done < "$f"
}
load_dotenv "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
