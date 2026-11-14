#!/usr/bin/env bash
# One local time source for cross-host latency. The reference host (the first
# probe, or REF and REF_PRIV) serves time to every other host on the private
# network and is itself disciplined by the provider's link-local time service.
# Offsets then share one nearby source; record them per run (chronyc tracking).
#
#   ./bin/testbed-chrony.sh            # configure every host, then report
#   ./bin/testbed-chrony.sh --check    # report only: each host's source, offset and RMS offset
#
# CHRONY_UPSTREAM sets the reference's upstream (default 169.254.169.123 on aws,
# 169.254.169.254 on oci, none elsewhere). Hosts poll the reference every 4 s.
# The settings live in /etc/chrony/conf.d/moqbench.conf and persist.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh
MODE=apply
[[ ${1:-} == --check ]] && MODE=check
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
REF=${REF:-${PROBES[0]:-}}
REF_PRIV=${REF_PRIV:-${PROBES_PRIV[0]:-}}
[[ -n $REF && -n $REF_PRIV ]] || { echo "no reference host: add a probe node, or set REF and REF_PRIV" >&2; exit 2; }
PROVIDER=${PROVIDER:-$(python3 -c 'import json; print(json.load(open(".testbed/inventory.json")).get("provider", ""))' 2>/dev/null)}
case $PROVIDER in
  aws) UPSTREAM=169.254.169.123 ;;
  oci) UPSTREAM=169.254.169.254 ;;
  *)   UPSTREAM= ;;
esac
UPSTREAM=${CHRONY_UPSTREAM-$UPSTREAM}
OTHERS=()
for h in "${RELAYS[@]}" "${CLIENTS[@]}" ${PROBES[@]+"${PROBES[@]}"}; do [[ $h == "$REF" ]] || OTHERS+=("$h"); done

# configure <host> <conf text>
configure() {
  $SSH "$RUSER@$1" "bash -s" <<CONF
set -e
command -v chronyc >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -q chrony >/dev/null
sudo mkdir -p /etc/chrony/conf.d
grep -q '^confdir /etc/chrony/conf.d' /etc/chrony/chrony.conf || echo 'confdir /etc/chrony/conf.d' | sudo tee -a /etc/chrony/chrony.conf >/dev/null
printf '%s\n' "$2" | sudo tee /etc/chrony/conf.d/moqbench.conf >/dev/null
sudo systemctl restart chrony
sudo chronyc -a makestep >/dev/null 2>&1 || true
CONF
}

if [[ $MODE == apply ]]; then
  ref_conf="local stratum 10
allow 10.0.0.0/8
allow 172.16.0.0/12
allow 192.168.0.0/16"
  [[ -n $UPSTREAM ]] && ref_conf="server $UPSTREAM iburst minpoll 4 maxpoll 4 prefer
$ref_conf"
  configure "$REF" "$ref_conf" || { echo "reference $REF: chrony setup failed" >&2; exit 1; }
  sleep 5
  for h in "${OTHERS[@]}"; do
    configure "$h" "server $REF_PRIV iburst minpoll 2 maxpoll 2 prefer" &
  done; wait
  sleep 10
fi

echo "reference $REF ($REF_PRIV) upstream ${UPSTREAM:-none}"
for h in "$REF" "${OTHERS[@]}"; do
  $SSH -n "$RUSER@$h" 'printf "%-16s %s\n" "$(hostname)" "$(chronyc -n tracking 2>/dev/null |
    awk -F": " "/^Reference ID/{r=\$2} /^System time/{s=\$2} /^RMS offset/{o=\$2} END{print \"ref=\" r \" | \" s \" | rms \" o}")"' 2>&1
done
