#!/usr/bin/env bash
# Readies a throwaway machine for Seam B, as root: `deadlift server bootstrap localhost` then
# reaches it over real SSH as root, with the test's own key, and finds a stand-in `tailscale`,
# the one thing faked, so bootstrap doesn't install the real one, and deploys and `deadlift app`
# get the answers a Server's Tailscale would give.
#
#   prepare.sh <public key file>
#
# Only for a throwaway machine (see test/support/throwaway.ts): it installs and starts sshd and
# lets the key log in as root.
set -euo pipefail

key=${1:?prepare.sh needs a public key file}

if ! command -v sshd >/dev/null && [[ ! -x /usr/sbin/sshd ]]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -qq --yes --no-install-recommends openssh-server
fi
systemctl start ssh

install -d -m 0700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 0600 /root/.ssh/authorized_keys
if ! grep -qxF "$(<"$key")" /root/.ssh/authorized_keys; then
  cat "$key" >>/root/.ssh/authorized_keys
fi

# The runner image leaves its own sudo rule group-readable, which sudo accepts and `visudo -c`
# doesn't. A Server's rules must pass `visudo -c`, so it gets the mode visudo wants.
for rule in /etc/sudoers.d/*; do
  if [[ -f $rule && $(stat -c %a "$rule") != 440 ]]; then chmod 0440 "$rule"; fi
done

# The Seam A tests' fake, which answers what the deploy script and app.sh ask of Tailscale on a
# Server. Installed over an earlier stand-in, never over a real tailscale.
fake=$(dirname "${BASH_SOURCE[0]}")/../fakes/tailscale
if ! command -v tailscale >/dev/null || grep -qF 'fake tailscale:' /usr/local/bin/tailscale 2>/dev/null; then
  if ! cmp -s "$fake" /usr/local/bin/tailscale; then install -m 0755 "$fake" /usr/local/bin/tailscale; fi
fi
