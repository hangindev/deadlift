#!/usr/bin/env bash
# Runs Seam B, then Seam A on the Server it leaves, in a fresh Multipass VM of Ubuntu 24.04, the
# way CI does on its runner. The VM is marked as a throwaway machine, so the tests agree to
# reconfigure it, and deleted afterwards.
#
#   test/seam-b/multipass.sh
#
# Needs multipass on this machine. Takes this checkout's committed and uncommitted files, not
# its node_modules.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
vm=deadlift-seam-b-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
node_version=24.21.0

command -v multipass >/dev/null || { echo "deadlift: Seam B in a VM needs multipass" >&2 && exit 2; }

trap 'multipass delete --purge "$vm" 2>/dev/null || true' EXIT
echo "Launching $vm"
multipass launch 24.04 --name "$vm" --cpus 2 --memory 4G --disk 20G

# The tests refuse to reconfigure a machine without this mark (test/support/throwaway.ts).
multipass exec "$vm" -- sudo touch /etc/deadlift-throwaway-machine

echo "Copying the checkout"
(cd "$repo" && git ls-files -z --cached --others --exclude-standard | tar --null -T - -czf -) |
  multipass exec "$vm" -- bash -c 'mkdir -p ~/deadlift && tar -xzf - -C ~/deadlift'

echo "Installing what the tests need: Node and pnpm"
multipass exec "$vm" -- bash -euo pipefail -c "
  curl -fsSL https://nodejs.org/dist/v$node_version/node-v$node_version-linux-\$(dpkg --print-architecture | sed s/amd64/x64/).tar.xz |
    sudo tar -xJf - -C /usr/local --strip-components=1
  sudo corepack enable
  # Unlike CI's runner, the VM has no Docker, so bootstrap installs it, as on a fresh Server. The
  # docker group is made now, and Docker's package keeps it, so the tests can drive Docker.
  sudo groupadd --system docker
  sudo usermod --append --groups docker ubuntu
"

echo "Running Seam B, then Seam A on the Server it leaves"
# A new login, so the docker group applies.
multipass exec "$vm" -- sudo -u ubuntu -i bash -euo pipefail -c "
  cd ~/deadlift
  pnpm install --frozen-lockfile
  pnpm test:seam-b
  DEADLIFT_TEST_BOOTSTRAPPED=true pnpm test
"
