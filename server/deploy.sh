#!/usr/bin/env bash
# deadlift's deploy script: runs one operation on one App's Environments. Bootstrap installs it on
# the Server, owned by root, at /usr/local/lib/deadlift/deploy.sh, and it's never delivered
# (ADR 0001): a fix to it reaches a Server when `deadlift server bootstrap` is re-run. Only the
# entry command runs it, as root, after working out the App from the calling user and unpacking
# the delivery.
#
# Usage:
#   deploy.sh --app <app> --delivery <dir> deploy dev|feature-<key> [--image <name>=<ref>]...
#   deploy.sh --app <app> --delivery <dir> destroy feature-<key>
#
#   --app       The App, as the entry command worked it out. Never from a delivery's arguments.
#   --delivery  The unpacked delivery: compose.yaml, the App's Compose template, and token, the
#               job's registry token.
#
# Operations:
#   deploy <environment> --image <name>=<ref>...
#       Deploys the delivered template to the Environment, with each image reaching the template
#       as ${DEADLIFT_IMAGE_<NAME>}. A Feature Environment, feature-<key>, takes a free Slot of
#       the App's, or the one its key already holds, and is skipped when none is free. A
#       redeploy keeps the Environment's Slot, URL and volumes, and a failed one leaves it
#       stopped.
#
#       The Dev Environment, dev, is checked and pulled before anything stops, so a bad template
#       or image reference leaves it running as it was. Then Dev stops, a Volume Snapshot is taken
#       of every named volume, and the new version starts. When it doesn't, Recovery brings Dev
#       down, puts the snapshot back and starts the previous version, and the reason says whether
#       that worked. The new version is recorded as current, and the one before it as previous,
#       for Rollback.
#
#   destroy feature-<key>
#       Removes the Feature Environment, its volumes and its Service mapping, and frees its
#       Slot. Done, changing nothing, when the App has no such Environment.
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
#                                   `deadlift app add`: port_base=<port>, slots=<n>,
#                                   snapshots=<n>, the Volume Snapshots to keep (3 if unset),
#                                   and the per-container limits memory=<size, e.g. 512m>,
#                                   cpus=<n> and pids=<n> (template.sh has the defaults).
#   apps/<app>/dev.env, feature.env The App's secrets files, written by `deadlift app secrets`.
#   apps/<app>/environments/<env>/  An Environment: its template (compose.yaml), the variables
#                                   it was deployed with (secrets.env, a copy of its secrets
#                                   file, and vars.env, deadlift's), its DEADLIFT_SECRET (secret),
#                                   deadlift's override (override.json) and, for Feature, the
#                                   Slot it holds (slot). Destroying it removes the directory.
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
# Where bootstrap installed this script, and template.sh beside it.
lib=$(dirname "${BASH_SOURCE[0]}")
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

# Sets slot to the Slot that $env holds, or the lowest free one. Skips when none is free.
claim_slot() {
  local held holders s
  if [[ -f $state/apps/$app/environments/$env/slot ]]; then
    slot=$(<"$state/apps/$app/environments/$env/slot")
    return
  fi
  held=$(held_slots)
  for ((s = 1; s <= slots; s++)); do
    if ! grep -q "^$s " <<<"$held"; then
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
  output environment "$env"
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
  rm -rf "${dir:?}"
  echo "deadlift: destroyed $env with its volumes${slot:+, freeing Slot $slot}" >&2
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
#
# The tars stream through the helper's stdout and stdin, with no host directory mounted, so it
# runs in the same user namespace as the App's containers on a Server with userns-remap: it reads
# and writes the volumes as their root does, and owners come back as they were.
take_snapshot() {
  local volume volumes partial n=1
  # Listed first: a snapshot missing a volume because Docker didn't answer would make Recovery
  # remove that volume.
  volumes=$(project_volumes) || return 1
  snapshot=$(date -u +%Y%m%dT%H%M%SZ)
  while [[ -e $snapshots/$snapshot ]]; do snapshot=$(date -u +%Y%m%dT%H%M%SZ)-$((++n)); done
  partial=$snapshots/$snapshot.partial
  mkdir -p "$partial"
  while IFS= read -r volume; do
    [[ -n $volume ]] || continue
    if ! docker run --rm --network none --volume "$volume:/volume:ro" "$snapshot_image" \
      tar -cf - -C /volume . >"$partial/$volume.tar"; then
      rm -rf "$partial"
      return 1
    fi
  done <<<"$volumes"
  mv "$partial" "$snapshots/$snapshot"
}

# Puts the Volume Snapshot $1 back into $project's named volumes, whose containers must be
# removed. Each volume it holds gets exactly its contents back, and a volume made since, which it
# doesn't hold, is removed.
restore_snapshot() {
  local from=$snapshots/$1 volume volumes file
  [[ -n $1 && -d $from ]] || return 1
  volumes=$(project_volumes) || return 1
  while IFS= read -r volume; do
    [[ -n $volume && ! -f $from/$volume.tar ]] || continue
    docker volume rm "$volume" >/dev/null || return 1
  done <<<"$volumes"
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
# (compose.yaml), the variables and deadlift's override it runs with (secrets.env, vars.env,
# override.json), its image references (images, <name>=<ref> per line), and the name of the Volume
# Snapshot taken just before it was first deployed (snapshot). DEADLIFT_SECRET (secret) belongs
# to the Environment, not to a version.
version_files=(compose.yaml secrets.env vars.env override.json images snapshot)

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
  for file in "${version_files[@]}" secret; do
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
  for name in "${!images[@]}"; do echo "$name=${images[$name]}"; done | sort >"$next/images"
  read_tailnet_suffix
  url=https://$app.$suffix/
  write_variables "$next" "$url"
  check_template "$next"
  write_override "$next" "$port"
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

# Promotes Dev's previous version, now running, to its current version, and forgets the one
# rolled back from: Rollback goes back one step only.
promote_previous() {
  local file
  for file in "${version_files[@]}"; do
    if [[ -f $previous/$file ]]; then
      cp "$previous/$file" "$dir/$file.new"
      mv "$dir/$file.new" "$dir/$file"
    else
      rm -f "$dir/$file"
    fi
  done
  rm -rf "${previous:?}"
}

# Rolls the Dev Environment back to its previous version, which becomes its current version: one
# step only. Nothing stops until the previous version's images are on the Server. It starts on
# today's data, and is kept when it's healthy. When it isn't, the current version starts again
# and Rollback is refused, unless $discard says to discard newer data: then a Volume Snapshot of
# today's data is taken, and the previous version starts on the snapshot from before the current
# version was first deployed. If that fails too, today's data and the current version go back.
rollback_dev() {
  local dir=$state/apps/$app/environments/dev url why before=''
  local previous=$state/apps/$app/environments/dev/previous
  local snapshots=$state/apps/$app/environments/dev/snapshots snapshot
  project=deadlift-$app-dev
  [[ -f $dir/compose.yaml ]] || refuse "dev hasn't been deployed, so there's nothing to roll back"
  [[ -f $previous/compose.yaml ]] ||
    refuse "dev has no previous version to roll back to: Rollback goes back one step only, and only once Dev has had two versions. To go further back, deploy an earlier commit. Nothing was changed"
  url=$(sed -n 's/^DEADLIFT_URL=//p' "$dir/vars.env")
  config=$(compose "$previous" config --format json 2>"$delivery/config.err") ||
    fail "couldn't read dev's previous version: $(tail -n 1 "$delivery/config.err"). Nothing was changed"
  routed=$(jq -r '."x-deadlift".route | split(":")[0]' <<<"$config")
  pull_images
  if [[ -n $discard ]]; then
    if [[ -f $dir/snapshot ]]; then before=$(<"$dir/snapshot"); fi
    [[ -n $before && -d $snapshots/$before ]] ||
      fail "the Volume Snapshot from before dev's current version was deployed is gone, so --discard-newer-data can't go back to it. Nothing was changed"
    pull_snapshot_image
  fi

  echo "Stopping dev to start its previous version"
  if ! project_containers stop; then
    restart_version "$dir" ||
      fail "couldn't stop dev to start its previous version, and its current version didn't start again, so Dev is stopped"
    fail "couldn't stop dev to start its previous version, so nothing was rolled back. Dev is up on its current version, at $url"
  fi
  echo "Starting dev's previous version on today's data"
  if start_environment "$previous" "$routed"; then
    promote_previous
    output environment dev
    output url "$url"
    echo "deadlift: rolled dev back to its previous version, on today's data, which is up at $url" >&2
    return
  fi
  why=$(tail -n 1 "$delivery/up.log")
  project_containers stop || true

  if [[ -z $discard ]]; then
    echo "Starting dev's current version again"
    if ! restart_version "$dir"; then
      project_containers stop || true
      fail "dev's previous version didn't start on today's data ($why), and its current version didn't start again either ($(tail -n 1 "$delivery/up.log")), so Dev is stopped"
    fi
    refuse "dev's previous version didn't start on today's data: $why. Rollback was refused, and Dev is up on its current version again, with its data as it was, at $url. To roll back to the data from before the current version was deployed, discarding what's been written since, run it again with --discard-newer-data, which keeps a Volume Snapshot of today's data first"
  fi

  echo "Taking a Volume Snapshot of dev's data as it is today"
  if ! take_snapshot; then
    restart_version "$dir" ||
      fail "dev's previous version didn't start on today's data ($why), and a Volume Snapshot of today's data couldn't be taken, so nothing was discarded, and Dev is stopped"
    fail "dev's previous version didn't start on today's data ($why), and a Volume Snapshot of today's data couldn't be taken, so nothing was discarded. Dev is up on its current version, at $url"
  fi
  echo "Putting back the Volume Snapshot from before dev's current version, $before"
  project_containers rm --force || true
  if ! restore_snapshot "$before"; then
    why="the Volume Snapshot from before the current version, $before, couldn't be put back"
  elif ! start_environment "$previous" "$routed"; then
    why="it didn't start on the data from before the current version either: $(tail -n 1 "$delivery/up.log")"
  else
    promote_previous
    prune_snapshots
    output environment dev
    output url "$url"
    echo "deadlift: rolled dev back to its previous version, on its data from before the current version, which is up at $url. Today's data is kept in the Volume Snapshot $snapshot" >&2
    return
  fi
  echo "Putting back today's data and dev's current version"
  project_containers rm --force || true
  if ! restore_snapshot "$snapshot" || ! restart_version "$dir"; then
    project_containers stop || true
    fail "dev's previous version didn't start on today's data, and $why. Putting back today's data and the current version failed too, so Dev is stopped. Today's data is in the Volume Snapshot $snapshot"
  fi
  fail "dev's previous version didn't start on today's data, and $why. Nothing was rolled back: Dev is up on its current version again, with today's data, at $url"
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
    feature-*) check_feature_key ;;
    dev) ;;
    *) usage "'$env' isn't an Environment: dev or feature-<key>" ;;
  esac
  read_app
  take_lock
  if [[ $env == dev ]]; then deploy_dev; else deploy_feature; fi
}

op_rollback() {
  env=${1:-}
  shift || true
  discard=''
  while (($#)); do
    case $1 in
      --discard-newer-data) discard=yes && shift ;;
      *) usage "unknown argument '$1' for rollback" ;;
    esac
  done
  case $env in
    dev) ;;
    feature-*) check_feature_key && refuse "Rollback is only for the Dev Environment, dev, and $env is a Feature Environment: deploy it again from an earlier commit instead. Nothing was changed" ;;
    *) usage "'$env' isn't the Dev Environment: rollback takes dev" ;;
  esac
  read_app
  take_lock
  rollback_dev
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
  destroy_feature "$env"
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
  rollback) shift && op_rollback "$@" ;;
  destroy) shift && op_destroy "$@" ;;
  *) usage "'${1:-}' isn't an operation" ;;
esac
