#!/usr/bin/env bash
# deadlift's deploy script: runs one operation on one App's Environments. Bootstrap installs it on
# the Server, owned by root, at /usr/local/lib/deadlift/deploy.sh, and it's never delivered
# (ADR 0001): a fix to it reaches a Server when `deadlift server bootstrap` is re-run. Only the
# entry command runs it, as root, after working out the App from the calling user and unpacking
# the delivery.
#
# Usage:
#   deploy.sh --app <app> --delivery <dir> deploy dev|feature-<key> [--image <name>=<ref>]...
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
#       The Dev Environment, dev, is checked and pulled before anything stops, so a bad template
#       or image reference leaves it running as it was. Then Dev stops, a Volume Snapshot is taken
#       of every named volume, and the new version starts. When it doesn't, Recovery brings Dev
#       down, puts the snapshot back and starts the previous version, and the reason says whether
#       that worked. The new version is recorded as current, and the one before it as previous,
#       for Rollback.
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
#                                   `deadlift app add`: port_base=<port>, slots=<n> and
#                                   snapshots=<n>, the Volume Snapshots to keep (3 if unset).
#   apps/<app>/environments/<env>/  An Environment: its template (compose.yaml), the variables
#                                   it was deployed with (vars.env), deadlift's override
#                                   (override.json) and, for Feature, the Slot it holds (slot).
#     dev/                          For Dev, these are its current version, which also has its
#                                   image references (images: <name>=<ref> per line) and the
#                                   name of the Volume Snapshot taken just before it was first
#                                   deployed (snapshot), which --discard-newer-data goes back to.
#     dev/previous/                 The version before the current one, in the same files, which
#                                   Rollback starts. Absent until Dev has had two versions.
#     dev/snapshots/<name>/         A Volume Snapshot: <volume>.tar for each of Dev's named
#                                   volumes, taken with its containers stopped. The newest are
#                                   kept, and the one the current version records.
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
# Takes Volume Snapshots and puts them back: only its tar, sh and find are used.
snapshot_image=alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
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

# Reads the App's entry in the Server registry into port_base, slots and snapshots_kept.
read_app() {
  local conf=$state/apps/$app/app.conf key value
  port_base='' slots='' snapshots_kept=3
  [[ -f $conf ]] || refuse "$app isn't an App on this Server"
  while IFS='=' read -r key value; do
    case $key in
      port_base) port_base=$value ;;
      slots) slots=$value ;;
      snapshots) snapshots_kept=$value ;;
    esac
  done <"$conf"
  [[ $port_base =~ ^[0-9]+$ && $slots =~ ^[1-9]$ && $snapshots_kept =~ ^[1-9][0-9]*$ ]] ||
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

# Checks the template in $1 and sets routed and route_port from its x-deadlift.route, and config
# to the template as Compose normalises it. Refuses, changing nothing, a template deadlift won't
# run.
check_template() {
  local dir=$1 health
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
  jq -n --arg service "$routed" --arg port "$port" --argjson target "$route_port" \
    '{services: {($service): {ports: [{host_ip: "127.0.0.1", published: $port, target: $target}]}}}' \
    >"$next/override.json"
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

# Starts the Environment whose files are in $1, which routes to the service $2, and waits for
# that service to be healthy. Returns non-zero when it doesn't start, with the reason as the last
# line of $delivery/up.log.
start_environment() {
  local dir=$1 routed=$2 log=$delivery/up.log
  compose "$dir" up --detach --pull never --remove-orphans 2>&1 | tee "$log" >&2 &&
    compose "$dir" up --detach --pull never --no-recreate --no-deps --wait \
      --wait-timeout "$health_timeout" "$routed" 2>&1 | tee "$log" >&2
}

# Starts the version of an Environment whose files are in $1 again, routing to the service its
# own template names. Returns non-zero when it doesn't start.
restart_version() {
  local routed_there
  [[ -f $1/compose.yaml ]] || return 1
  routed_there=$(compose "$1" config --format json 2>/dev/null |
    jq -er '."x-deadlift".route | split(":")[0]') || return 1
  start_environment "$1" "$routed_there"
}

# Stops, or with `rm --force` removes, every container of $project, whatever template started it.
project_containers() {
  local action=$1 ids
  shift
  mapfile -t ids < <(docker container ls --all --quiet --filter "label=com.docker.compose.project=$project")
  if ((${#ids[@]})); then docker container "$action" "$@" "${ids[@]}" >/dev/null; fi
}

# The named volumes of $project, by their full names.
project_volumes() {
  docker volume ls --quiet --filter "label=com.docker.compose.project=$project"
}

# Pulls the image Volume Snapshots are taken and put back with, if the Server doesn't have it.
pull_snapshot_image() {
  if docker image inspect "$snapshot_image" >/dev/null 2>&1; then return; fi
  echo "Pulling $snapshot_image"
  docker pull --quiet "$snapshot_image" >/dev/null 2>"$delivery/pull.err" ||
    fail "couldn't pull $snapshot_image, which takes Volume Snapshots: $(tail -n 1 "$delivery/pull.err"). Nothing was changed"
}

# Takes a Volume Snapshot of every named volume of $project, whose containers must be stopped:
# each volume's contents as <volume>.tar, in a directory of its own under $snapshots. Sets
# snapshot to its name. One that couldn't be finished is removed, so it never counts.
take_snapshot() {
  local volume partial n=1
  snapshot=$(date -u +%Y%m%dT%H%M%SZ)
  while [[ -e $snapshots/$snapshot ]]; do snapshot=$(date -u +%Y%m%dT%H%M%SZ)-$((++n)); done
  partial=$snapshots/$snapshot.partial
  mkdir -p "$partial"
  while IFS= read -r volume; do
    if ! docker run --rm --network none --volume "$volume:/volume:ro" "$snapshot_image" \
      tar -cf - -C /volume . >"$partial/$volume.tar"; then
      rm -rf "$partial"
      return 1
    fi
  done < <(project_volumes)
  mv "$partial" "$snapshots/$snapshot"
}

# Puts the Volume Snapshot $1 back into $project's named volumes, whose containers must be
# removed. Each volume it holds gets exactly its contents back, and a volume made since, which it
# doesn't hold, is removed.
restore_snapshot() {
  local from=$snapshots/$1 volume file
  [[ -n $1 && -d $from ]] || return 1
  while IFS= read -r volume; do
    if [[ ! -f $from/$volume.tar ]]; then docker volume rm "$volume" >/dev/null || return 1; fi
  done < <(project_volumes)
  for file in "$from"/*.tar; do
    [[ -f $file ]] || continue
    volume=$(basename "$file" .tar)
    docker run --rm --interactive --network none --volume "$volume:/volume" "$snapshot_image" \
      sh -c 'find /volume -mindepth 1 -delete && tar -xf - -C /volume' <"$file" >/dev/null || return 1
  done
}

# Keeps the App's configured number of Volume Snapshots, the newest, and also the one Dev's
# current version was first deployed over, which Rollback's --discard-newer-data goes back to.
prune_snapshots() {
  local needed='' ids id
  if [[ -f $dir/snapshot ]]; then needed=$(<"$dir/snapshot"); fi
  mapfile -t ids < <(find "$snapshots" -mindepth 1 -maxdepth 1 -type d ! -name '*.partial' -printf '%f\n' | sort -r)
  for id in "${ids[@]:snapshots_kept}"; do
    if [[ $id != "$needed" ]]; then rm -rf "${snapshots:?}/$id"; fi
  done
}

# The files that make up a version of the Dev Environment, in its directory: the template
# (compose.yaml), the variables and deadlift's override it runs with (vars.env, override.json),
# its image references (images, <name>=<ref> per line), and the name of the Volume Snapshot taken
# just before it was first deployed (snapshot).
version_files=(compose.yaml vars.env override.json images snapshot)

# Records the version in $next, now running, as Dev's current version. The current version
# before it becomes the previous one, which Rollback goes back to. Deploying the current version
# again, the same template and image references, leaves the previous one as it was.
record_version() {
  local file
  if [[ -f $dir/compose.yaml ]] && cmp -s "$dir/compose.yaml" "$next/compose.yaml" &&
    cmp -s "$dir/images" "$next/images"; then
    cp "$dir/snapshot" "$next/snapshot"
  else
    echo "$snapshot" >"$next/snapshot"
    if [[ -f $dir/compose.yaml ]]; then
      rm -rf "$dir/previous.new"
      mkdir "$dir/previous.new"
      for file in "${version_files[@]}"; do
        if [[ -f $dir/$file ]]; then cp "$dir/$file" "$dir/previous.new/"; fi
      done
      rm -rf "$dir/previous"
      mv "$dir/previous.new" "$dir/previous"
    fi
  fi
  for file in "${version_files[@]}"; do
    cp "$next/$file" "$dir/$file.new"
    mv "$dir/$file.new" "$dir/$file"
  done
}

# Recovery, after the new version in $next didn't start, for the reason $1: brings Dev down, puts
# back the Volume Snapshot taken before this deploy, and starts the current version again. Always
# fails, saying whether Recovery succeeded and what's left running.
recover_dev() {
  local why="dev didn't start on its new version: $1"
  echo "Recovery: putting back dev's Volume Snapshot and its previous version"
  project_containers rm --force || true
  if ! restore_snapshot "$snapshot"; then
    fail "$why. Recovery failed: it couldn't put back the Volume Snapshot taken before this deploy, $snapshot, so Dev is stopped"
  fi
  if [[ ! -f $dir/compose.yaml ]]; then
    fail "$why. Recovery put back Dev's data as it was before this deploy, and there was no previous version to start, so Dev is stopped"
  fi
  if ! restart_version "$dir"; then
    project_containers stop || true
    fail "$why. Recovery failed: it put back Dev's data as it was before this deploy, but the previous version didn't start either ($(tail -n 1 "$delivery/up.log" 2>/dev/null)), so Dev is stopped"
  fi
  fail "$why. Recovery succeeded: Dev is back on its previous version, with its data as it was before this deploy, at $url"
}

# Deploys the delivered template to the Dev Environment, at the base of the App's ports and the
# root of its Service. Nothing stops until the template is checked and every image is pulled.
# Then Dev stops, a Volume Snapshot is taken, and the new version starts. If it doesn't, Recovery
# puts back the snapshot and the previous version.
deploy_dev() {
  local dir=$state/apps/$app/environments/dev next=$delivery/next name url suffix port
  local snapshots=$state/apps/$app/environments/dev/snapshots snapshot
  [[ -f $delivery/compose.yaml ]] || refuse "the delivery has no compose.yaml to deploy"
  project=deadlift-$app-dev
  port=$port_base

  mkdir "$next"
  cp "$delivery/compose.yaml" "$next/compose.yaml"
  for name in "${!images[@]}"; do
    echo "DEADLIFT_IMAGE_${name^^}=${images[$name]}"
  done >"$next/vars.env"
  for name in "${!images[@]}"; do echo "$name=${images[$name]}"; done | sort >"$next/images"
  check_template "$next"
  read_tailnet_suffix
  url=https://$app.$suffix/
  jq -n --arg service "$routed" --arg port "$port" --argjson target "$route_port" \
    '{services: {($service): {ports: [{host_ip: "127.0.0.1", published: $port, target: $target}]}}}' \
    >"$next/override.json"
  pull_images
  pull_snapshot_image

  echo "Stopping dev for a Volume Snapshot"
  mkdir -p "$dir" "$snapshots"
  if ! project_containers stop; then
    restart_version "$dir" ||
      fail "couldn't stop dev for a Volume Snapshot, and its current version didn't start again, so Dev is stopped. Its new version wasn't started"
    fail "couldn't stop dev for a Volume Snapshot, so its new version wasn't started. Dev is up on its current version, at $url"
  fi
  if ! take_snapshot; then
    restart_version "$dir" ||
      fail "couldn't take a Volume Snapshot of dev, so its new version wasn't started, and Dev is stopped"
    fail "couldn't take a Volume Snapshot of dev, so its new version wasn't started. Dev is up on its current version, at $url"
  fi

  echo "Starting dev"
  if ! start_environment "$next" "$routed"; then
    recover_dev "$(tail -n 1 "$delivery/up.log")"
  fi
  record_version
  prune_snapshots
  if ! tailscale serve --service="svc:$app" --https=443 --bg "http://127.0.0.1:$port" >&2; then
    fail "dev is up on its new version, but Tailscale Serve couldn't map it at $url"
  fi

  output environment dev
  output url "$url"
  echo "deadlift: deployed dev, which is up at $url" >&2
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
    dev) ;;
    *) usage "'$env' isn't an Environment: dev or feature-<key>" ;;
  esac
  read_app
  take_lock
  if [[ $env == dev ]]; then deploy_dev; else deploy_feature; fi
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
