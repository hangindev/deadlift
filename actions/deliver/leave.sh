#!/usr/bin/env bash
# leave: takes a CI job off the tailnet once the deliver action is done, as the Tailscale
# action's own clean-up would at the end of the job. Logging out removes the ephemeral node at
# once, and stopping tailscaled lets a later step in the job join again.
set -euo pipefail

# Never installed: the join failed before it got that far.
command -v tailscaled >/dev/null || exit 0

sudo tailscale logout ||
  echo "::warning::deadlift: couldn't log out of the tailnet. The node is removed when the job ends."
sudo pkill -x tailscaled || true
for ((waited = 0; waited < 20; waited++)); do
  pgrep -x tailscaled >/dev/null || break
  sleep 0.5
done
if pgrep -x tailscaled >/dev/null; then
  echo "deadlift: tailscaled didn't stop, so this job can't join the tailnet again." >&2
  exit 1
fi
sudo tailscaled --cleanup
