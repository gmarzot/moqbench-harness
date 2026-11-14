# Hosts for the commands in bin/. Sourced from the repo root, before SSH is set.
#
# They come from .testbed/inventory.json, which ./bin/testbed.sh writes for the
# selected environment, so no command here knows which cloud made the hosts.
# For machines no testbed created, name them instead (public addresses, plus the
# relays' private ones when the others reach them over a private network):
#
#   RELAY_HOST=<ip> [RPRIV_HOST=<ip>] CLIENT_HOSTS="<ip> ..." [PROBE_HOSTS="<ip> ..."]
#   [RELAY_HOSTS="<ip> ..." RELAY_PRIV_HOSTS="<ip> ..."]   # several relays, RELAY_HOST first
#
# Sets RELAY, RPRIV (the first relay), RELAYS[], RELAYS_PRIV[], RELAY_NAMES[],
# CLIENTS[], CLIENTS_PRIV[], PROBES[], PROBES_PRIV[], TESTBED, and KEY (SSH
# private key) and RUSER, both of which the environment can override.
CLIENTS=() PROBES=() RELAYS=() RELAYS_PRIV=() RELAY_NAMES=() CLIENTS_PRIV=() PROBES_PRIV=()
if [[ -n ${RELAY_HOST:-} ]]; then
  RELAY=$RELAY_HOST
  RPRIV=${RPRIV_HOST:-$RELAY_HOST}
  read -r -a RELAYS <<< "${RELAY_HOSTS:-$RELAY_HOST}"
  read -r -a RELAYS_PRIV <<< "${RELAY_PRIV_HOSTS:-${RELAY_HOSTS:-$RPRIV}}"
  for i in "${!RELAYS[@]}"; do RELAY_NAMES+=("relay-$(printf %02d $(( i + 1 )))"); done
  read -r -a CLIENTS <<< "${CLIENT_HOSTS:-}"
  read -r -a PROBES <<< "${PROBE_HOSTS:-}"
  read -r -a CLIENTS_PRIV <<< "${CLIENT_PRIV_HOSTS:-${CLIENT_HOSTS:-}}"
  read -r -a PROBES_PRIV <<< "${PROBE_PRIV_HOSTS:-${PROBE_HOSTS:-}}"
  TESTBED=manual
elif [[ -f .testbed/inventory.json ]]; then
  eval "$(python3 - .testbed/inventory.json <<'PY'
import json, shlex, sys
inv = json.load(open(sys.argv[1]))
q = shlex.quote
print(f"TESTBED={q(inv.get('deployment') or inv.get('env', ''))}")
print(f"RELAY={q(inv['relay']['public'])}")
print(f"RPRIV={q(inv['relay']['private'])}")
relays = inv.get('relays') or [dict(inv['relay'], name='relay-01')]
print("RELAYS=(" + " ".join(q(h["public"]) for h in relays) + ")")
print("RELAYS_PRIV=(" + " ".join(q(h["private"]) for h in relays) + ")")
print("RELAY_NAMES=(" + " ".join(q(h["name"]) for h in relays) + ")")
print("CLIENTS=(" + " ".join(q(h["public"]) for h in inv["clients"]) + ")")
print("PROBES=(" + " ".join(q(h["public"]) for h in inv["probes"]) + ")")
print("CLIENTS_PRIV=(" + " ".join(q(h["private"]) for h in inv["clients"]) + ")")
print("PROBES_PRIV=(" + " ".join(q(h["private"]) for h in inv["probes"]) + ")")
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
