#!/usr/bin/env bash
# SSH to a host of the selected testbed by its role.
#
#   ./bin/testbed-ssh.sh relay                                   # interactive
#   ./bin/testbed-ssh.sh client-01 'iperf3 --version'            # one command
#   ./bin/testbed-ssh.sh relay -N -L 4500:localhost:4500         # ssh options first
#
# Roles: relay, client-01.., probe-01... Key, user and addresses come from the
# selected testbed (lib/hosts.sh), so this works the same in any cloud.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
source ./lib/dotenv.sh
source ./lib/hosts.sh

ROLE=${1:?host: relay, client-NN or probe-NN}; shift
case $ROLE in
  relay)    H=$RELAY ;;
  client-*) H=${CLIENTS[$(( 10#${ROLE#client-} - 1 ))]:-} ;;
  probe-*)  H=${PROBES[$(( 10#${ROLE#probe-} - 1 ))]:-} ;;
  *)        H="" ;;
esac
[[ -n $H ]] || { echo "no host $ROLE in testbed $TESTBED: relay, client-01..${#CLIENTS[@]}, probe-01..${#PROBES[@]}" >&2; exit 2; }

# Leading arguments that start with "-" are ssh options (with the value of the
# ones that take one); the rest is the remote command.
OPTS=()
while [[ $# -gt 0 && $1 == -* ]]; do
  OPTS+=("$1")
  if [[ $1 =~ ^-[LRDopFlJiW]$ && $# -gt 1 ]]; then OPTS+=("$2"); shift; fi
  shift
done
exec ssh -i "$KEY" -o StrictHostKeyChecking=accept-new ${OPTS[@]+"${OPTS[@]}"} "$RUSER@$H" "$@"
