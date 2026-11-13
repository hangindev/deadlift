#!/usr/bin/env bash
# deadlift's bootstrap: turns a fresh Ubuntu 24.04 machine into a Server. `deadlift server
# bootstrap <server>` sends it over SSH with the rest of server/ and runs it as root, so you rarely
# run it yourself. By hand, as root, from a directory holding server/:
#
#   bootstrap.sh
#
# It's safe to run again, and running it again is how a Server catches up with deadlift: a fix to
# the entry command or the deploy script reaches a Server only this way, never with a delivery
# (ADR 0001). Each step checks first and changes only what's missing or different, so a run with
# nothing to do changes no file and restarts nothing: not Docker, and no Environment.
#
# What it sets up:
#   - Docker Engine with the Compose plugin, from Docker's apt repository, running with
#     `userns-remap`, so root in a container is an unprivileged user on the host. Once installed,
#     Docker is never upgraded here: that would restart every Environment. Turning on
#     `userns-remap` restarts Docker once, and the daemon then keeps its images and volumes apart
#     from the ones it had before.
#   - Tailscale, from its apt repository, unless it's installed. Joining the tailnet is the
#     provision wizard's step, not this one.
#   - The firewall for containers, firewall.sh, at /usr/local/lib/deadlift/firewall.sh, applied
#     now and by deadlift-firewall.service at every boot before Docker starts (see firewall.sh).
#   - The entry command, /usr/local/sbin/deadlift-entry, and the deploy script it runs,
#     /usr/local/lib/deadlift/deploy.sh, with the template check, template.sh, beside it, all
#     owned by root. `deadlift app add` gives each App's deploy user one sudo rule for the entry
#     command. jq, and python3 with PyYAML, which they use.
#   - /srv/deadlift, only for root: the Server's lock, the Server registry (apps/) and the
#     Server-wide settings (settings, below).
#
# The Server-wide settings, in /srv/deadlift/settings, one `name=value` a line. A setting missing
# from the file is added with its default; one already set is never changed, so edit the file to
# change one:
#   feature_deploy_disk_limit  Over this percentage of disk used, Feature deploys are skipped. 80.
#   watchdog_disk_limit        Over this percentage, the watchdog stops every Feature Environment
#                              on the Server. 90.
#
# Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused (nothing
# was changed).
set -euo pipefail
umask 022

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
state=/srv/deadlift
lib=/usr/local/lib/deadlift
entry_command=/usr/local/sbin/deadlift-entry
daemon_json=/etc/docker/daemon.json
firewall_unit=/etc/systemd/system/deadlift-firewall.service
settings_defaults=(feature_deploy_disk_limit=80 watchdog_disk_limit=90)

fail() {
  echo "deadlift: $1" >&2
  exit 1
}

refuse() {
  echo "deadlift: $1. Nothing was changed." >&2
  exit 2
}

step() {
  echo "==> $1"
}

apt_updated=''
apt_install() {
  if [[ -z $apt_updated ]]; then
    apt-get update -qq
    apt_updated=yes
  fi
  DEBIAN_FRONTEND=noninteractive apt-get install -qq --yes --no-install-recommends "$@"
}

# The suite name of this Ubuntu release, e.g. noble.
codename() {
  # shellcheck source=/dev/null
  (. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
}

# Installs <source> at <target> with <mode>, owned by root, unless it's there already just so.
# Written aside and then renamed, so a run cut off half way never leaves half a file, and a
# running script keeps reading the copy it started with. Sets changed when it writes.
install_file() {
  local mode=$1 source=$2 target=$3
  changed=''
  if [[ -f $target && ! -L $target ]] && cmp -s "$source" "$target" &&
    [[ $(stat -c '%a %U %G' "$target") == "${mode#0} root root" ]]; then
    return
  fi
  install -m "$mode" -o root -g root "$source" "$target.new"
  mv -f "$target.new" "$target"
  changed=yes
}

# Installs a file at <target> holding <content>, as install_file does.
install_content() {
  local mode=$1 target=$2 content=$3 source
  source=$(mktemp)
  printf '%s\n' "$content" >"$source"
  install_file "$mode" "$source" "$target"
  rm -f "$source"
}

install_prerequisites() {
  local missing=() tool
  # jq for the deploy script and the Docker config; flock and the rest come with Ubuntu.
  for tool in jq curl; do
    command -v "$tool" >/dev/null || missing+=("$tool")
  done
  # PyYAML, for template.sh to read a template's YAML before Compose does.
  python3 -I -c 'import yaml' 2>/dev/null || missing+=(python3 python3-yaml)
  if ((${#missing[@]})); then apt_install ca-certificates "${missing[@]}"; fi
  echo "jq, curl and python3 with PyYAML are installed"
}

# Turns on userns-remap in Docker's config, keeping what else it holds. Done before Docker is
# installed, so a fresh Server's Docker starts remapped and never needs a restart.
configure_docker() {
  local config='{}'
  install -d -m 0755 /etc/docker
  if [[ -f $daemon_json ]]; then
    config=$(jq . "$daemon_json") || fail "$daemon_json isn't valid JSON, so Docker's config was left alone"
  fi
  if jq -e '."userns-remap" | type == "string" and length > 0' <<<"$config" >/dev/null; then
    echo "Docker's config has userns-remap"
    return
  fi
  install_content 0644 "$daemon_json" "$(jq '. + {"userns-remap": "default"}' <<<"$config")"
  echo "Turned on userns-remap in $daemon_json"
}

install_docker() {
  if docker compose version >/dev/null 2>&1; then
    echo "Docker and Compose are installed"
    return
  fi
  # https://docs.docker.com/engine/install/ubuntu/
  apt_install ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/ubuntu $(codename) stable" \
    >/etc/apt/sources.list.d/docker.list
  apt_updated=''
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_tailscale() {
  if command -v tailscale >/dev/null; then
    echo "Tailscale is installed"
    return
  fi
  # https://tailscale.com/kb/1187/install-ubuntu-2204, for this release's codename.
  apt_install ca-certificates curl
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$(codename).noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/$(codename).tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt_updated=''
  apt_install tailscale
  systemctl enable --now tailscaled
}

# What install_file just did, for the log.
installed_or_kept() {
  if [[ -n $changed ]]; then echo Installed; else echo Kept; fi
}

install_scripts() {
  install -d -m 0755 -o root -g root "$lib"
  install_file 0755 "$here/entry.sh" "$entry_command"
  echo "$(installed_or_kept) the entry command, $entry_command"
  install_file 0755 "$here/deploy.sh" "$lib/deploy.sh"
  echo "$(installed_or_kept) the deploy script, $lib/deploy.sh"
  install_file 0755 "$here/template.sh" "$lib/template.sh"
  echo "$(installed_or_kept) the template check beside it, $lib/template.sh"
  install_file 0755 "$here/firewall.sh" "$lib/firewall.sh"
}

# Starts and enables a unit, unless it's both already.
start_and_enable() {
  systemctl is-enabled --quiet "$1" || systemctl enable --quiet "$1"
  systemctl is-active --quiet "$1" || systemctl start "$1"
}

setup_firewall() {
  install_content 0644 "$firewall_unit" "[Unit]
Description=deadlift's firewall for containers, before Docker starts any (ADR 0001)
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$lib/firewall.sh

[Install]
WantedBy=multi-user.target
RequiredBy=docker.service"
  # Only after a change, so running bootstrap again leaves systemd alone.
  if [[ -n $changed ]]; then systemctl daemon-reload; fi
  # Applied now as well as by the unit, which runs it only when it starts, so a changed firewall
  # takes effect without restarting anything.
  "$lib/firewall.sh"
  start_and_enable deadlift-firewall.service
  echo "The firewall for containers is in place"
}

# Whether the running Docker daemon remaps users.
docker_remaps() {
  docker info --format '{{json .SecurityOptions}}' 2>/dev/null | grep -q 'name=userns'
}

start_docker() {
  start_and_enable docker.service
  if docker_remaps; then
    echo "Docker is running with userns-remap"
    return
  fi
  echo "Restarting Docker to turn on userns-remap. Its containers stop, and it keeps images and" \
    "volumes made before apart from the remapped ones."
  systemctl restart docker.service
  local tries
  for ((tries = 0; tries < 60; tries++)); do
    if docker_remaps; then
      echo "Docker is running with userns-remap"
      return
    fi
    sleep 1
  done
  fail "Docker didn't come back with userns-remap after restarting. See \`journalctl -u docker\`"
}

setup_state() {
  install -d -m 0700 -o root -g root "$state" "$state/apps"
  local file=$state/settings setting
  if [[ ! -f $file ]]; then
    install_content 0600 "$file" "# deadlift's Server-wide settings. See \`deadlift server bootstrap\`'s
# bootstrap.sh for what each means. Bootstrap adds a missing one and never changes one set here."
  fi
  for setting in "${settings_defaults[@]}"; do
    if ! grep -q "^${setting%%=*}=" "$file"; then
      echo "$setting" >>"$file"
      echo "Set $setting in $file"
    fi
  done
}

[[ $EUID -eq 0 ]] || refuse "bootstrap needs root"
for file in entry.sh deploy.sh template.sh firewall.sh; do
  [[ -r $here/$file ]] || refuse "$file isn't beside bootstrap.sh. Run it through \`deadlift server bootstrap\`, which sends all of server/"
done
# shellcheck source=/dev/null
[[ $(. /etc/os-release && echo "$ID") == ubuntu ]] || refuse "a Server runs Ubuntu 24.04, and this isn't Ubuntu"

# Bootstrap restarts Docker the first time, so it waits for any operation on the Server first.
install -d -m 0700 -o root -g root "$state"
exec 9>>"$state/lock"
if ! flock --nonblock 9; then
  echo "Waiting for another operation on this Server to finish"
  flock 9
fi

step "Installing what deadlift needs"
install_prerequisites
step "Configuring Docker"
configure_docker
step "Installing Docker"
install_docker
step "Installing Tailscale"
install_tailscale
step "Installing the entry command and the deploy script"
install_scripts
step "Installing the firewall for containers"
setup_firewall
step "Starting Docker"
start_docker
step "Setting up $state and the Server-wide settings"
setup_state

echo "deadlift: this machine is a Server, bootstrapped and up to date."
