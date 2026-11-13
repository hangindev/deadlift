#!/usr/bin/env bash
# deliver: the client half of a delivery, run by the `deliver` action on the App's CI job once it
# has joined the tailnet. It sends the delivery over SSH as the App's deploy user to the Server's
# entry command, and reports what happened.
#
# Usage:
#   deliver.sh --server <host> --app <app> [--compose <file>] <operation> [arguments...]
#
#   --server   The Server's name on the tailnet.
#   --app      The App, whose deploy user deadlift-<app> the delivery logs in as.
#   --compose  The App's Compose template, sent with `deploy` only. compose.yaml by default.
#   The operation and its arguments are passed to the entry command as given, for example
#   `deploy feature-42 --image web=ghcr.io/owner/app/web:sha-1234`.
#
# Environment:
#   DEADLIFT_REGISTRY_TOKEN  The job's registry token, which the Server pulls the App's images
#                            with. Sent inside the delivery, never on a command line, and removed
#                            on the Server after the operation.
#   GITHUB_OUTPUT            Where the step's outputs go, when set:
#                              outcome      done, skipped or failed
#                              reason       the last line starting `deadlift: `
#                              url          the Environment's address, when it has one
#                              environment  the Environment acted on
#                              swept        the keys of Feature Environments the Sweep destroyed
#
# The delivery is one tar on the entry command's stdin, holding only compose.yaml and the token.
# The Server runs its own installed deploy script, never anything delivered.
#
# Exit codes: 0 for done and skipped, so a skip doesn't fail the step. Otherwise the entry
# command's (1 failed, 2 refused), or 1 when the Server couldn't be reached.
set -euo pipefail

# The delivery protocol this client speaks. The entry command refuses one it doesn't support,
# saying to re-run bootstrap.
protocol=1

usage() {
  echo "deadlift: $1" >&2
  sed -n 's/^#   \(deliver.sh .*\)/Usage: \1/p' "$0" >&2
  exit 2
}

server='' app='' compose=compose.yaml
while (($#)); do
  case $1 in
    --server | --app | --compose)
      [[ $# -ge 2 && -n $2 ]] || usage "$1 needs a value"
      declare "${1#--}=$2"
      shift 2
      ;;
    -*) usage "unknown option $1" ;;
    *) break ;;
  esac
done
[[ -n $server ]] || usage "--server is required"
[[ -n $app ]] || usage "--app is required"
(($#)) || usage "no operation given"
operation=$1

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/delivery"
members=()
if [[ $operation == deploy ]]; then
  [[ -f $compose ]] || usage "there's no Compose template at $compose"
  cp "$compose" "$work/delivery/compose.yaml"
  members+=(compose.yaml)
fi
if [[ -n ${DEADLIFT_REGISTRY_TOKEN:-} ]]; then
  (umask 077 && printf '%s' "$DEADLIFT_REGISTRY_TOKEN" >"$work/delivery/token")
  members+=(token)
fi

remote=$(printf '%q ' sudo --non-interactive /usr/local/sbin/deadlift-entry --protocol "$protocol" "$@")
status=0
tar -C "$work/delivery" -cf - "${members[@]}" |
  ssh -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=accept-new \
    "deadlift-$app@$server" "$remote" 2>&1 | tee "$work/log" || status=${PIPESTATUS[1]}

# The Server reports its outputs on lines of their own: `deadlift-output: <name>=<value>`.
output() {
  sed -n "s/^deadlift-output: $1=//p" "$work/log" | tail -n 1
}
reason=$(grep -E '^deadlift: ' "$work/log" | tail -n 1 || true)
case $status in
  0) outcome=done ;;
  75) outcome=skipped ;;
  255)
    outcome=failed
    reason="deadlift: couldn't reach $server over SSH as deadlift-$app, so nothing was run."
    echo "$reason" >&2
    status=1
    ;;
  *) outcome=failed ;;
esac

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
  {
    echo "outcome=$outcome"
    echo "reason=$reason"
    echo "url=$(output url)"
    echo "environment=$(output environment)"
    echo "swept=$(output swept)"
  } >>"$GITHUB_OUTPUT"
fi

if [[ $outcome == failed ]]; then exit "$status"; fi
exit 0
