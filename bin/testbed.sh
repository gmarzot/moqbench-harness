#!/usr/bin/env bash
# Create, power and destroy a testbed: the relay host, its load generators and
# the probe, in whichever cloud its environment is defined for.
#
#   ./bin/testbed.sh                                      # list environments and deployments
#   ./bin/testbed.sh <deployment> up [--env <env>] [-y]   # create (or resize) a deployment
#   ./bin/testbed.sh <deployment> plan [--env <env>]      # preview it
#   ./bin/testbed.sh <deployment> stop|start              # power off / on; disks and network kept
#   ./bin/testbed.sh <deployment> destroy [-y]            # delete what this deployment created
#   ./bin/testbed.sh <deployment> output|use              # show / select its hosts
#
# An environment is a definition, <cloud>/envs/<env>.tfvars and/or
# <env>.local.tfvars: tenancy, region, image, sizes. A deployment is a named
# instance of one; the name is yours, and becomes the prefix of everything it
# creates. A new deployment's environment is --env, else the environment with
# the deployment's own name, else $TESTBED_ENV (set it once in your shell
# profile for a standing default), else the only one defined; it is remembered
# after that. -y approves up and destroy without the prompt.
#
# The cloud's own testbed.sh (oci/, aws/) does the work. Selecting a
# deployment writes .testbed/inventory.json, the host list every other command
# in bin/ reads, so none of them depends on the cloud.
set -euo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
source "$ROOT/lib/dotenv.sh"

list() {
  local f cloud n env
  echo "environments:" >&2
  for f in "$ROOT"/*/envs/*.tfvars; do
    [[ -e $f ]] || continue
    cloud=$(basename "$(dirname "$(dirname "$f")")") n=$(basename "$f" .tfvars)
    echo "${n%.local} $cloud"
  done | sort -u | while read -r n cloud; do printf '  %-24s (%s)\n' "$n" "$cloud" >&2; done
  echo "deployments:" >&2
  for f in "$ROOT"/.testbed/deployments/*; do
    [[ -e $f ]] || { echo "  (none)" >&2; break; }
    read -r cloud env < "$f"
    printf '  %-24s (%s, environment %s)\n' "$(basename "$f")" "$cloud" "$env" >&2
  done
}

if [[ $# -lt 2 ]]; then
  echo "usage: ./bin/testbed.sh <deployment> plan|up|stop|start|destroy|output|use [--env <environment>] [-y]" >&2
  list
  exit 2
fi

# The cloud: from the deployment's record, else from the environment it will
# use (--env, the deployment's own name, $TESTBED_ENV), else the only cloud
# with environments.
DEPLOY=$1 CLOUD="" ENVNAME=""
args=("$@")
for (( i = 2; i < ${#args[@]} - 1; i++ )); do [[ ${args[$i]} == --env ]] && ENVNAME=${args[$(( i + 1 ))]}; done
defines() { for f in "$ROOT"/*/envs/"$1".tfvars "$ROOT"/*/envs/"$1".local.tfvars; do
  [[ -e $f ]] && basename "$(dirname "$(dirname "$f")")"; done | sort -u; }
if [[ -z $ENVNAME ]]; then
  if [[ -n $(defines "$DEPLOY") ]]; then ENVNAME=$DEPLOY; else ENVNAME=${TESTBED_ENV:-}; fi
fi
if [[ -f $ROOT/.testbed/deployments/$DEPLOY ]]; then
  read -r CLOUD _ < "$ROOT/.testbed/deployments/$DEPLOY"
else
  mapfile -t CLOUDS < <([[ -n $ENVNAME ]] && defines "$ENVNAME")
  [[ ${#CLOUDS[@]} -eq 0 ]] && mapfile -t CLOUDS < <(for f in "$ROOT"/*/envs/*.tfvars; do
    [[ -e $f ]] && basename "$(dirname "$(dirname "$f")")"; done | sort -u)
  case ${#CLOUDS[@]} in
    1) CLOUD=${CLOUDS[0]} ;;
    0) echo "no environments defined" >&2; list; exit 2 ;;
    *) echo "environments exist for several clouds (${CLOUDS[*]}): name one with --env" >&2; list; exit 2 ;;
  esac
fi
exec "$ROOT/$CLOUD/testbed.sh" "$@"
