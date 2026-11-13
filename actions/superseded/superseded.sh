#!/usr/bin/env bash
# Says whether the tracked branch has moved past a commit, so a Dev workflow can skip a deploy that
# a newer commit's own run deploys, instead of putting older code on the Dev Environment.
#
# Usage: superseded.sh <branch> <commit>
# Writes `superseded=true|false`, `head=<the branch's head>` and `reason=` to $GITHUB_OUTPUT (or
# stdout). A commit the branch no longer holds, after a force-push, counts as superseded too.
# Needs GH_TOKEN with contents: read, and GITHUB_REPOSITORY. Asks GitHub, so it needs no clone.
# Exits 0 with an answer, 1 when GitHub couldn't say (no answer), 2 on bad usage.
set -euo pipefail

refuse() {
  echo "deadlift: $1" >&2
  exit 2
}

(($# == 2)) || refuse "usage: superseded.sh <branch> <commit>"
branch=$1
commit=$2
repo=${GITHUB_REPOSITORY:-}
: "${GITHUB_OUTPUT:=/dev/stdout}"

[[ $repo =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
  refuse "GITHUB_REPOSITORY must be <owner>/<name>, not '$repo'."
[[ $commit =~ ^[0-9a-f]{7,40}$ ]] || refuse "the commit must be a SHA, not '$commit'."
# Plain branch names only, so the name can go into the API's paths as it is.
if ! [[ $branch =~ ^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$ ]] || [[ $branch == *..* || $branch == .* ]]; then
  refuse "the branch must be a plain branch name such as main, not '$branch'."
fi

decide() {
  local reason="deadlift: $2"
  echo "$reason" >&2
  {
    echo "superseded=$1"
    echo "head=$head"
    echo "reason=$reason"
  } >>"$GITHUB_OUTPUT"
  exit 0
}

fail() {
  echo "deadlift: $1" >&2
  exit 1
}

head=$(gh api "repos/$repo/git/ref/heads/$branch" --jq .object.sha) ||
  fail "couldn't find $branch's head in $repo, so this says nothing either way."
if [[ $commit == "$head" ]]; then
  decide false "$commit is $branch's head, so it's the newest."
fi

status=$(gh api "repos/$repo/compare/$commit...$head" --jq .status) ||
  fail "couldn't compare $commit with $branch's head $head, so this says nothing either way."
case $status in
  identical) decide false "$commit is $branch's head, so it's the newest." ;;
  ahead) decide true "$branch has moved past $commit to $head, whose own run deploys it." ;;
  behind | diverged) decide true "$commit isn't in $branch's history any more ($status)." ;;
  *) fail "GitHub compared $commit with $branch's head $head as '$status', which this doesn't know." ;;
esac
