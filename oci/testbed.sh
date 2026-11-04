#!/usr/bin/env bash
# OCI implementation of ./bin/testbed.sh, which calls this for deployments of
# environments defined in oci/envs/.
#
#   ./bin/testbed.sh <deployment> plan|up [--env <environment>] [-y]
#   ./bin/testbed.sh <deployment> stop|start
#   ./bin/testbed.sh <deployment> destroy [-y]
#   ./bin/testbed.sh <deployment> output|use       # show / select its hosts
#
# An environment is the definition: envs/<env>.tfvars (shared, tracked) and/or
# envs/<env>.local.tfvars (one tester's settings, gitignored), applied in that
# order so the local file wins. A deployment is one named instance of it: any
# name, which becomes its Terraform workspace and the prefix of every resource
# it creates, so deployments never collide and the destroy prompt names the
# one you made. A new deployment's environment is, in order: --env, the
# environment with the deployment's own name, $TESTBED_ENV, or the only one
# defined. After its first plan or up the deployment remembers it
# (.testbed/deployments/<name>), and TESTBED_ENV no longer applies to it.
#
# -y approves up and destroy without Terraform's prompt. There is no "down": it
# reads as "shut down", and it once destroyed a testbed meant to be stopped.
#
# The OCI CLI calls here (session check, stop/start) use the same profile,
# region and auth that Terraform does: from the local file, else the shared
# one, else TF_VAR_* in the environment. Credentials stay in ~/.oci/config.
#
# Selecting a deployment (up, start, output, use) writes the repo's
# .testbed/inventory.json: the cloud-neutral host list every other command in
# bin/ reads. destroy removes it.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source ../lib/dotenv.sh
INVENTORY=../.testbed/inventory.json

environments() { ls envs/*.tfvars 2>/dev/null | sed -E 's#envs/##; s#(\.local)?\.tfvars$##' | sort -u; }
usage() {
  echo "usage: ./bin/testbed.sh <deployment> plan|up|stop|start|destroy|output|use [--env <environment>] [-y]" >&2
  echo "OCI environments:" >&2
  local n kind
  for n in $(environments); do
    kind=""
    [[ -f envs/$n.tfvars ]] && kind="shared"
    [[ -f envs/$n.local.tfvars ]] && kind="${kind:+$kind + }local"
    [[ -f envs/$n.tfvars && ! -f envs/$n.local.tfvars && -f envs/$n.local.tfvars.example ]] \
      && kind+=", needs envs/$n.local.tfvars"
    printf '  %-24s %s\n' "$n" "($kind)" >&2
  done
  echo "new tenancy: cp oci/envs/my-tenancy.local.tfvars.example oci/envs/<name>.local.tfvars" >&2
  exit 2
}

[[ $# -ge 2 ]] || usage
DEPLOY=$1 ACTION=$2; shift 2
ENVNAME="" APPROVE=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --env) ENVNAME=${2:?--env needs an environment name}; shift 2 ;;
    -y)    APPROVE=(-auto-approve); shift ;;
    *)     echo "unknown argument: $1" >&2; usage ;;
  esac
done
[[ $DEPLOY =~ ^[a-z][a-z0-9-]{0,30}$ ]] || {
  echo "deployment names: lowercase letters, digits and '-', starting with a letter, at most 31" >&2; exit 2; }

# Which environment: the deployment's record, else --env, else an environment
# with the deployment's own name, else $TESTBED_ENV, else the only one defined.
RECORD=../.testbed/deployments/$DEPLOY
RECORDED=""
[[ -f $RECORD ]] && read -r _ RECORDED < "$RECORD"
if [[ -n $RECORDED && -n $ENVNAME && $ENVNAME != "$RECORDED" ]]; then
  echo "deployment $DEPLOY was made from environment $RECORDED; it cannot be planned against $ENVNAME" >&2
  exit 2
fi
ENVNAME=${ENVNAME:-$RECORDED}
if [[ -z $ENVNAME && ( -f envs/$DEPLOY.tfvars || -f envs/$DEPLOY.local.tfvars ) ]]; then ENVNAME=$DEPLOY; fi
if [[ -z $ENVNAME && -n ${TESTBED_ENV:-} && ( $ACTION == up || $ACTION == plan ) ]]; then
  [[ -f envs/$TESTBED_ENV.tfvars || -f envs/$TESTBED_ENV.local.tfvars ]] || {
    echo "TESTBED_ENV=$TESTBED_ENV is not an environment here (one of: $(environments | xargs))" >&2; exit 2; }
  ENVNAME=$TESTBED_ENV
  echo "deployment $DEPLOY: environment $ENVNAME (TESTBED_ENV)" >&2
fi
if [[ -z $ENVNAME ]]; then
  mapfile -t ALL < <(environments)
  if [[ ${#ALL[@]} -eq 1 && ( $ACTION == up || $ACTION == plan ) ]]; then
    ENVNAME=${ALL[0]}
    echo "deployment $DEPLOY: environment $ENVNAME (the only one defined)" >&2
  elif [[ $ACTION == up || $ACTION == plan ]]; then
    echo "deployment $DEPLOY is new: name its environment with --env (one of: ${ALL[*]})" >&2; exit 2
  else
    echo "no deployment named $DEPLOY (./bin/testbed.sh lists them)" >&2; exit 2
  fi
fi

SHARED=envs/$ENVNAME.tfvars LOCAL=envs/$ENVNAME.local.tfvars
VARFILES=()
[[ -f $SHARED ]] && VARFILES+=(-var-file="$SHARED")
[[ -f $LOCAL ]] && VARFILES+=(-var-file="$LOCAL")
[[ ${#VARFILES[@]} -gt 0 ]] || { echo "no such environment: $ENVNAME" >&2; usage; }
if [[ -f $SHARED && ! -f $LOCAL && -f $LOCAL.example ]]; then
  echo "$ENVNAME needs your own settings: cp oci/$LOCAL.example oci/$LOCAL and fill it in" >&2
  exit 1
fi
VARFILES+=(-var "environment=$ENVNAME")

# name = "value" from the local file, else the shared one.
setting() {
  local f v
  for f in "$LOCAL" "$SHARED"; do
    [[ -f $f ]] || continue
    v=$(sed -n -E "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"([^\"]*)\".*/\1/p" "$f" | head -1)
    [[ -n $v ]] && { echo "$v"; return; }
  done
}
PROFILE=$(setting config_file_profile); PROFILE=${PROFILE:-${TF_VAR_config_file_profile:-DEFAULT}}
REGION=$(setting region);               REGION=${REGION:-${TF_VAR_region:-}}
AUTH=$(setting auth);                   AUTH=${AUTH:-${TF_VAR_auth:-APIKey}}
export OCI_CLI_PROFILE=$PROFILE
[[ -n $REGION ]] && export OCI_CLI_REGION=$REGION
if [[ $AUTH == SecurityToken ]]; then export OCI_CLI_AUTH=security_token; else export OCI_CLI_AUTH=api_key; fi

# A session token lasts about an hour; fail here rather than mid-apply, and
# refresh one that is about to lapse so a slow apply cannot outlive it. stdin
# is closed because an expired session makes the CLI ask whether to log in
# again, a prompt that would otherwise wait invisibly on the terminal.
session_until() {  # empty when expired; never fails, or set -e would exit silently
  { timeout 30 oci session validate --auth security_token </dev/null 2>&1 || true; } |
    sed -n -E 's/.*valid until ([0-9-]+ [0-9:]+).*/\1/p'
}
if [[ $AUTH == SecurityToken && $ACTION != use ]]; then
  UNTIL=$(session_until)
  if [[ -z $UNTIL ]]; then
    echo "OCI session for profile $PROFILE has expired (tokens last about an hour). Log in again:" >&2
    echo "  oci session authenticate --profile-name $PROFILE --region ${REGION:-<region>}   # plus your login options" >&2
    exit 1
  fi
  LEFT=$(( ($(date -d "$UNTIL" +%s) - $(date +%s)) / 60 ))
  if (( LEFT < 15 )); then
    echo "session for $PROFILE has ${LEFT} min left; refreshing" >&2
    timeout 60 oci session refresh --auth security_token </dev/null >/dev/null 2>&1 && UNTIL=$(session_until)
  fi
  echo "session: $PROFILE valid until ${UNTIL:-unknown}" >&2
fi

[[ -d .terraform ]] || terraform init -input=false >/dev/null
terraform workspace select -or-create "$DEPLOY" >/dev/null
echo "deployment: $DEPLOY (environment $ENVNAME, profile $PROFILE${REGION:+, $REGION})" >&2

# The host list the commands in bin/ read, from this deployment's outputs.
inventory() {
  if terraform output -json | python3 ../lib/inventory.py --deployment "$DEPLOY" \
      --environment "$ENVNAME" --provider oci --region "$REGION" --out "$INVENTORY"; then
    echo "hosts: $DEPLOY -> .testbed/inventory.json" >&2
  else
    echo "no hosts recorded for $DEPLOY yet" >&2
  fi
}

power() {
  local action=$1 ids
  ids=$(terraform output -json instance_ids 2>/dev/null | python3 -c 'import json,sys;[print(i) for i in json.load(sys.stdin)]') || true
  [[ -n $ids ]] || { echo "no instances recorded for $DEPLOY; nothing to $action" >&2; exit 1; }
  # SOFTSTOP lets the guest shut down cleanly; billing stops either way. An
  # in-guest `shutdown` does NOT stop billing — only the API does.
  local wait=()
  [[ $action == START ]] && wait=(--wait-for-state RUNNING --max-wait-seconds 600)
  for id in $ids; do
    oci compute instance action --instance-id "$id" --action "$action" "${wait[@]}" \
      --query 'data."display-name"' --raw-output >/dev/null &
  done
  wait
  echo "$action sent to $(wc -w <<< "$ids") instances" >&2
}

case $ACTION in
  plan)   terraform plan -input=false "${VARFILES[@]}" ;;
  up)
    terraform apply -input=false ${APPROVE[@]+"${APPROVE[@]}"} "${VARFILES[@]}"
    mkdir -p "$(dirname "$RECORD")" && echo "oci $ENVNAME" > "$RECORD"
    inventory ;;
  destroy)
    # Terraform's prompt says "all resources in workspace <deployment>". That
    # means only what this deployment created.
    echo "destroy deletes the $(terraform state list | wc -l) resources deployment $DEPLOY created," >&2
    echo "all named $DEPLOY-*. Nothing else in the compartment or tenancy is touched." >&2
    echo "To power off and keep everything instead: ./bin/testbed.sh $DEPLOY stop" >&2
    terraform destroy -input=false ${APPROVE[@]+"${APPROVE[@]}"} "${VARFILES[@]}"
    if [[ -z $(terraform state list) ]]; then
      rm -f "$RECORD"
      if [[ -f $INVENTORY ]] && python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("deployment") != sys.argv[2])' "$INVENTORY" "$DEPLOY"; then
        rm -f "$INVENTORY"
      fi
      terraform workspace select default >/dev/null && terraform workspace delete "$DEPLOY" >/dev/null
      echo "deployment $DEPLOY is gone" >&2
    fi ;;
  down)
    echo "'down' is ambiguous, so it does nothing. Choose:" >&2
    echo "  ./bin/testbed.sh $DEPLOY stop      # power off; keeps disks and network, compute billing stops" >&2
    echo "  ./bin/testbed.sh $DEPLOY destroy   # delete what $DEPLOY created" >&2
    exit 2 ;;
  output) terraform output; inventory ;;
  use)    inventory ;;
  stop)   power SOFTSTOP ;;
  start)
    power START
    # A public IP can change across a stop and start; refresh the state so the
    # host list follows it. Refresh-only changes no infrastructure.
    terraform apply -refresh-only -auto-approve -input=false "${VARFILES[@]}" >/dev/null && inventory
    echo "re-run ./bin/testbed-tune.sh: device tuning does not survive a stop" >&2 ;;
  *)      usage ;;
esac
