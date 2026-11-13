#!/usr/bin/env bash
# deadlift's App administration on a Server: the Server half of `deadlift app add`, `app secrets`,
# `app remove` and `server status`. Bootstrap installs it, owned by root, at
# /usr/local/lib/deadlift/app.sh, and the maintainer's `deadlift` runs it over SSH as root. No
# App's CI can run it: a deploy user's one sudo rule is for the entry command only.
#
# Usage:
#   app.sh add <app> [--slots <n>] [--snapshots <n>] [--memory <size>] [--cpus <n>] [--pids <n>]
#   app.sh secrets <app> dev|feature
#   app.sh remove <app>
#   app.sh status
#
# add      Gives the App its place on the Server, or brings it up to date, changing only what's
#          missing or different, so running it again with the same settings changes nothing:
#            - its deploy user, deadlift-<app>, outside the docker group;
#            - one sudo rule letting that user run the entry command, and nothing else
#              (ADR 0001), checked with visudo;
#            - its entry in the Server registry, apps/<app>/app.conf: a block of 100 loopback
#              ports overlapping no other App's, kept once given, and its settings. A setting
#              not given keeps the value it has, or gets its default on a first add: 3 Slots
#              (1 to 9), 3 Volume Snapshots kept, and per container 1g of memory, 1 CPU and 512
#              processes.
#          Prints `deadlift-output: slots=<n>`, for the App's Service to list every Slot's port.
# secrets  Puts the App's secrets file for its Dev or its Feature Environments, read from stdin,
#          on the Server as apps/<app>/dev.env or feature.env, only for root. Its variables reach
#          the template from each deploy of that kind on.
# remove   Removes everything of the App from the Server: its sudo rule, its Service's Serve
#          mappings, every Environment with its containers, volumes and networks, its Volume
#          Snapshots, secrets files and registry entry, and its deploy user. Done, changing
#          nothing, when there's nothing of it left.
# status   Shows the Server's disk usage, and each App's ports, Slots in use, Volume Snapshots
#          and Environments.
#
# add, secrets and remove take the Server's lock, so they never run alongside an operation.
#
# Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused or bad usage
# (nothing was changed).
set -euo pipefail
umask 077

state=/srv/deadlift
entry_command=/usr/local/sbin/deadlift-entry
# Where the Apps' port blocks go: 20000-20099, 20100-20199 and so on.
first_port_base=20000
last_port_base=29900
max_secrets_bytes=$((1024 * 1024))

usage() {
  echo "deadlift: $1" >&2
  sed -n 's/^#   \(app\.sh .*\)/Usage: \1/p' "$0" >&2
  exit 2
}

refuse() {
  echo "deadlift: $1. Nothing was changed." >&2
  exit 2
}

fail() {
  echo "deadlift: $1" >&2
  exit 1
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

# Refuses an App name deadlift can't use in a user name, a Tailscale Service name, a tag and the
# Compose projects deadlift-<app>-<environment>. A name with a `-feature` part could make one
# App's project the same as another's (deadlift-x-feature-y-dev is App x's feature-y-dev and App
# x-feature-y's dev), so it's refused too.
check_app() {
  [[ $1 =~ ^[a-z]([a-z0-9-]{0,21}[a-z0-9])?$ ]] ||
    usage "'$1' isn't an App name: up to 23 of a-z, 0-9 and -, starting with a letter and not ending with -"
  [[ ! $1 =~ -feature(-|$) ]] ||
    usage "'$1' isn't an App name: a part named feature after a - could clash with another App's Feature Environments"
}

take_lock() {
  install -d -m 0700 "$state"
  exec 9>>"$state/lock"
  if ! flock --nonblock 9; then
    echo "Waiting for another operation on this Server to finish"
    flock 9
  fi
}

# Writes <content> to <file> with <mode>, owned by root, unless it holds exactly that already.
# Written aside and renamed, so it's never half there. Sets changed when it writes.
write_file() {
  local mode=$1 file=$2 content=$3
  changed=''
  if [[ -f $file && ! -L $file ]] && [[ $(<"$file") == "$content" ]] &&
    [[ $(stat -c '%a %U %G' "$file") == "${mode#0} root root" ]]; then
    return
  fi
  printf '%s\n' "$content" >"$file.new"
  chown root:root "$file.new"
  chmod "$mode" "$file.new"
  mv -f "$file.new" "$file"
  changed=yes
}

# Prints a setting from an app.conf, or nothing.
setting() {
  sed -n "s/^$1=//p" "$2" | tail -n 1
}

# The lowest port block no other App has.
free_port_base() {
  local base conf taken=' '
  for conf in "$state"/apps/*/app.conf; do
    [[ -f $conf ]] && taken+="$(setting port_base "$conf") "
  done
  for ((base = first_port_base; base <= last_port_base; base += 100)); do
    if [[ $taken != *" $base "* ]]; then
      echo "$base"
      return
    fi
  done
  return 1
}

# The highest Slot one of the App's Feature Environments holds, or 0.
highest_held_slot() {
  local file highest=0
  for file in "$state/apps/$app/environments"/feature-*/slot; do
    [[ -f $file ]] || continue
    if (($(<"$file") > highest)); then highest=$(<"$file"); fi
  done
  echo "$highest"
}

op_add() {
  local slots='' snapshots='' memory='' cpus='' pids='' dir conf port_base user rule what=()
  app=${1:-}
  shift || true
  check_app "$app"
  while (($#)); do
    case $1 in
      --slots | --snapshots | --memory | --cpus | --pids)
        [[ $# -ge 2 ]] || usage "$1 needs a value"
        declare "${1#--}=$2"
        shift 2
        ;;
      *) usage "unknown argument '$1' for add" ;;
    esac
  done
  [[ -z $slots || $slots =~ ^[1-9]$ ]] || usage "--slots is a number of Slots from 1 to 9"
  [[ -z $snapshots || $snapshots =~ ^[1-9][0-9]{0,2}$ ]] ||
    usage "--snapshots is a number of Volume Snapshots to keep, at least 1"
  [[ -z $memory || $memory =~ ^[1-9][0-9]*[kmg]?$ ]] ||
    usage "--memory is a size such as 512m or 2g"
  [[ -z $cpus || ($cpus =~ ^[0-9]+(\.[0-9]+)?$ && ! $cpus =~ ^0+(\.0+)?$) ]] ||
    usage "--cpus is a number of CPUs above 0, such as 0.5 or 2"
  [[ -z $pids || $pids =~ ^[1-9][0-9]*$ ]] || usage "--pids is a number of processes, at least 1"
  [[ -x $entry_command ]] ||
    refuse "this Server has no entry command. Run \`deadlift server bootstrap\` first"

  take_lock
  dir=$state/apps/$app
  conf=$dir/app.conf
  install -d -m 0700 -o root -g root "$state/apps"
  if [[ -f $conf ]]; then
    port_base=$(setting port_base "$conf")
    : "${slots:=$(setting slots "$conf")}" "${snapshots:=$(setting snapshots "$conf")}"
    : "${memory:=$(setting memory "$conf")}" "${cpus:=$(setting cpus "$conf")}"
    : "${pids:=$(setting pids "$conf")}"
  else
    port_base=$(free_port_base) ||
      refuse "every block of ports from $first_port_base to $((last_port_base + 99)) is another App's"
  fi
  : "${slots:=3}" "${snapshots:=3}"
  if (($(highest_held_slot) > slots)); then
    refuse "$app's Feature Environments hold Slots up to $(highest_held_slot), more than $slots. Destroy them first, or keep more Slots"
  fi

  # The deploy user, outside the docker group: only the entry command acts on Docker for it.
  user=deadlift-$app
  if ! id "$user" >/dev/null 2>&1; then
    useradd --create-home --user-group --shell /bin/bash --comment "deadlift deploy user of $app" "$user"
    what+=("created the deploy user $user")
  fi
  if id -nG "$user" | tr ' ' '\n' | grep -qx docker; then
    gpasswd --delete "$user" docker >/dev/null
    what+=("took $user out of the docker group")
  fi

  # The one sudo rule. sudo ignores files in sudoers.d with a dot in their name, so the new one
  # isn't live until it's checked and renamed.
  rule=/etc/sudoers.d/$user
  if [[ $(cat "$rule" 2>/dev/null) != "$user ALL=(root) NOPASSWD: $entry_command" ||
    $(stat -c '%a %U %G' "$rule" 2>/dev/null) != "440 root root" ]]; then
    printf '%s\n' "$user ALL=(root) NOPASSWD: $entry_command" >"$rule.new"
    chmod 0440 "$rule.new"
    if ! visudo --check --quiet --file "$rule.new" >/dev/null; then
      rm -f "$rule.new"
      fail "the sudo rule for $user didn't pass visudo, so it wasn't installed"
    fi
    mv -f "$rule.new" "$rule"
    what+=("installed $user's sudo rule for the entry command")
  fi
  visudo --check --quiet >/dev/null || fail "visudo finds the Server's sudo rules broken after adding $user's. See \`visudo -c\`"

  # The App's entry in the Server registry.
  install -d -m 0700 -o root -g root "$dir"
  write_file 0600 "$conf" "$(
    echo "port_base=$port_base"
    echo "slots=$slots"
    echo "snapshots=$snapshots"
    if [[ -n $memory ]]; then echo "memory=$memory"; fi
    if [[ -n $cpus ]]; then echo "cpus=$cpus"; fi
    if [[ -n $pids ]]; then echo "pids=$pids"; fi
  )"
  if [[ -n $changed ]]; then what+=("set its settings in the Server registry"); fi

  echo "deadlift-output: slots=$slots"
  local summary="ports $port_base-$((port_base + 99)), $slots Slots, $snapshots Volume Snapshots kept"
  if ((${#what[@]})); then
    echo "deadlift: $app is on this Server, with $summary. This run $(sentence "${what[@]}")." >&2
  else
    echo "deadlift: $app is on this Server, with $summary. Nothing needed changing." >&2
  fi
}

op_secrets() {
  local kind=${2:-} dir file size
  app=${1:-}
  check_app "$app"
  [[ $kind == dev || $kind == feature ]] || usage "secrets is for dev or feature, not '$kind'"
  (($# == 2)) || usage "secrets takes <app> dev|feature, and the file on stdin"
  dir=$state/apps/$app
  file=$dir/$kind.env
  take_lock
  [[ -f $dir/app.conf ]] || refuse "$app isn't an App on this Server. Run \`deadlift app add\` first"
  head -c $((max_secrets_bytes + 1)) >"$file.new"
  size=$(stat -c %s "$file.new")
  if ((size > max_secrets_bytes)); then
    rm -f "$file.new"
    refuse "the secrets file is over $max_secrets_bytes bytes"
  fi
  if [[ -f $file ]] && cmp -s "$file" "$file.new"; then
    rm -f "$file.new"
    echo "deadlift: $app's $kind.env on this Server already holds that. Nothing was changed." >&2
    return
  fi
  chown root:root "$file.new"
  chmod 0600 "$file.new"
  mv -f "$file.new" "$file"
  echo "deadlift: put $app's $kind.env on this Server. Its variables reach the template from the next deploy of a ${kind/dev/Dev} Environment on." >&2
}

# Removes every container, volume and network of the Compose project $1, by Compose's label.
remove_project() {
  local by_project=(--filter "label=com.docker.compose.project=$1") ids
  mapfile -t ids < <(docker container ls --all --quiet "${by_project[@]}")
  if ((${#ids[@]})); then docker container rm --force --volumes "${ids[@]}" >/dev/null || return; fi
  mapfile -t ids < <(docker volume ls --quiet "${by_project[@]}")
  if ((${#ids[@]})); then docker volume rm "${ids[@]}" >/dev/null || return; fi
  mapfile -t ids < <(docker network ls --quiet "${by_project[@]}")
  if ((${#ids[@]})); then docker network rm "${ids[@]}" >/dev/null || return; fi
}

op_remove() {
  local user rule dir projects=() project env what=() ids
  app=${1:-}
  check_app "$app"
  (($# == 1)) || usage "remove takes only <app>"
  user=deadlift-$app
  rule=/etc/sudoers.d/$user
  dir=$state/apps/$app
  take_lock
  docker info >/dev/null 2>&1 || fail "Docker isn't answering on this Server, so nothing of $app was removed"

  # First the way in, so the App's CI can't start anything while the rest goes.
  if [[ -e $rule ]]; then
    rm -f "$rule"
    what+=("its sudo rule")
  fi

  # Then its addresses. Tailscale Serve keeps them per Service, so only this App's go.
  local serve
  serve=$(tailscale serve status --json) ||
    fail "couldn't read Tailscale Serve's mappings, so $app's Environments were left as they were. Its sudo rule is gone, so its CI can't reach them; run \`deadlift app remove\` again"
  if [[ -n $serve ]] && jq -e --arg service "svc:$app" '.Services[$service] // empty' <<<"$serve" >/dev/null; then
    tailscale serve clear "svc:$app" >&2 ||
      fail "couldn't remove $app's Service from Tailscale Serve, so its Environments were left as they were. Run \`deadlift app remove\` again"
    what+=("its Service's mappings")
  fi

  # Every Environment's Compose project: the ones deadlift has state for, and any Docker still has
  # by deadlift's label, so a half-finished deploy or destroy is cleaned up too.
  if [[ -d $dir/environments ]]; then
    for env in "$dir"/environments/*/; do
      [[ -d $env ]] && projects+=("deadlift-$app-$(basename "$env")")
    done
  fi
  mapfile -t -O "${#projects[@]}" projects < <(
    {
      docker container ls --all --filter "label=deadlift.app=$app" --format '{{.Label "com.docker.compose.project"}}'
      docker volume ls --filter "label=deadlift.app=$app" --format '{{.Label "com.docker.compose.project"}}'
    } | grep . || true
  )
  while IFS= read -r project; do
    [[ -n $project ]] || continue
    remove_project "$project" ||
      fail "couldn't remove all of $app's containers, volumes and networks (in $project). Run \`deadlift app remove\` again"
  done < <(printf '%s\n' "${projects[@]}" | sort -u)
  # A volume carrying the App's label outside any project, should one ever exist.
  mapfile -t ids < <(docker volume ls --quiet --filter "label=deadlift.app=$app")
  if ((${#ids[@]})); then
    docker volume rm "${ids[@]}" >/dev/null || fail "couldn't remove $app's volumes. Run \`deadlift app remove\` again"
  fi
  if ((${#projects[@]})); then what+=("its Environments with their volumes"); fi

  # Its Volume Snapshots, secrets files and registry entry.
  if [[ -d $dir ]]; then
    rm -rf "${dir:?}"
    what+=("its registry entry with its secrets files and Volume Snapshots")
  fi

  # Last, its deploy user, with anything still running as it, such as a closing SSH session.
  if id "$user" >/dev/null 2>&1; then
    pkill --signal KILL --uid "$user" 2>/dev/null || true
    userdel --force --remove "$user" 2>/dev/null || fail "couldn't remove the deploy user $user. Run \`deadlift app remove\` again"
    what+=("its deploy user $user")
  fi

  if ((${#what[@]})); then
    echo "deadlift: removed $app from this Server: $(sentence "${what[@]}")." >&2
  else
    echo "deadlift: $app isn't on this Server, so there was nothing to remove." >&2
  fi
}

# The percentage of disk used where the Server keeps its state and Docker its data, the fuller.
disk_used() {
  local dirs=("$state") root used highest=0
  root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) && dirs+=("$root")
  for used in $(df -P "${dirs[@]}" | awk 'NR > 1 { sub("%", "", $5); print $5 }'); do
    if ((used > highest)); then highest=$used; fi
  done
  echo "$highest"
}

op_status() {
  (($# == 0)) || usage "status takes no arguments"
  local suffix settings conf app_dir env_dir env slot slots held snaps url running total line
  suffix=$(tailscale status --json 2>/dev/null | jq -r '.MagicDNSSuffix // empty' 2>/dev/null) || suffix=''
  settings=$state/settings
  echo "Disk: $(disk_used)% used. Feature deploys are skipped over $(setting feature_deploy_disk_limit "$settings" 2>/dev/null || true)%, and the watchdog stops Feature Environments over $(setting watchdog_disk_limit "$settings" 2>/dev/null || true)%."
  local apps=0
  for app_dir in "$state"/apps/*/; do
    conf=$app_dir/app.conf
    [[ -f $conf ]] || continue
    app=$(basename "$app_dir")
    apps=$((apps + 1))
    slots=$(setting slots "$conf")
    held=$( (find "$app_dir/environments" -mindepth 2 -maxdepth 2 -path '*/feature-*/slot' 2>/dev/null || true) | wc -l)
    snaps=$( (find "$app_dir/environments/dev/snapshots" -mindepth 1 -maxdepth 1 -type d ! -name '*.partial' 2>/dev/null || true) | wc -l)
    echo
    echo "$app: ports $(setting port_base "$conf")-$(($(setting port_base "$conf") + 99)), Slots in use: $held of $slots, Volume Snapshots: $snaps"
    line=0
    for env_dir in "$app_dir"/environments/*/; do
      [[ -d $env_dir ]] || continue
      env=$(basename "$env_dir")
      slot=''
      if [[ $env == dev ]]; then
        url=${suffix:+https://$app.$suffix/}
      elif [[ -f $env_dir/slot ]]; then
        slot=$(<"$env_dir/slot")
        url=${suffix:+https://$app.$suffix:$((10000 + slot))/}
      else
        url=''
      fi
      total=$(docker container ls --all --quiet --filter "label=com.docker.compose.project=deadlift-$app-$env" | wc -l)
      running=$(docker container ls --quiet --filter "label=com.docker.compose.project=deadlift-$app-$env" | wc -l)
      printf '  %-36s %-22s %-8s %s\n' "$env" "$running of $total running" "${slot:+Slot $slot}" "$url"
      line=1
    done
    if ((line == 0)); then echo "  no Environments"; fi
  done
  if ((apps == 0)); then
    echo
    echo "No Apps. Add one with \`deadlift app add\`."
  fi
}

[[ $EUID -eq 0 ]] || refuse "app.sh needs root"
app=''
case ${1:-} in
  add) shift && op_add "$@" ;;
  secrets) shift && op_secrets "$@" ;;
  remove) shift && op_remove "$@" ;;
  status) shift && op_status "$@" ;;
  *) usage "'${1:-}' isn't a command: add, secrets, remove or status" ;;
esac
