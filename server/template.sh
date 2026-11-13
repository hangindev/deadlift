#!/usr/bin/env bash
# deadlift's template check: what an App's Compose template may hold (ADR 0001), and the override
# deadlift adds to it. Bootstrap installs it on the Server beside the deploy script, owned by root,
# at /usr/local/lib/deadlift/template.sh, and only the deploy script runs it. It never starts,
# stops or changes anything: it reads a template and prints JSON.
#
# Usage:
#   template.sh check <dir> <project>
#   template.sh override <dir> <project> --port <port> --app <app> --environment <env> --limits <app.conf>
#
#   <dir>      Holds compose.yaml, the App's template, and the variables it's deployed with:
#              vars.env, and secrets.env when the App has a secrets file. vars.env wins.
#   <project>  The Environment's Compose project, deadlift-<app>-<environment>.
#
# check     Prints the template as `docker compose config` normalises it, when deadlift will
#           run it. The App's routed service is its `x-deadlift.route: <service>:<port>`, and
#           has a Compose healthcheck that isn't disabled (ADR 0002).
# override  Checks the template the same way, then prints deadlift's override for it, the
#           Compose file merged over the template to run it:
#             - the routed service published on 127.0.0.1:<port>, and nothing else published;
#             - every service limited to the App's memory, CPUs and processes, from the
#               `memory`, `cpus` and `pids` settings in its app.conf, or to the template's own
#               limit where that's lower;
#             - no-new-privileges and capped json-file logs on every service;
#             - the labels deadlift.app and deadlift.environment on every service and volume.
#
# How a template is checked:
#   1. Before Compose reads it, the YAML itself must not use include, extends, env_file,
#      label_file, or configs or secrets read from a file. Compose reads those files from the
#      Server as root, some of them while normalising, and they could be another App's secrets
#      files. (A bind mount can only be told once Compose has interpolated the template, and
#      normalising doesn't read its source, so step 3 refuses it.)
#   2. `docker compose config` normalises it, with nothing of this script's environment, or its
#      caller's, visible: only secrets.env and vars.env.
#   3. Every key of the normalised model must be on the allowlist below. Keys not on it are
#      refused, so a key Compose adds later is refused until it's judged.
#
# Every reason line starts with `deadlift: `.
#
# Exit codes: 0 done, 1 failed, 2 refused (the template, or bad usage).
set -euo pipefail
umask 077

# What an App gets when its app.conf doesn't say, per container.
default_memory=1g
default_cpus=1
default_pids=512

refuse() {
  echo "deadlift: $1. Nothing was changed" >&2
  exit 2
}

fail() {
  echo "deadlift: $1. Nothing was changed" >&2
  exit 1
}

usage() {
  echo "deadlift: $1" >&2
  sed -n 's/^#   \(template\.sh .*\)/Usage: \1/p' "$0" >&2
  exit 2
}

# Prints the first place in the template's YAML that would make Compose read a file on the
# Server, as a path such as `include` or `services.web.env_file`, or nothing. Anchors and merge
# keys are resolved, as Compose resolves them, and every document of the file counts, as Compose
# merges them. A file that isn't YAML exits 3.
file_reading_key() {
  python3 -I - "$1" <<'PYTHON'
import sys

import yaml


class Loader(yaml.SafeLoader):
    pass


def any_local_tag(loader, _suffix, node):
    # Compose's own tags, such as !reset and !override, read as the value they tag.
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


Loader.add_multi_constructor("!", any_local_tag)

try:
    with open(sys.argv[1], encoding="utf-8") as file:
        documents = list(yaml.load_all(file, Loader=Loader))
except (yaml.YAMLError, UnicodeDecodeError) as error:
    print(str(error).replace("\n", " "), file=sys.stderr)
    sys.exit(3)

for document in documents:
    if not isinstance(document, dict):
        continue
    if "include" in document:
        print("include")
        sys.exit(0)
    for kind in ("configs", "secrets"):
        resources = document.get(kind)
        if not isinstance(resources, dict):
            continue
        for name, resource in resources.items():
            if isinstance(resource, dict) and "file" in resource:
                print(f"{kind}.{name}.file")
                sys.exit(0)
    services = document.get("services")
    if not isinstance(services, dict):
        continue
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        for key in ("extends", "env_file", "label_file"):
            if key in service:
                print(f"services.{name}.{key}")
                sys.exit(0)
PYTHON
}

# The allowlist, over the normalised model. Prints one line for the first key it refuses: the
# key's path, then why.
allowlist=$(
  cat <<'JQ'
  def why:
    {
      privileged: "it gives the container the whole Server",
      cap_add: "added capabilities reach past the container",
      devices: "it reaches the Server's devices",
      device_cgroup_rules: "it reaches the Server's devices",
      gpus: "it reaches the Server's devices",
      build: "deadlift runs images only, built and pushed by CI",
      ports: "only deadlift publishes, and only the routed service, on the Server's loopback",
      use_api_socket: "it hands the container the Docker socket",
      container_name: "a container name is shared by every App on the Server",
      external_links: "it reaches containers outside the App's project",
      userns_mode: "it changes the user namespace",
      uts: "it shares the Server's UTS namespace",
      cgroup_parent: "it moves the container out of deadlift's limits",
      runtime: "it changes how the container is run",
      sysctls: "it changes kernel settings",
      oom_score_adj: "it changes how the Server picks what to stop when memory runs out",
      oom_kill_disable: "it changes how the Server picks what to stop when memory runs out",
      deploy: "deadlift sets the limits, from the App's settings"
    }[.] // "it isn't on deadlift's allowlist";

  # Keys allowed as they are.
  def plain_service_keys: [
    "attach", "cap_drop", "command", "configs", "cpus", "depends_on", "dns", "dns_opt",
    "dns_search", "domainname", "entrypoint", "environment", "expose", "extra_hosts",
    "group_add", "healthcheck", "hostname", "image", "init", "links", "mem_limit", "pids_limit",
    "platform", "profiles", "read_only", "restart", "scale", "secrets", "shm_size", "stdin_open",
    "stop_grace_period", "stop_signal", "tmpfs", "tty", "user", "working_dir"
  ];

  def namespace_ok($allowed):
    . as $value | ($allowed | any(. == $value)) or startswith("service:");

  def service_problems($services):
    .key as $service | .value | to_entries[] | .key as $key | .value as $value
    | "services.\($service).\($key)" as $path
    | if (plain_service_keys | any(. == $key)) then empty
      elif $key == "privileged" then
        if $value == true then "\($path) is true: \($key | why)" else empty end
      elif $key == "network_mode" then
        if ($value | namespace_ok(["none"])) then empty
        elif ($value | startswith("container:")) then
          "\($path) is \($value): another container's network (container:) reaches outside the App's project"
        else "\($path) is \($value): only the project's own networks are allowed, never the host's network or a shared one" end
      elif $key == "pid" or $key == "ipc" or $key == "cgroup" then
        if ($value | namespace_ok(["private", "shareable", "none"])) then empty
        elif ($value | startswith("container:")) then
          "\($path) is \($value): another container's namespace (container:) reaches outside the App's project"
        else "\($path) is \($value): it shares the Server's \($key | ascii_upcase) namespace" end
      elif $key == "volumes" then
        $value[]
        | if .type == "bind" and ((.source // "") | test("docker\\.sock")) then
            "\($path) mounts the Docker socket, \(.source), which reaches every App on the Server"
          elif .type == "bind" then
            "\($path) bind-mounts \(.source) from the Server: only named volumes and tmpfs are allowed"
          elif .type == "volume" or .type == "tmpfs" then
            (keys - ["type", "source", "target", "read_only", "volume", "tmpfs", "consistency"])
            | if length > 0 then "\($path) sets \(.[0]) on a mount: \(.[0] | why)" else empty end
          else "\($path) mounts a \(.type): only named volumes and tmpfs are allowed" end
      elif $key == "volumes_from" then
        $value[]
        | if startswith("container:") then
            "\($path) names \(.): another container (container:) reaches outside the App's project"
          elif ($services | has(sub(":(ro|rw)$"; ""))) then empty
          else "\($path) names \(.), which isn't a service of the template" end
      elif $key == "networks" then
        $value | to_entries[]
        | (.value // {} | keys - ["aliases", "priority", "gw_priority"])
        | if length > 0 then "\($path) sets \(.[0]) on a network: \(.[0] | why)" else empty end
      elif $key == "labels" then
        $value | keys[]
        | if startswith("deadlift.") or startswith("com.docker.") then
            "\($path) sets \(.), and deadlift.* and com.docker.* labels are deadlift's and Docker's"
          else empty end
      elif $key == "security_opt" then
        $value[]
        | if test("^no-new-privileges([:=]true)?$") then empty
          else "\($path) has \(.): only no-new-privileges is allowed" end
      elif $key == "pull_policy" then
        if $value == "build" then "\($path) is build: \("build" | why)" else empty end
      else "\($path) is set: \($key | why)" end;

  # A top-level volume, network, config or secret: its own keys, and its name, which must be the
  # one Compose gives it in this project so it can never be another project's.
  def resource_problems($kind; $allowed; $project):
    .key as $name
    | if .value.external == true then
        "\($kind).\($name).external is true: an external \($kind | rtrimstr("s")) is outside the App's project"
      else .value | to_entries[] | .key as $key | .value as $value
    | "\($kind).\($name).\($key)" as $path
    | if $key == "name" then
        if $kind == "configs" or $kind == "secrets" or $value == "\($project)_\($name)" then empty
        else "\($path) is \($value): a \($kind | rtrimstr("s")) named outside the project could be another App's" end
      elif $key == "external" then empty
      elif $key == "driver_opts" then
        "\($path) is set: driver options can mount the Server's files"
      elif $key == "file" then
        "\($path) reads \($value) from the Server: only inline content is allowed"
      elif $key == "driver" then
        if $value == "local" or ($kind == "networks" and $value == "bridge") then empty
        else "\($path) is \($value): only the default driver is allowed" end
      elif $key == "ipam" then
        if ($value | keys - ["config", "driver"] | length) == 0 and ($value.config // [] | length) == 0
          and ($value.driver // "default") == "default" then empty
        else "\($path) is set: the project's addresses are Docker's to choose" end
      elif ($allowed | any(. == $key)) then empty
      else "\($path) is set: \($key | why)" end
      end;

  . as $config
  | [
      (keys[] | . as $key
        | select((["name", "services", "networks", "volumes", "configs", "secrets"] | any(. == $key) | not) and (startswith("x-") | not))
        | "\(.) is set: \(. | why)"),
      ($config.services // {} | to_entries[] | service_problems($config.services)),
      ($config.volumes // {} | to_entries[] | resource_problems("volumes"; ["labels"]; $config.name)),
      ($config.networks // {} | to_entries[] | resource_problems("networks"; ["labels", "internal"]; $config.name)),
      ($config.configs // {} | to_entries[] | resource_problems("configs"; ["content", "environment", "labels"]; $config.name)),
      ($config.secrets // {} | to_entries[] | resource_problems("secrets"; ["content", "environment", "labels"]; $config.name))
    ]
  | first // empty
JQ
)

# Prints the checked template as `docker compose config` normalises it, or refuses it.
check() {
  local dir=$1 project=$2 key problem config route routed health env_files=()
  [[ -f $dir/compose.yaml && -f $dir/vars.env ]] || usage "$dir needs compose.yaml and vars.env"
  if [[ -f $dir/secrets.env ]]; then env_files+=(--env-file "$dir/secrets.env"); fi
  env_files+=(--env-file "$dir/vars.env")

  if ! key=$(file_reading_key "$dir/compose.yaml" 2>"$dir/yaml.err"); then
    refuse "the template isn't valid YAML: $(tail -n 1 "$dir/yaml.err")"
  fi
  [[ -z $key ]] ||
    refuse "the template uses $key, which would read a file on the Server, and ADR 0001 refuses it"

  config=$(env -i PATH="$PATH" HOME="${HOME:-/root}" ${DOCKER_HOST:+"DOCKER_HOST=$DOCKER_HOST"} \
    docker compose --project-name "$project" --project-directory "$dir" \
    "${env_files[@]}" --file "$dir/compose.yaml" \
    config --format json --no-env-resolution 2>"$dir/config.err") ||
    refuse "the template isn't valid Compose: $(tail -n 1 "$dir/config.err")"

  problem=$(jq -r "$allowlist" <<<"$config")
  [[ -z $problem ]] || refuse "the template's $problem, and ADR 0001 refuses it"

  route=$(jq -r '."x-deadlift".route // empty' <<<"$config")
  [[ -n $route ]] ||
    refuse "the template has no x-deadlift.route. Name the service deadlift routes to as \`x-deadlift.route: <service>:<port>\`"
  [[ $route =~ ^([a-zA-Z0-9][a-zA-Z0-9_.-]*):([0-9]{1,5})$ ]] ||
    refuse "x-deadlift.route is '$route', and it must be <service>:<port>"
  routed=${BASH_REMATCH[1]}
  health=$(jq -r --arg service "$routed" '
    .services[$service] // "missing" | if . == "missing" then . else
      .healthcheck | if . == null then "none"
      elif .disable == true or (.test | type == "array" and .[0] == "NONE") then "disabled"
      else "ok" end
    end' <<<"$config")
  case $health in
    missing) refuse "x-deadlift.route names the service $routed, and the template has none" ;;
    none) refuse "the routed service $routed has no healthcheck in the template, and deadlift judges an Environment by it (ADR 0002)" ;;
    disabled) refuse "the routed service $routed has its healthcheck disabled, and deadlift judges an Environment by it (ADR 0002)" ;;
  esac
  printf '%s\n' "$config"
}

# Prints a size such as 512m or 1g in bytes.
bytes() {
  [[ $1 =~ ^([0-9]+)([kmg]?)$ ]] || return 1
  case ${BASH_REMATCH[2]} in
    k) echo $((BASH_REMATCH[1] * 1024)) ;;
    m) echo $((BASH_REMATCH[1] * 1024 * 1024)) ;;
    g) echo $((BASH_REMATCH[1] * 1024 * 1024 * 1024)) ;;
    *) echo "${BASH_REMATCH[1]}" ;;
  esac
}

# Prints deadlift's override for the checked template.
override() {
  local dir=$1 project=$2 port='' app='' environment='' limits='' config key value
  local memory=$default_memory cpus=$default_cpus pids=$default_pids
  shift 2
  while (($#)); do
    case $1 in
      --port | --app | --environment | --limits)
        [[ $# -ge 2 ]] || usage "$1 needs a value"
        declare "${1#--}=$2"
        shift 2
        ;;
      *) usage "unknown argument '$1' for override" ;;
    esac
  done
  [[ $port =~ ^[0-9]+$ && -n $app && -n $environment && -f $limits ]] ||
    usage "override needs --port, --app, --environment and --limits"

  while IFS='=' read -r key value; do
    case $key in
      memory) memory=$value ;;
      cpus) cpus=$value ;;
      pids) pids=$value ;;
    esac
  done <"$limits"
  memory=$(bytes "$memory") && [[ $cpus =~ ^[0-9]+(\.[0-9]+)?$ && $pids =~ ^[1-9][0-9]*$ ]] ||
    fail "$app's limits in the Server registry are broken. Re-run \`deadlift app add $app\`"

  config=$(check "$dir" "$project")
  # Each limit is the template's own where that's set and lower, else the App's.
  jq --arg app "$app" --arg environment "$environment" --arg port "$port" \
    --argjson memory "$memory" --argjson cpus "$cpus" --argjson pids "$pids" '
    def lower($limit): (tonumber? // 0) as $own | if $own > 0 and $own < $limit then $own else $limit end;
    (."x-deadlift".route | capture("^(?<service>.+):(?<port>[0-9]+)$")) as $route
    | {"deadlift.app": $app, "deadlift.environment": $environment} as $labels
    | {
        services: (.services | with_entries(.value |= {
          mem_limit: (.mem_limit | lower($memory)),
          cpus: (.cpus | lower($cpus)),
          pids_limit: (.pids_limit | lower($pids)),
          security_opt: ["no-new-privileges:true"],
          logging: {driver: "json-file", options: {"max-size": "10m", "max-file": "3"}},
          labels: $labels
        }) | .[$route.service].ports = [
          {host_ip: "127.0.0.1", published: $port, target: ($route.port | tonumber)}
        ]),
        volumes: (.volumes // {} | map_values({labels: $labels}))
      }' <<<"$config"
}

case ${1:-} in
  check)
    [[ $# -eq 3 ]] || usage "check needs <dir> <project>"
    check "$2" "$3"
    ;;
  override)
    [[ $# -ge 3 ]] || usage "override needs <dir> <project>"
    shift
    override "$@"
    ;;
  *) usage "'${1:-}' isn't a command: check or override" ;;
esac
