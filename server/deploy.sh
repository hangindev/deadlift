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
# (ADR 0002). deadlift publishes it on the host's loopback at the Environment's port, starts the
# Environment with `docker compose up`, and waits for the routed service to be healthy. It waits
# on that service only, because `up --wait` over a whole project fails when a one-shot service
# that nothing depends on exits 0. A migration the routed service depends on still has to
# succeed first.
#
# Ports: each App has a block of 100 loopback ports from its `port_base`. The Dev Environment is
# at the base, and the Feature Environment holding Slot s at base + 10s. Tailscale Serve maps
# them on the App's own Service, svc:<app> (ADR 0003): Dev at HTTPS 443, and Slot s at
# 10000 + s, so a Feature Environment's URL stays the same while it holds its Slot.
#
# State, all root-only, under /srv/deadlift:
#   lock                            The Server's lock: one operation at a time.
#   apps/<app>/app.conf             The App's entry in the Server registry, written by
#                                   `deadlift app add`: port_base=<port> and slots=<n>.
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
# How long the routed service has to report healthy.
health_timeout=300

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

# Runs `docker compose` for the Environment whose files are in $1, as the project $project. The
# template sees only the variables in vars.env: nothing from this script's own environment, or
# its caller's, reaches it.
compose() {
  local dir=$1
  shift
  env -i PATH="$PATH" HOME="${HOME:-/root}" ${DOCKER_HOST:+"DOCKER_HOST=$DOCKER_HOST"} \
    docker compose --project-name "$project" --project-directory "$dir" \
    --env-file "$dir/vars.env" --file "$dir/compose.yaml" \
    ${override:+--file "$dir/override.json"} "$@"
}

# Checks the template in $1 and sets routed and route_port from its x-deadlift.route, and config
# to the template as Compose normalises it. Refuses, changing nothing, a template deadlift won't
# run.
check_template() {
  local dir=$1 health
  override=''
  config=$(compose "$dir" config --format json 2>"$dir/config.err") ||
    refuse "the template isn't valid Compose: $(tail -n 1 "$dir/config.err"). Nothing was changed"
  route=$(jq -r '."x-deadlift".route // empty' <<<"$config")
  [[ -n $route ]] ||
    refuse "the template has no x-deadlift.route. Name the service deadlift routes to as \`x-deadlift.route: <service>:<port>\`. Nothing was changed"
  [[ $route =~ ^([a-zA-Z0-9][a-zA-Z0-9_.-]*):([0-9]{1,5})$ ]] ||
    refuse "x-deadlift.route is '$route', and it must be <service>:<port>. Nothing was changed"
  routed=${BASH_REMATCH[1]} route_port=${BASH_REMATCH[2]}
  health=$(jq -r --arg service "$routed" '
    .services[$service] // "missing" | if . == "missing" then . else
      .healthcheck | if . == null then "none"
      elif .disable == true or (.test | type == "array" and .[0] == "NONE") then "disabled"
      else "ok" end
    end' <<<"$config")
  case $health in
    missing) refuse "x-deadlift.route names the service $routed, and the template has none. Nothing was changed" ;;
    none) refuse "the routed service $routed has no healthcheck in the template, and deadlift judges an Environment by it (ADR 0002). Nothing was changed" ;;
    disabled) refuse "the routed service $routed has its healthcheck disabled, and deadlift judges an Environment by it (ADR 0002). Nothing was changed" ;;
  esac
}

# Pulls every image the template names that the Server doesn't have yet, with the job's registry
# token. Fails, changing nothing, when one can't be pulled.
pull_images() {
  local docker_config=$delivery/docker ref
  mkdir -p "$docker_config"
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
  jq -n --arg service "$routed" --arg port "$port" --argjson target "$route_port" \
    '{services: {($service): {ports: [{host_ip: "127.0.0.1", published: $port, target: $target}]}}}' \
    >"$next/override.json"
  pull_images

  mkdir -p "$dir"
  cp "$next/compose.yaml" "$next/vars.env" "$next/override.json" "$dir/"
  echo "$slot" >"$dir/slot"
  override=yes
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
  images=()
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
declare -A images

case ${1:-} in
  deploy) shift && op_deploy "$@" ;;
  *) usage "'${1:-}' isn't an operation" ;;
esac
