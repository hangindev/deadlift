#!/usr/bin/env bash
# deadlift-entry: a Server's one way in for an App's CI. Bootstrap installs it, owned by root, at
# /usr/local/sbin/deadlift-entry, and `deadlift app add` gives each App's deploy user one sudo
# rule that runs it and nothing else (ADR 0001):
#
#   deadlift-<app> ALL=(root) NOPASSWD: /usr/local/sbin/deadlift-entry
#
# Usage, as `deliver` calls it over Tailscale SSH:
#   tar -cf - compose.yaml token |
#     ssh deadlift-<app>@<server> sudo deadlift-entry --protocol <n> <operation> [arguments...]
#
# It works out the App from the user who called it (SUDO_USER, deadlift-<app>), never from its
# arguments, so a deploy user can only ever act on its own App. It reads the delivery, one tar on
# stdin holding at most compose.yaml (the App's Compose template) and token (the job's registry
# token) as plain files, and refuses anything else. It then runs the deploy script that bootstrap
# installed beside it, which checks the operation's arguments and runs it under the Server's lock.
# Nothing in a delivery is ever executed, and the delivery is removed afterwards, token included.
#
# The delivery protocol is versioned, so a `deliver` newer than the Server is refused with a
# reason saying to re-run bootstrap, rather than misread.
#
# Exit codes: 2 when it refuses, changing and running nothing; otherwise the deploy script's
# (0 done, 1 failed, 2 refused, 75 skipped).
#
# Settings, for the tests only: DEADLIFT_STATE_DIR and DEADLIFT_LIB_DIR move the Server's state
# and the installed deploy script. sudo's env_reset never passes them on, so on a Server they're
# always the defaults.
set -euo pipefail
umask 077

state=${DEADLIFT_STATE_DIR:-/srv/deadlift}
lib=${DEADLIFT_LIB_DIR:-/usr/local/lib/deadlift}

# The delivery protocols this entry command speaks. A new one is added here, by bootstrap, before
# any `deliver` sends it.
protocols=(1)
# The most a delivery may hold: a Compose template and a token are far smaller.
max_delivery_bytes=$((1024 * 1024))

refuse() {
  echo "deadlift: refused the delivery: $1. Nothing was run." >&2
  exit 2
}

# The App, from the deploy user that sudo says called us.
user=${SUDO_USER:-}
app=${user#deadlift-}
if [[ $user != deadlift-* || ! $app =~ ^[a-z0-9][a-z0-9-]*$ || ! -f $state/apps/$app/app.conf ]]; then
  refuse "'${user:-nobody}' isn't the deploy user of an App on this Server"
fi

if [[ ${1:-} != --protocol || -z ${2:-} ]]; then
  refuse "it named no protocol version. Use deadlift's deliver action, which sends one"
fi
protocol=$2
shift 2
supported=" ${protocols[*]} "
if [[ $supported != *" $protocol "* ]]; then
  refuse "it uses delivery protocol '$protocol', and this Server speaks only ${protocols[*]}. Re-run \`deadlift server bootstrap\` to bring the Server up to date"
fi

case ${1:-} in
  deploy | rollback | destroy | sweep) ;;
  *) refuse "'${1:-}' isn't an operation. The operations are deploy, rollback, destroy and sweep" ;;
esac

mkdir -p "$state/deliveries"
delivery=$(mktemp -d "$state/deliveries/XXXXXX")
trap 'rm -rf "$delivery"' EXIT
archive=$delivery/archive.tar
files=$delivery/files

head -c $((max_delivery_bytes + 1)) >"$archive"
size=$(stat -c %s "$archive")
((size > 0)) || refuse "stdin was empty, and a delivery is a tar archive"
((size <= max_delivery_bytes)) || refuse "it's over $max_delivery_bytes bytes"
listing=$(tar -tvf "$archive" 2>/dev/null) || refuse "it isn't a tar archive"
members=$(tar -tf "$archive")
if [[ -n $listing ]]; then
  # Only plain files: no directories, links or devices.
  if grep -qv '^-' <<<"$listing"; then refuse "it holds something other than plain files"; fi
  # Sizes as unpacked, so a compressed archive can't grow past the limit either.
  unpacked=$(awk '{ total += $3 } END { print total }' <<<"$listing")
  ((unpacked <= max_delivery_bytes)) || refuse "it unpacks to over $max_delivery_bytes bytes"
fi
seen=' '
while IFS= read -r member; do
  [[ -n $member ]] || continue
  name=${member#./}
  case $name in
    compose.yaml | token) ;;
    *) refuse "it holds '$member', and only compose.yaml and token may be delivered" ;;
  esac
  [[ $seen != *" $name "* ]] || refuse "it holds $name twice"
  seen+="$name "
done <<<"$members"
mkdir "$files"
tar -xf "$archive" -C "$files" --no-same-owner --no-same-permissions ||
  refuse "it couldn't be unpacked"
rm -f "$archive"

# Not exec, so the delivery is removed afterwards.
status=0
"$lib/deploy.sh" --app "$app" --delivery "$files" "$@" </dev/null || status=$?
exit "$status"
