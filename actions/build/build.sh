#!/usr/bin/env bash
# build: builds an App's named images and pushes them to GHCR with deadlift's tag convention, run
# by the `build` action from the App's checkout.
#
# Usage:
#   build.sh --repository <owner/name> --commit <sha> [--platform <platform>] [--source <url>] -- <image>...
#
#   --repository  The App's repository. Its images are GHCR packages under it.
#   --commit      The commit built, which names every image's tag.
#   --platform    The one platform to build for, the Server's: linux/amd64 by default.
#   --source      The repository's URL, labelled on each image so GHCR links the package to it.
#   <image>       One image: its name, then any of context=<dir> (. by default),
#                 file=<Dockerfile> (<context>/Dockerfile by default) and target=<stage>,
#                 separated by spaces, as in `worker context=worker target=runtime`.
#
# Each image is pushed as ghcr.io/<owner>/<name>/<image>:sha-<commit>, lowercased. That's what
# `prune-registry` keeps and deletes by, and it needs each version to be one single-platform
# image with no attestations, which GHCR would list as untagged versions of their own.
#
# Image names are lowercase letters and digits, joined by single `_`, so that each reaches the
# App's template as ${DEADLIFT_IMAGE_<NAME>}.
#
# Environment:
#   REGISTRY_USERNAME, REGISTRY_TOKEN  The login to GHCR, with packages: write. It's kept in a
#                                      Docker config of this run's own, removed afterwards.
#   GITHUB_OUTPUT        When set, gets `images`: one <name>=<ref> per line, as `deliver` takes.
#   GITHUB_STEP_SUMMARY  When set, gets the references pushed.
#
# Exit codes: 0 every image pushed, 1 a build or push failed, 2 refused (nothing built).
set -euo pipefail

usage() {
  echo "deadlift: $1, so nothing was built." >&2
  sed -n 's/^#   \(build\.sh .*\)/Usage: \1/p' "$0" >&2
  exit 2
}

repository='' commit='' platform=linux/amd64 source=''
while (($#)); do
  case $1 in
    --repository | --commit | --platform | --source)
      [[ $# -ge 2 && -n $2 ]] || usage "$1 needs a value"
      declare "${1#--}=$2"
      shift 2
      ;;
    --) shift && break ;;
    *) usage "unknown argument '$1'" ;;
  esac
done
[[ $repository =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || usage "--repository needs <owner>/<name>"
[[ $commit =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || usage "--commit needs a full commit SHA, not '$commit'"
[[ $platform =~ ^[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?$ ]] ||
  usage "--platform needs one platform, such as linux/amd64, not '$platform'"
[[ -n ${REGISTRY_USERNAME:-} && -n ${REGISTRY_TOKEN:-} ]] ||
  usage "REGISTRY_USERNAME and REGISTRY_TOKEN must be set"

# Each image's settings, by name, in the order given.
names=()
declare -A contexts=() files=() targets=()
for spec in "$@"; do
  read -ra words <<<"$spec"
  ((${#words[@]})) || continue
  name=${words[0]}
  [[ $name =~ ^[a-z][a-z0-9]*(_[a-z0-9]+)*$ ]] ||
    usage "'$name' isn't an image name: lowercase letters and digits, joined by single _"
  [[ -z ${contexts[$name]:-} ]] || usage "the image $name is given twice"
  context=. file='' target=''
  for setting in "${words[@]:1}"; do
    case $setting in
      context=?*) context=${setting#*=} ;;
      file=?*) file=${setting#*=} ;;
      target=?*) target=${setting#*=} ;;
      *) usage "'$setting' isn't a setting of the image $name: context=, file= or target=" ;;
    esac
  done
  names+=("$name")
  contexts[$name]=$context
  files[$name]=${file:-$context/Dockerfile}
  targets[$name]=$target
done
((${#names[@]})) || usage "no image was named"

DOCKER_CONFIG=$(mktemp -d)
export DOCKER_CONFIG
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
docker login ghcr.io --username "$REGISTRY_USERNAME" --password-stdin <<<"$REGISTRY_TOKEN" >/dev/null ||
  {
    echo "deadlift: couldn't log in to ghcr.io as $REGISTRY_USERNAME, so nothing was built." >&2
    exit 1
  }

image_base=ghcr.io/${repository,,}
pushed=()
for name in "${names[@]}"; do
  ref=$image_base/$name:sha-$commit
  args=(--platform "$platform" --provenance=false --sbom=false --file "${files[$name]}" --tag "$ref" --push)
  if [[ -n $source ]]; then args+=(--label "org.opencontainers.image.source=$source"); fi
  args+=(--label "org.opencontainers.image.revision=$commit")
  if [[ -n ${targets[$name]} ]]; then args+=(--target "${targets[$name]}"); fi
  echo "Building $name as $ref"
  if ! docker buildx build "${args[@]}" "${contexts[$name]}"; then
    echo "deadlift: couldn't build and push the image $name.${pushed[*]:+ Pushed before it: ${pushed[*]#*=}.}" >&2
    exit 1
  fi
  pushed+=("$name=$ref")
done

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
  printf 'images<<DEADLIFT_IMAGES\n%s\nDEADLIFT_IMAGES\n' "$(printf '%s\n' "${pushed[@]}")" >>"$GITHUB_OUTPUT"
fi
if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  {
    echo "### deadlift build: pushed ${#pushed[@]} image$( ((${#pushed[@]} == 1)) || echo s)"
    echo
    printf -- "- \`%s\`\n" "${pushed[@]#*=}"
  } >>"$GITHUB_STEP_SUMMARY"
fi
echo "deadlift: pushed ${pushed[*]#*=}" >&2
