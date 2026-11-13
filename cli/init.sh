#!/usr/bin/env bash
# deadlift init: writes deadlift's starter workflows (cli/starters) into the App's repository, at
# .github/workflows/deadlift-{feature,dev,sweep,rollback}.yml. Run by `deadlift init`.
#
# Usage:
#   deadlift init <app> --server <server> [--branch <branch>] [--image <name>]...
#
#   <app>     The App, as `deadlift app add` named it.
#   --server  The Server's name on the tailnet.
#   --branch  The branch the Dev Environment tracks, main by default.
#   --image   An image the workflows build, by its name, once per image: web by default. Each is
#             built from the repository's root and its Dockerfile, which the build step of
#             deadlift-feature and deadlift-dev can change.
#
# The workflows are the App's own from then on. Each starts with a line holding a hash of what
# deadlift init wrote, so a run again rewrites only the ones nobody has changed since, and keeps
# the rest as they are.
#
# Every reason line starts with `deadlift: `. Exit codes: 0 done, 1 failed, 2 refused or bad
# usage (nothing was written).
set -euo pipefail

starters=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)/starters
names=(deadlift-feature deadlift-dev deadlift-sweep deadlift-rollback)

usage() {
  echo "deadlift: $1. Nothing was written." >&2
  sed -n 's/^#   \(deadlift init .*\)/Usage: \1/p' "${BASH_SOURCE[0]}" >&2
  exit 2
}

app=${1:-}
[[ -n $app && $app != -* ]] || usage "init needs the <app> first"
shift
# The same rules as `deadlift app add`.
[[ $app =~ ^[a-z]([a-z0-9-]{0,21}[a-z0-9])?$ && ! $app =~ -feature(-|$) ]] ||
  usage "'$app' isn't an App name, as \`deadlift app add\` takes them: up to 23 of a-z, 0-9 and -"
server='' branch=main images=()
while (($#)); do
  case $1 in
    --server | --branch | --image)
      [[ $# -ge 2 ]] || usage "$1 needs a value"
      case $1 in
        --server) server=$2 ;;
        --branch) branch=$2 ;;
        --image) images+=("$2") ;;
      esac
      shift 2
      ;;
    *) usage "unknown argument '$1'" ;;
  esac
done
[[ -n $server ]] || usage "init needs --server, the Server's name on the tailnet"
[[ $server =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || usage "'$server' isn't a host name"
# Plain names only, which are safe unquoted in YAML.
if [[ ! $branch =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] ||
  ! git check-ref-format --branch "$branch" >/dev/null 2>&1; then
  usage "'$branch' isn't a branch name deadlift init takes: a-z, A-Z, 0-9, ., _, / and -"
fi
((${#images[@]})) || images=(web)
for image in "${images[@]}"; do
  # As the build action names images.
  [[ $image =~ ^[a-z0-9]+(_[a-z0-9]+)*$ ]] ||
    usage "'$image' isn't an image name: lowercase letters and digits, joined by single _"
done

repo=$(git rev-parse --show-toplevel 2>/dev/null) ||
  usage "deadlift init writes into the App's git repository, and $PWD isn't in one"

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi | cut -d ' ' -f 1
}

# A starter with this App's values in place of its @placeholders@.
render() {
  local line
  while IFS= read -r line || [[ -n $line ]]; do
    if [[ $line =~ ^( *)@images@$ ]]; then
      for image in "${images[@]}"; do printf '%s%s\n' "${BASH_REMATCH[1]}" "$image"; done
      continue
    fi
    line=${line//@app@/$app}
    line=${line//@server@/$server}
    line=${line//@branch@/$branch}
    line=${line//@image-names@/${images[*]}}
    printf '%s\n' "$line"
  done <"$starters/$1.yml"
}

# The header's 3 lines, holding the hash of the rest of the file as deadlift init wrote it.
header() {
  echo "# Written by \`deadlift init\`, and the App's own: once this file changes, deadlift init"
  echo "# leaves it alone. Starter sha256:$1"
  echo "#"
}

mkdir -p "$repo/.github/workflows"
for name in "${names[@]}"; do
  file=$repo/.github/workflows/$name.yml
  shown=.github/workflows/$name.yml
  body=$(render "$name")
  new=$(header "$(printf '%s\n' "$body" | sha256)" && printf '%s\n' "$body")
  if [[ -e $file ]]; then
    # What deadlift init wrote last, by the hash in its header, and what the file holds now.
    written=$(sed -n '2s/^# .* Starter sha256:\([0-9a-f]\{64\}\)$/\1/p' "$file")
    if [[ -z $written ]]; then
      echo "Kept $shown, which deadlift init didn't write"
      continue
    fi
    if [[ $(tail -n +4 "$file" | sha256) != "$written" ]]; then
      echo "Kept $shown, which has changed since deadlift init wrote it"
      continue
    fi
    if [[ $(cat "$file") == "$new" ]]; then
      echo "Unchanged $shown"
      continue
    fi
    verb=Updated
  else
    verb=Wrote
  fi
  printf '%s\n' "$new" >"$file.deadlift-init"
  mv "$file.deadlift-init" "$file"
  echo "$verb $shown"
done

cat <<EOF

Next:
- Commit the workflows. They're the App's own, to change as it likes.
- Set the repository variables TS_OAUTH_CLIENT_ID and TS_AUDIENCE to the Client ID and Audience
  of the federated credential that \`deadlift app add\` printed, if they aren't set yet.
- The workflows deliver compose.yaml, at the repository's root, as the App's Compose template.
  Each image reaches it as \${DEADLIFT_IMAGE_<NAME>}, such as \${DEADLIFT_IMAGE_${images[0]^^}}.
- The skip-deploy label on a pull request stops its Feature Environment being deployed.
EOF
