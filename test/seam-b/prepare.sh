#!/usr/bin/env bash
# Readies a throwaway machine for Seam B, as root: `deadlift server bootstrap localhost` then
# reaches it over real SSH as root, with the test's own key, and finds a stand-in `tailscale`,
# the one thing faked, so bootstrap doesn't install the real one.
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

if ! command -v tailscale >/dev/null; then
  cat >/usr/local/bin/tailscale <<'EOF'
#!/usr/bin/env bash
# Seam B's stand-in for Tailscale, so bootstrap finds it installed. Bootstrap never calls it.
echo "fake tailscale: Seam B has no answer for 'tailscale $*'" >&2
exit 1
EOF
  chmod 0755 /usr/local/bin/tailscale
fi
