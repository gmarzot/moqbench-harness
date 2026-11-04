#!/usr/bin/env bash
# AWS implementation of ./bin/testbed.sh, which calls this for deployments of
# environments defined in aws/envs/.
#
#   ./bin/testbed.sh <deployment> plan|up [--env <environment>] [-y]
#   ./bin/testbed.sh <deployment> stop|start
#   ./bin/testbed.sh <deployment> destroy [-y]
#   ./bin/testbed.sh <deployment> output|use       # show / select its hosts
#
# Environments, deployments, -y and the deployment record work exactly as in
# oci/testbed.sh. A shared environment needs a local file beside it with your
# own settings (envs/my-account.local.tfvars.example).
#
# The AWS CLI calls here (identity check, stop/start) use the same profile and
# region that Terraform does: from the local file, else the shared one, else
# TF_VAR_* (and AWS_PROFILE). Credentials stay in ~/.aws.
#
# Selecting a deployment (up, start, output, use) writes the repo's
# .testbed/inventory.json; destroy removes it.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source ../lib/dotenv.sh
INVENTORY=../.testbed/inventory.json

environments() { ls envs/*.tfvars 2>/dev/null | sed -E 's#envs/##; s#(\.local)?\.tfvars$##' | sort -u; }
usage() {
  echo "usage: ./bin/testbed.sh <deployment> plan|up|stop|start|destroy|output|use [--env <environment>] [-y]" >&2
  echo "AWS environments:" >&2
  local n kind
  for n in $(environments); do
    kind=""
    [[ -f envs/$n.tfvars ]] && kind="shared"
    [[ -f envs/$n.local.tfvars ]] && kind="${kind:+$kind + }local"
    [[ -f envs/$n.tfvars && ! -f envs/$n.local.tfvars ]] && kind+=", needs envs/$n.local.tfvars"
    printf '  %-24s %s\n' "$n" "($kind)" >&2
  done
  echo "new account: cp aws/envs/my-account.local.tfvars.example aws/envs/<name>.local.tfvars" >&2
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
if [[ -f $SHARED && ! -f $LOCAL ]]; then
  echo "$ENVNAME needs your own settings: cp aws/envs/my-account.local.tfvars.example aws/$LOCAL and fill it in" >&2
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
PROFILE=$(setting profile);             PROFILE=${PROFILE:-${TF_VAR_profile:-${AWS_PROFILE:-}}}
REGION=$(setting region);               REGION=${REGION:-${TF_VAR_region:-us-west-2}}   # main.tf default
ACCOUNT=$(setting expected_account_id); ACCOUNT=${ACCOUNT:-${TF_VAR_expected_account_id:-}}
[[ -z $PROFILE ]] || export AWS_PROFILE=$PROFILE
export AWS_REGION=$REGION
export AWS_PAGER=""

# Fail here on expired or wrong credentials rather than mid-apply. stdin is
# closed so an SSO prompt cannot wait invisibly on the terminal.
if [[ $ACTION != use ]]; then
  HAVE=$(timeout 30 aws sts get-caller-identity --query Account --output text </dev/null 2>/dev/null) || {
    echo "AWS credentials for profile ${PROFILE:-default} are missing or expired. Log in again, e.g.:" >&2
    echo "  aws sso login${PROFILE:+ --profile $PROFILE}" >&2
    exit 1; }
  if [[ -n $ACCOUNT && $HAVE != "$ACCOUNT" ]]; then
    echo "profile ${PROFILE:-default} is account $HAVE, not expected_account_id $ACCOUNT" >&2
    exit 1
  fi
  echo "account: $HAVE (profile ${PROFILE:-default})" >&2
fi

[[ -d .terraform ]] || terraform init -input=false >/dev/null
terraform workspace select -or-create "$DEPLOY" >/dev/null
echo "deployment: $DEPLOY (environment $ENVNAME, profile ${PROFILE:-default}, $REGION)" >&2

# The host list the commands in bin/ read, from this deployment's outputs.
inventory() {
  if terraform output -json | python3 ../lib/inventory.py --deployment "$DEPLOY" \
      --environment "$ENVNAME" --provider aws --region "$REGION" --out "$INVENTORY"; then
    echo "hosts: $DEPLOY -> .testbed/inventory.json" >&2
  else
    echo "no hosts recorded for $DEPLOY yet" >&2
  fi
}

power() {
  local action=$1 ids=()
  mapfile -t ids < <(terraform output -json instance_ids 2>/dev/null |
    python3 -c 'import json,sys;[print(i) for i in json.load(sys.stdin)]' 2>/dev/null || true)
  [[ ${#ids[@]} -gt 0 ]] || { echo "no instances recorded for $DEPLOY; nothing to $action" >&2; exit 1; }
  aws ec2 "$action-instances" --instance-ids "${ids[@]}" >/dev/null
  if [[ $action == start ]]; then aws ec2 wait instance-running --instance-ids "${ids[@]}"; fi
  echo "$action sent to ${#ids[@]} instances" >&2
}

case $ACTION in
  plan)   terraform plan -input=false "${VARFILES[@]}" ;;
  up)
    terraform apply -input=false ${APPROVE[@]+"${APPROVE[@]}"} "${VARFILES[@]}"
    mkdir -p "$(dirname "$RECORD")" && echo "aws $ENVNAME" > "$RECORD"
    inventory ;;
  destroy)
    # Terraform's prompt says "all resources in workspace <deployment>". That
    # means only what this deployment created.
    echo "destroy deletes the $(terraform state list | wc -l) resources deployment $DEPLOY created," >&2
    echo "all named $DEPLOY-*. Nothing else in the account is touched." >&2
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
  stop)   power stop ;;
  start)
    power start
    # A public IP can change across a stop and start; refresh the state so the
    # host list follows it. Refresh-only changes no infrastructure.
    terraform apply -refresh-only -auto-approve -input=false "${VARFILES[@]}" >/dev/null && inventory
    echo "re-run ./bin/testbed-tune.sh: device tuning does not survive a stop" >&2 ;;
  *)      usage ;;
esac
