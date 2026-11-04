# Hosts for the commands in bin/. Sourced from the repo root, before SSH is set.
#
# They come from .testbed/inventory.json, which ./bin/testbed.sh writes for the
# selected environment, so no command here knows which cloud made the hosts.
# For machines no testbed created, name them instead (public addresses, plus the
# relay's private one when the others reach it over a private network):
#
#   RELAY_HOST=<ip> [RPRIV_HOST=<ip>] CLIENT_HOSTS="<ip> ..." [PROBE_HOSTS="<ip> ..."]
#
# Sets RELAY, RPRIV, CLIENTS[], PROBES[], TESTBED, and KEY (SSH private key) and
# RUSER, both of which the environment can override.
CLIENTS=() PROBES=()
if [[ -n ${RELAY_HOST:-} ]]; then
  RELAY=$RELAY_HOST
  RPRIV=${RPRIV_HOST:-$RELAY_HOST}
  read -r -a CLIENTS <<< "${CLIENT_HOSTS:-}"
  read -r -a PROBES <<< "${PROBE_HOSTS:-}"
  TESTBED=manual
elif [[ -f .testbed/inventory.json ]]; then
  eval "$(python3 - .testbed/inventory.json <<'PY'
import json, shlex, sys
inv = json.load(open(sys.argv[1]))
q = shlex.quote
print(f"TESTBED={q(inv.get('deployment') or inv.get('env', ''))}")
print(f"RELAY={q(inv['relay']['public'])}")
print(f"RPRIV={q(inv['relay']['private'])}")
print("CLIENTS=(" + " ".join(q(h["public"]) for h in inv["clients"]) + ")")
print("PROBES=(" + " ".join(q(h["public"]) for h in inv["probes"]) + ")")
print(f"INV_KEY={q(inv.get('ssh_key_file', ''))}")
print(f"INV_USER={q(inv.get('user', ''))}")
PY
)"
else
  echo "no testbed selected: ./bin/testbed.sh <deployment> up (or output), or set RELAY_HOST and CLIENT_HOSTS" >&2
  exit 1
fi
[[ ${#CLIENTS[@]} -gt 0 ]] || { echo "no client hosts in testbed $TESTBED" >&2; exit 1; }
KEY=${KEY:-${INV_KEY:-$HOME/.ssh/id_ed25519}}
RUSER=${RUSER:-${INV_USER:-ubuntu}}
