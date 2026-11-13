#!/usr/bin/env bash
# deadlift's deploy script: runs one operation on one App's Environments. Bootstrap installs it on
# the Server, owned by root, at /usr/local/lib/deadlift/deploy.sh, and it's never delivered
# (ADR 0001): a fix to it reaches a Server when `deadlift server bootstrap` is re-run. Only the
# entry command runs it, as root, after working out the App from the calling user and unpacking
# the delivery.
#
# Usage:
#   deploy.sh --app <app> --delivery <dir> deploy feature-<key> [--image <name>=<ref>]... [--keep <keys> | --keep-none]
#   deploy.sh --app <app> --delivery <dir> destroy feature-<key>
#   deploy.sh --app <app> --delivery <dir> sweep --keep <keys> | --keep-none
#
#   --app       The App, as the entry command worked it out. Never from a delivery's arguments.
#   --delivery  The unpacked delivery: compose.yaml, the App's Compose template, and token, the
#               job's registry token.
#
# Operations:
#   deploy <environment> --image <name>=<ref>... [--keep <keys> | --keep-none]
#       Deploys the delivered template to the Environment, with each image reaching the template
#       as ${DEADLIFT_IMAGE_<NAME>}. A Feature Environment, feature-<key>, takes a free Slot of
#       the App's, or the one its key already holds, and is skipped when none is free. A
#       redeploy keeps the Environment's Slot, URL and volumes, and a failed one leaves it
#       stopped. With --keep or --keep-none, a Feature deploy first sweeps the App as `sweep`
#       does, always keeping the Environment it deploys, so Slots held by keys no longer kept
#       are free for it. It sweeps only once the template is checked and the images pulled, and
#       not at all when it's skipped.
#
#   destroy feature-<key>
#       Removes the Feature Environment, its volumes and its Service mapping, and frees its
#       Slot. Done, changing nothing, when the App has no such Environment.
#
#   sweep --keep <keys> | --keep-none
#       The Sweep: destroys every Feature Environment of the App whose key isn't in <keys>,
#       separated by commas, or every one with --keep-none, and outputs the swept keys. An empty
#       <keys> is refused, so a list that failed to come never sweeps everything. It goes on past
#       an Environment it can't destroy, then fails naming it. The Dev Environment, and other
#       Apps' Environments, are never swept.
#
# A Feature Environment's key is up to 30 of a-z, 0-9 and -, starting with a letter or digit.
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
# Variables reach the template through Compose's ${VAR} substitution, and nothing else: the
# App's secrets file for the kind of Environment (dev.env for Dev, feature.env for Feature),
# and deadlift's own, which win over the secrets file's:
#   DEADLIFT_ENV             The Environment: dev or feature-<key>.
#   DEADLIFT_APP             The App.
#   DEADLIFT_URL             The Environment's URL.
#   DEADLIFT_SECRET          Random, made on the Environment's first deploy and kept until it's
#                            destroyed.
#   DEADLIFT_IMAGE_<NAME>    Each --image.
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
#   apps/<app>/dev.env, feature.env The App's secrets files, written by `deadlift app secrets`.
#   apps/<app>/environments/<env>/  An Environment: its template (compose.yaml), the variables
#                                   it was deployed with (secrets.env, a copy of its secrets
#                                   file, and vars.env, deadlift's), its DEADLIFT_SECRET (secret),
#                                   deadlift's override (override.json) and, for Feature, the
#                                   Slot it holds (slot). Destroying it removes the directory.
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
# --keep or --keep-none: whether either was given, and the Feature Environments to keep.
keep_given='' keep=()
# What a Sweep destroyed, and what it couldn't.
swept=() unswept=()

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

# Refuses $env unless its key is up to 30 of a-z, 0-9 and -, so it's safe in paths, Compose
# project names and URLs.
check_feature_key() {
  [[ ${env#feature-} =~ ^[a-z0-9][a-z0-9-]{0,29}$ ]] ||
    usage "'${env#feature-}' isn't a Feature Environment key: up to 30 of a-z, 0-9 and -"
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
# secrets.env and vars.env, vars.env winning: nothing from this script's own environment, or its
# caller's, reaches it.
compose() {
  local dir=$1 files
  shift
  files=(--file "$dir/compose.yaml")
  if [[ -f $dir/override.json ]]; then files+=(--file "$dir/override.json"); fi
  if [[ -f $dir/secrets.env ]]; then files=(--env-file "$dir/secrets.env" "${files[@]}"); fi
  env -i PATH="$PATH" HOME="${HOME:-/root}" ${DOCKER_HOST:+"DOCKER_HOST=$DOCKER_HOST"} \
    docker compose --project-name "$project" --project-directory "$dir" \
    "${files[@]}" --env-file "$dir/vars.env" "$@"
}

# Writes the variables the template in $1 will see, for the Environment $env of $app at the URL
# $2: secrets.env, a copy of the App's secrets file for that kind of Environment (dev.env or
# feature.env), and vars.env, deadlift's own variables, which win over the secrets file's. Also
# writes secret, the Environment's DEADLIFT_SECRET: the one it already has, or a new random one on
# its first deploy, so it's kept until the Environment is destroyed.
write_variables() {
  local dir=$1 url=$2 secrets=$state/apps/$app/${env%%-*}.env name error
  local secret=$state/apps/$app/environments/$env/secret
  if [[ -f $secrets ]]; then cp "$secrets" "$dir/secrets.env"; else : >"$dir/secrets.env"; fi
  # Read on its own first, so a broken secrets file isn't blamed on the template.
  if ! error=$(echo 'services: {}' | env -i PATH="$PATH" HOME="${HOME:-/root}" \
    ${DOCKER_HOST:+"DOCKER_HOST=$DOCKER_HOST"} docker compose --project-name "$project" \
    --project-directory "$dir" --env-file "$dir/secrets.env" --file - config --quiet 2>&1); then
    fail "$app's ${secrets##*/} on this Server isn't a valid env file: ${error##*secrets.env: }. Fix it and re-run \`deadlift app secrets $app ${env%%-*}\`. Nothing was changed"
  fi
  if [[ -s $secret ]]; then
    cp "$secret" "$dir/secret"
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n' >"$dir/secret"
  fi
  {
    echo "DEADLIFT_ENV=$env"
    echo "DEADLIFT_APP=$app"
    echo "DEADLIFT_URL=$url"
    echo "DEADLIFT_SECRET=$(<"$dir/secret")"
    for name in "${!images[@]}"; do
      echo "DEADLIFT_IMAGE_${name^^}=${images[$name]}"
    done
  } >"$dir/vars.env"
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

# Joins its arguments as in a sentence: "a", "a and b", "a, b and c".
sentence() {
  local joined=$1
  shift
  while (($# > 1)); do
    joined+=", $1"
    shift
  done
  echo "$joined${1:+ and $1}"
}

# Prints which Slot each of the App's Feature Environments holds, as `<slot> <environment>`
# lines in Slot order.
held_slots() {
  local file
  for file in "$state/apps/$app/environments"/feature-*/slot; do
    [[ -f $file ]] || continue
    echo "$(<"$file") $(basename "$(dirname "$file")")"
  done | sort -n
}

# Sets slot to the Slot that $env holds, or the lowest free one. With --keep or --keep-none, a
# Slot held by a Feature Environment that isn't kept counts as free, since the Sweep before the
# deploy frees it. Skips when none is free, having changed nothing.
claim_slot() {
  local held holders s holder
  if [[ -f $state/apps/$app/environments/$env/slot ]]; then
    slot=$(<"$state/apps/$app/environments/$env/slot")
    return
  fi
  held=$(held_slots)
  for ((s = 1; s <= slots; s++)); do
    holder=$(sed -n "s/^$s //p" <<<"$held")
    if [[ -z $holder ]] || { [[ -n $keep_given ]] && ! is_kept "$holder"; }; then
      slot=$s
      return
    fi
  done
  mapfile -t holders < <(cut -d ' ' -f 2 <<<"$held")
  skip "all $slots of $app's Slots are held, by $(sentence "${holders[@]}"), so $env wasn't deployed. Destroy one of them to free its Slot"
}

deploy_feature() {
  local next=$delivery/next dir=$state/apps/$app/environments/$env url log
  local suffix slot port public
  [[ -f $delivery/compose.yaml ]] || refuse "the delivery has no compose.yaml to deploy"
  project=deadlift-$app-$env

  mkdir "$next"
  cp "$delivery/compose.yaml" "$next/compose.yaml"
  # The Slot comes first, because the template sees the URL it gives.
  read_tailnet_suffix
  claim_slot
  port=$((port_base + 10 * slot))
  public=$((10000 + slot))
  url=https://$app.$suffix:$public/
  write_variables "$next" "$url"
  check_template "$next"
  write_override "$next" "$port"
  pull_images
  # Only now, so a template refused or an image that can't be pulled sweeps nothing.
  if [[ -n $keep_given ]]; then sweep_before_deploy; fi

  mkdir -p "$dir"
  cp "$next/compose.yaml" "$next/secrets.env" "$next/vars.env" "$next/secret" "$next/override.json" \
    "$dir/"
  echo "$slot" >"$dir/slot"
  log=$delivery/up.log
  echo "Starting $env"
  if ! compose "$dir" up --detach --pull never --remove-orphans 2>&1 | tee "$log" >&2 ||
    ! compose "$dir" up --detach --pull never --no-recreate --no-deps --wait \
      --wait-timeout "$health_timeout" "$routed" 2>&1 | tee "$log" >&2; then
    compose "$dir" stop >/dev/null 2>&1 || true
    fail "$env didn't start: $(tail -n 1 "$log"). It's stopped, and the App's other Environments are as they were${swept[*]:+ after sweeping $(sentence "${swept[@]}")}"
  fi
  if ! tailscale serve --service="svc:$app" --https="$public" --bg "http://127.0.0.1:$port" >&2; then
    compose "$dir" stop >/dev/null 2>&1 || true
    fail "$env started, but Tailscale Serve couldn't map it at $url, so it's stopped again"
  fi

  output environment "$env"
  output url "$url"
  echo "deadlift: deployed $env, which is up at $url${swept[*]:+, and swept $(sentence "${swept[@]}")}" >&2
}

# For a Feature deploy with --keep or --keep-none: sweeps the App, $env always kept. Fails if a
# destroy that failed left the Slot claim_slot picked held.
sweep_before_deploy() {
  local holder
  sweep_features
  ((${#unswept[@]})) || return 0
  holder=$(held_slots | sed -n "s/^$slot //p")
  if [[ -n $holder && $holder != "$env" ]]; then
    fail "couldn't destroy $holder (above) to free Slot $slot, so $env wasn't deployed${swept[*]:+. Swept $(sentence "${swept[@]}")}"
  fi
  echo "deadlift: couldn't destroy $(sentence "${unswept[@]}") (above), which the next Sweep tries again" >&2
}

# Removes the Slot's public port from the App's Service, if it's mapped. A Feature Environment
# whose first deploy failed was never mapped.
unmap_slot() {
  local public=$((10000 + $1))
  tailscale serve --service="svc:$app" --https="$public" off >&2 2>/dev/null && return
  # `off` fails when the port isn't mapped, so it fails here only if the port still is.
  ! tailscale serve status --json | jq -e --arg service "svc:$app" --arg port "$public" \
    '.Services[$service].TCP[$port] // empty' >/dev/null
}

# Removes every container, volume and network of the Compose project $1, by Compose's own
# label, so it needs neither the template nor its variables.
remove_project() {
  local by_project=(--filter "label=com.docker.compose.project=$1") ids
  mapfile -t ids < <(docker container ls --all --quiet "${by_project[@]}")
  if ((${#ids[@]})); then docker container rm --force --volumes "${ids[@]}" >/dev/null || return; fi
  mapfile -t ids < <(docker volume ls --quiet "${by_project[@]}")
  if ((${#ids[@]})); then docker volume rm "${ids[@]}" >/dev/null || return; fi
  mapfile -t ids < <(docker network ls --quiet "${by_project[@]}")
  if ((${#ids[@]})); then docker network rm "${ids[@]}" >/dev/null || return; fi
}

# Destroys the App's Feature Environment $1: its Service mapping, its containers, volumes and
# networks, and then its state, which frees its Slot. A destroy that stops partway leaves the
# Slot held, and running it again finishes the job.
destroy_feature() {
  local env=$1 dir=$state/apps/$app/environments/$1 slot=''
  if [[ ! -d $dir ]]; then
    echo "deadlift: $env isn't on this Server, so there was nothing to destroy" >&2
    return
  fi
  if [[ -f $dir/slot ]]; then slot=$(<"$dir/slot"); fi
  echo "Destroying $env"
  if [[ -n $slot ]] && ! unmap_slot "$slot"; then
    fail "couldn't remove $env's address from Tailscale Serve, so $env was left as it was"
  fi
  remove_project "deadlift-$app-$env" ||
    fail "couldn't remove all of $env's containers, volumes and networks. It's no longer mapped, and it keeps its Slot until destroy is run again"
  rm -rf "${dir:?}" ||
    fail "removed $env's containers, volumes and networks, but not its state, so it keeps its Slot until destroy is run again"
  echo "deadlift: destroyed $env with its volumes${slot:+, freeing Slot $slot}" >&2
}

# Prints the App's Feature Environments, one per line. One whose destroy stopped partway is
# still listed, because its state goes last.
feature_environments() {
  local dir
  for dir in "$state/apps/$app/environments"/feature-*/; do
    if [[ -d $dir ]]; then basename "$dir"; fi
  done
}

# Sets keep to the Feature Environments named by $1, keys separated by commas. Refuses a key that
# isn't one, and a list with no key in it: an empty list, as from a failed API call, would sweep
# everything, and only --keep-none may.
read_keep() {
  local key
  local -a keys
  [[ -z $keep_given ]] || usage "give either --keep or --keep-none, and only once"
  keep_given=yes
  IFS=, read -ra keys <<<"$1"
  for key in "${keys[@]}"; do
    [[ -n $key ]] || continue
    [[ $key =~ ^[a-z0-9][a-z0-9-]{0,29}$ ]] ||
      usage "'$key' in --keep isn't a Feature Environment key: up to 30 of a-z, 0-9 and -"
    keep+=("feature-$key")
  done
  ((${#keep[@]})) ||
    refuse "--keep lists no key, so nothing was swept or deployed. Give --keep-none to sweep every Feature Environment"
}

# For --keep-none: keep no Feature Environment.
read_keep_none() {
  [[ -z $keep_given ]] || usage "give either --keep or --keep-none, and only once"
  keep_given=yes
}

# Whether the Feature Environment $1 is kept.
is_kept() {
  local kept
  for kept in "${keep[@]}"; do
    if [[ $kept == "$1" ]]; then return 0; fi
  done
  return 1
}

# Sweeps the App: destroys every Feature Environment of it that isn't kept, going on past one
# that fails. Sets swept to the ones destroyed and unswept to the ones that weren't, and outputs
# the swept keys for `deliver`.
sweep_features() {
  local each keys
  swept=() unswept=()
  while IFS= read -r each; do
    if is_kept "$each"; then continue; fi
    # In a subshell, because a destroy exits when it fails.
    if (destroy_feature "$each"); then swept+=("$each"); else unswept+=("$each"); fi
  done < <(feature_environments)
  keys=("${swept[@]#feature-}")
  output swept "$(IFS=, && echo "${keys[*]}")"
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
      --keep)
        [[ $# -ge 2 ]] || usage "--keep needs the keys of the Feature Environments to keep"
        read_keep "$2"
        shift 2
        ;;
      --keep-none) read_keep_none && shift ;;
      *) usage "unknown argument '$1' for deploy" ;;
    esac
  done
  case $env in
    feature-*)
      check_feature_key
      # The Environment being deployed is always kept.
      keep+=("$env")
      ;;
    dev)
      [[ -z $keep_given ]] || usage "--keep and --keep-none are for a Feature deploy, and dev is the Dev Environment"
      refuse "deploying the Dev Environment isn't supported by this Server yet"
      ;;
    *) usage "'$env' isn't an Environment: dev or feature-<key>" ;;
  esac
  read_app
  take_lock
  deploy_feature
}

op_destroy() {
  env=${1:-}
  (($# <= 1)) || usage "unknown argument '$2' for destroy"
  case $env in
    feature-*) check_feature_key ;;
    dev) refuse "only Feature Environments can be destroyed, and dev is the Dev Environment. Nothing was changed" ;;
    *) usage "'$env' isn't a Feature Environment: feature-<key>" ;;
  esac
  read_app
  take_lock
  output environment "$env"
  destroy_feature "$env"
}

op_sweep() {
  local remaining
  while (($#)); do
    case $1 in
      --keep)
        [[ $# -ge 2 ]] || usage "--keep needs the keys of the Feature Environments to keep"
        read_keep "$2"
        shift 2
        ;;
      --keep-none) read_keep_none && shift ;;
      *) usage "unknown argument '$1' for sweep" ;;
    esac
  done
  [[ -n $keep_given ]] ||
    refuse "sweep needs --keep with the keys of the Feature Environments to keep, or --keep-none to sweep them all. Nothing was swept"
  read_app
  take_lock
  sweep_features
  mapfile -t remaining < <(feature_environments)
  if ((${#unswept[@]})); then
    fail "couldn't destroy $(sentence "${unswept[@]}") (above)${swept[*]:+, after sweeping $(sentence "${swept[@]}")}. Sweep again to finish"
  fi
  if ((${#swept[@]})); then
    echo "deadlift: swept $(sentence "${swept[@]}")${remaining[*]:+, keeping $(sentence "${remaining[@]}")}" >&2
  elif ((${#remaining[@]})); then
    echo "deadlift: swept nothing: $app's Feature Environments, $(sentence "${remaining[@]}"), are all kept" >&2
  else
    echo "deadlift: swept nothing: $app has no Feature Environments" >&2
  fi
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
  destroy) shift && op_destroy "$@" ;;
  sweep) shift && op_sweep "$@" ;;
  *) usage "'${1:-}' isn't an operation" ;;
esac
