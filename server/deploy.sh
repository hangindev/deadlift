#!/usr/bin/env bash
# deadlift's deploy script: runs one operation on one App's Environments. Bootstrap installs it on
# the Server, owned by root, at /usr/local/lib/deadlift/deploy.sh, and it's never delivered
# (ADR 0001): a fix to it reaches a Server when `deadlift server bootstrap` is re-run. Only the
# entry command runs it, as root, after working out the App from the calling user and unpacking
# the delivery.
#
# Usage:
#   deploy.sh --app <app> --delivery <dir> deploy feature-<key> [--image <name>=<ref>]...
#
#   --app       The App, as the entry command worked it out. Never from a delivery's arguments.
#   --delivery  The unpacked delivery: compose.yaml, the App's Compose template, and token, the
#               job's registry token.
#
# Operations:
#   deploy <environment> --image <name>=<ref>...
#       Deploys the delivered template to the Environment, with each image reaching the template
#       as ${DEADLIFT_IMAGE_<NAME>}. A Feature Environment, feature-<key>, takes a free Slot of
#       the App's, or the one its key already holds, and is skipped when none is free.
#
# The template must name the one service deadlift routes to as `x-deadlift.route:
# <service>:<port>`, and that service must have a Compose healthcheck that isn't disabled
# (ADR 0002). Anything ADR 0001 refuses is refused before anything stops: template.sh, installed
# beside this script, checks the template and writes deadlift's override, which publishes the
# routed service on the host's loopback at the Environment's port and sets the App's limits.
# deadlift then starts the Environment with `docker compose up`, and waits for the routed service
# to be healthy. It waits on that service only, because `up --wait` over a whole project fails
# when a one-shot service that nothing depends on exits 0. A migration the routed service depends
# on still has to succeed first.
#
# Ports: each App has a block of 100 loopback ports from its `port_base`. The Dev Environment is
# at the base, and the Feature Environment holding Slot s at base + 10s. Tailscale Serve maps
# them on the App's own Service, svc:<app> (ADR 0003): Dev at HTTPS 443, and Slot s at
# 10000 + s, so a Feature Environment's URL stays the same while it holds its Slot.
#
# State, all root-only, under /srv/deadlift:
#   lock                            The Server's lock: one operation at a time.
#   apps/<app>/app.conf             The App's entry in the Server registry, written by
#                                   `deadlift app add`: port_base=<port> and slots=<n>, and
#                                   the per-container limits memory=<size, e.g. 512m>,
#                                   cpus=<n> and pids=<n> (template.sh has the defaults).
#   apps/<app>/environments/<env>/  An Environment: its template (compose.yaml), the variables
#                                   it was deployed with (vars.env), deadlift's override
#                                   (override.json) and, for Feature, the Slot it holds (slot).
#   deliveries/                     Deliveries while their operation runs.
#
# Every reason line starts with `deadlift: `, and the last one says what happened. Outputs for
# `deliver` are lines of their own: `deadlift-output: <name>=<value>`.
#
# Exit codes: 0 done, 1 failed, 2 refused or bad usage (nothing changed), 75 skipped.
#
# Settings, for the tests only: DEADLIFT_STATE_DIR moves the Server's state. sudo's env_reset
# never passes it on, so on a Server it's always the default.
set -euo pipefail
umask 077

state=${DEADLIFT_STATE_DIR:-/srv/deadlift}
# Where bootstrap installed this script, and template.sh beside it.
lib=$(dirname "${BASH_SOURCE[0]}")
# How long the routed service has to report healthy.
health_timeout=300
# The --image arguments: each image's reference, by name.
declare -A images=()

usage() {
  echo "deadlift: $1" >&2
  sed -n 's/^#   \(deploy\.sh .*\)/Usage: \1/p' "$0" >&2
  exit 2
}

refuse() {
  echo "deadlift: $1" >&2
  exit 2
}

fail() {
  echo "deadlift: $1" >&2
  exit 1
}

skip() {
  echo "deadlift: $1" >&2
  exit 75
}

# An output for `deliver`.
output() {
  echo "deadlift-output: $1=$2"
}

# Held for the whole run. Children inherit fd 9, so none may outlive the script.
take_lock() {
  mkdir -p "$state"
  exec 9>>"$state/lock"
  if ! flock --nonblock 9; then
    echo "Waiting for another operation on this Server to finish"
    flock 9
  fi
}

# Reads the App's entry in the Server registry into port_base and slots.
read_app() {
  local conf=$state/apps/$app/app.conf key value
  port_base='' slots=''
  [[ -f $conf ]] || refuse "$app isn't an App on this Server"
  while IFS='=' read -r key value; do
    case $key in
      port_base) port_base=$value ;;
      slots) slots=$value ;;
    esac
  done <"$conf"
  [[ $port_base =~ ^[0-9]+$ && $slots =~ ^[1-9]$ ]] ||
    fail "$app's entry in the Server registry is broken. Re-run \`deadlift app add $app\`"
}

# Sets suffix to the tailnet's MagicDNS suffix, for the App's address on its Service.
read_tailnet_suffix() {
  suffix=$(tailscale status --json | jq -r '.MagicDNSSuffix // empty') || suffix=''
  [[ -n $suffix ]] || fail "couldn't read the tailnet's name from Tailscale, so nothing was changed"
}

# Runs `docker compose` for the Environment whose files are in $1, as the project $project: its
# template, and deadlift's override once there is one. The template sees only the variables in
# vars.env: nothing from this script's own environment, or its caller's, reaches it.
compose() {
  local dir=$1 files
  shift
  files=(--file "$dir/compose.yaml")
  if [[ -f $dir/override.json ]]; then files+=(--file "$dir/override.json"); fi
  env -i PATH="$PATH" HOME="${HOME:-/root}" ${DOCKER_HOST:+"DOCKER_HOST=$DOCKER_HOST"} \
    docker compose --project-name "$project" --project-directory "$dir" \
    --env-file "$dir/vars.env" "${files[@]}" "$@"
}

# Checks the template in $1 with template.sh, and sets routed to the service its
# x-deadlift.route names, and config to the template as Compose normalises it. Refuses, changing
# nothing, a template deadlift won't run (ADR 0001, ADR 0002).
check_template() {
  local status=0
  config=$("$lib/template.sh" check "$1" "$project") || status=$?
  ((status == 0)) || exit "$status"
  route=$(jq -r '."x-deadlift".route' <<<"$config")
  routed=${route%:*}
}

# Writes deadlift's override for the checked template in $1: the routed service published on the
# host's loopback at port $2, and the App's limits and deadlift's hardening on every service.
write_override() {
  local status=0
  "$lib/template.sh" override "$1" "$project" --port "$2" --app "$app" --environment "$env" \
    --limits "$state/apps/$app/app.conf" >"$1/override.json" || status=$?
  ((status == 0)) || exit "$status"
}

# Pulls every image the template names that the Server doesn't have yet, with the job's registry
# token. Fails, changing nothing, when one can't be pulled.
pull_images() {
  local docker_config=$delivery/docker ref
  mkdir -p "$docker_config"
  # In a config of its own, inside the delivery, so it's removed with it. GHCR checks only the
  # token, so the username is a placeholder.
  if [[ -s $delivery/token ]]; then
    jq -n --arg auth "$(printf 'deadlift:%s' "$(<"$delivery/token")" | base64 -w0)" \
      '{auths: {"ghcr.io": {auth: $auth}}}' >"$docker_config/config.json"
  fi
  while IFS= read -r ref; do
    if docker image inspect "$ref" >/dev/null 2>&1; then continue; fi
    echo "Pulling $ref"
    DOCKER_CONFIG=$docker_config docker pull --quiet "$ref" >/dev/null 2>"$delivery/pull.err" ||
      fail "couldn't pull $ref: $(tail -n 1 "$delivery/pull.err"). Nothing was changed"
  done < <(jq -r '[.services[].image // empty] | unique[]' <<<"$config")
}

# Sets slot to the Slot that $env holds, or the lowest free one. Skips when none is free.
claim_slot() {
  local environments=$state/apps/$app/environments held=() holders=() file s
  if [[ -f $environments/$env/slot ]]; then
    slot=$(<"$environments/$env/slot")
    return
  fi
  for file in "$environments"/feature-*/slot; do
    [[ -f $file ]] || continue
    held+=("$(<"$file")")
    holders+=("$(basename "$(dirname "$file")")")
  done
  for ((s = 1; s <= slots; s++)); do
    if [[ " ${held[*]} " != *" $s "* ]]; then
      slot=$s
      return
    fi
  done
  skip "all $slots of $app's Slots are held, by ${holders[*]}, so $env wasn't deployed"
}

deploy_feature() {
  local next=$delivery/next dir=$state/apps/$app/environments/$env name url log
  local suffix slot port public
  [[ -f $delivery/compose.yaml ]] || refuse "the delivery has no compose.yaml to deploy"
  project=deadlift-$app-$env

  mkdir "$next"
  cp "$delivery/compose.yaml" "$next/compose.yaml"
  for name in "${!images[@]}"; do
    echo "DEADLIFT_IMAGE_${name^^}=${images[$name]}"
  done >"$next/vars.env"
  check_template "$next"
  read_tailnet_suffix
  claim_slot
  port=$((port_base + 10 * slot))
  public=$((10000 + slot))
  url=https://$app.$suffix:$public/
  write_override "$next" "$port"
  pull_images

  mkdir -p "$dir"
  cp "$next/compose.yaml" "$next/vars.env" "$next/override.json" "$dir/"
  echo "$slot" >"$dir/slot"
  log=$delivery/up.log
  echo "Starting $env"
  if ! compose "$dir" up --detach --pull never --remove-orphans 2>&1 | tee "$log" >&2 ||
    ! compose "$dir" up --detach --pull never --no-recreate --no-deps --wait \
      --wait-timeout "$health_timeout" "$routed" 2>&1 | tee "$log" >&2; then
    compose "$dir" stop >/dev/null 2>&1 || true
    fail "$env didn't start: $(tail -n 1 "$log"). It's stopped, and the App's other Environments are as they were"
  fi
  if ! tailscale serve --service="svc:$app" --https="$public" --bg "http://127.0.0.1:$port" >&2; then
    compose "$dir" stop >/dev/null 2>&1 || true
    fail "$env started, but Tailscale Serve couldn't map it at $url, so it's stopped again"
  fi

  output environment "$env"
  output url "$url"
  echo "deadlift: deployed $env, which is up at $url" >&2
}

op_deploy() {
  env=${1:-}
  shift || true
  while (($#)); do
    case $1 in
      --image)
        [[ ${2:-} =~ ^([a-z][a-z0-9_]*)=([a-zA-Z0-9][a-zA-Z0-9._/:@-]*)$ ]] ||
          usage "--image needs <name>=<ref>, with a name of lowercase letters, digits and _"
        [[ -z ${images[${BASH_REMATCH[1]}]:-} ]] || usage "the image ${BASH_REMATCH[1]} is given twice"
        images[${BASH_REMATCH[1]}]=${BASH_REMATCH[2]}
        shift 2
        ;;
      *) usage "unknown argument '$1' for deploy" ;;
    esac
  done
  case $env in
    feature-*)
      [[ ${env#feature-} =~ ^[a-z0-9][a-z0-9-]{0,29}$ ]] ||
        usage "'${env#feature-}' isn't a Feature Environment key: up to 30 of a-z, 0-9 and -"
      ;;
    dev) refuse "deploying the Dev Environment isn't supported by this Server yet" ;;
    *) usage "'$env' isn't an Environment: dev or feature-<key>" ;;
  esac
  read_app
  take_lock
  deploy_feature
}

app='' delivery=''
while (($#)); do
  case $1 in
    --app | --delivery)
      [[ $# -ge 2 ]] || usage "$1 needs a value"
      declare "${1#--}=$2"
      shift 2
      ;;
    *) break ;;
  esac
done
[[ $app =~ ^[a-z0-9][a-z0-9-]*$ ]] || usage "--app needs an App"
[[ -d $delivery ]] || usage "--delivery needs the unpacked delivery"

case ${1:-} in
  deploy) shift && op_deploy "$@" ;;
  *) usage "'${1:-}' isn't an operation" ;;
esac
