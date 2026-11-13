#!/usr/bin/env bash
# Deletes an App's image versions from GHCR, except the ones it's told to keep. It knows nothing
# about pull requests: what to keep is the caller's choice.
#
# Usage: prune-registry.sh [--dry-run] (--keep <versions> | --keep-none) <image>...
#   <versions>   tags (such as sha-<commit>) or digests, separated by commas or whitespace. A
#                version is kept when any of its tags, or its digest, is listed.
#   --keep-none  delete every version. An empty keep list is refused without it, and so is one
#                that names none of an image's versions, since either would delete them all.
#   --dry-run    say what would be deleted, and delete nothing.
# Needs GH_TOKEN with packages: write (and each package giving the repository the Admin role), and
# GITHUB_REPOSITORY. Exits 0 done, 1 failed, 2 refused (nothing deleted).
set -euo pipefail

usage="usage: prune-registry.sh [--dry-run] (--keep <versions> | --keep-none) <image>..."
refuse() {
  echo "deadlift: $1" >&2
  exit 2
}
fail() {
  echo "deadlift: $1" >&2
  exit 1
}

keep=''
keep_given=''
keep_none=''
dry_run=''
while (($#)); do
  case $1 in
    --keep)
      (($# >= 2)) || refuse "$usage"
      keep+=" $2"
      keep_given=yes
      shift 2
      ;;
    --keep-none) keep_none=yes && shift ;;
    --dry-run) dry_run=yes && shift ;;
    --) shift && break ;;
    -*) refuse "$usage" ;;
    *) break ;;
  esac
done

# The versions to keep, as a JSON list: tags or digests, separated by commas or whitespace.
keep_json=$(jq -Rsc '[splits("[,\\s]+") | select(. != "")]' <<<"$keep")
if [[ $keep_none ]]; then
  [[ $keep_given ]] && refuse "pass either --keep or --keep-none, not both."
elif [[ $keep_json == '[]' ]]; then
  refuse "refused: no versions to keep were given, which would delete every version, so nothing was deleted. Pass --keep-none (keep-none: true) to mean that."
fi

(($#)) || refuse "$usage"
for image in "$@"; do
  [[ $image =~ ^[a-z0-9]+([._-][a-z0-9]+)*$ ]] || refuse "'$image' isn't an image name such as web."
done
repo=${GITHUB_REPOSITORY:-}
[[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
  refuse "GITHUB_REPOSITORY must be <owner>/<name>, not '$repo'."
# GHCR names an image ghcr.io/<owner>/<repository>/<image>, all lowercase, and its package
# <repository>/<image>.
repo=${repo,,}
owner=${repo%%/*}
# GHCR's API puts a user's packages under users/ and an organisation's under orgs/.
owner_type=$(gh api "repos/$repo" --jq .owner.type) ||
  fail "couldn't look up $repo to find its packages, so nothing was deleted."
case $owner_type in
  Organization) owner_path="orgs/$owner" ;;
  *) owner_path="users/$owner" ;;
esac

# Every image's versions are listed and checked before any is deleted. Each line of to_delete:
# <versions URL> <id> <what it is, e.g. web:sha-c1>.
to_delete=''
total=0
for image in "$@"; do
  versions_url="$owner_path/packages/container/${repo#*/}%2F$image/versions"
  versions=$(gh api --paginate "$versions_url?per_page=100" --jq '.[]' | jq -s .) ||
    fail "couldn't list $image's versions in GHCR (ghcr.io/$owner/${repo#*/}/$image), so nothing was deleted. The package must exist and give $repo the Admin role (its settings, Manage Actions access), and the token needs packages: write."
  count=$(jq length <<<"$versions")
  total=$((total + count))
  if [[ ! $keep_none ]] && ! jq -e --argjson keep "$keep_json" \
    'any(.[]; any(.name, .metadata.container.tags[]; IN($keep[])))' <<<"$versions" >/dev/null; then
    refuse "refused: none of $image's $count versions is one to keep, which would delete them all, so nothing was deleted. Keep names versions by tag (such as sha-<commit>) or digest."
  fi
  to_delete+=$(jq -r --arg url "$versions_url" --arg image "$image" --argjson keep "$keep_json" '
    .[]
    | select(any(.name, .metadata.container.tags[]; IN($keep[])) | not)
    | .metadata.container.tags as $tags
    | "\($url) \(.id) \($image)\(if ($tags | length) > 0 then ":" + ($tags | join(",")) else "@" + .name end)"
  ' <<<"$versions")$'\n'
done

printf -v images '%s, ' "$@"
images=${images%, }
deleted=0
while read -r versions_url id what; do
  [[ $id ]] || continue
  if [[ $dry_run ]]; then
    echo "Would delete $what"
    deleted=$((deleted + 1))
    continue
  fi
  gh api --method DELETE "$versions_url/$id" </dev/null >/dev/null ||
    fail "couldn't delete $what. $deleted versions were deleted before it, and the rest are still there. Deleting needs the package to give $repo the Admin role (its settings, Manage Actions access)."
  echo "Deleted $what"
  deleted=$((deleted + 1))
done <<<"$to_delete"
if [[ $dry_run ]]; then
  echo "deadlift: would delete $deleted of $total versions of $images (a dry run)."
else
  echo "deadlift: deleted $deleted of $total versions of $images."
fi
