#!/usr/bin/env bash
# Runs the server-setup tests, then the delivery tests on the Server they leave, in a fresh
# Multipass VM of Ubuntu 24.04, the way CI does on its runner. The VM is marked as a throwaway
# machine, so the tests agree to reconfigure it, and deleted afterwards.
#
#   tests/server_setup/multipass.sh
#
# Needs multipass on this machine. Takes this checkout's committed and uncommitted files, not
# its .venv.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
vm=deadlift-server-setup-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
# As CI's setup-uv step pins it.
uv_version=0.11.26

command -v multipass >/dev/null ||
  { echo "deadlift: the server-setup tests in a VM need multipass" >&2 && exit 2; }

trap 'multipass delete --purge "$vm" 2>/dev/null || true' EXIT
echo "Launching $vm"
multipass launch 24.04 --name "$vm" --cpus 2 --memory 4G --disk 20G

# The tests refuse to reconfigure a machine without this mark (tests/support/throwaway.py).
multipass exec "$vm" -- sudo touch /etc/deadlift-throwaway-machine

echo "Copying the checkout"
(cd "$repo" && git ls-files -z --cached --others --exclude-standard | tar --null -T - -czf -) |
  multipass exec "$vm" -- bash -c 'mkdir -p ~/deadlift && tar -xzf - -C ~/deadlift'

echo "Installing what the tests need: uv"
multipass exec "$vm" -- bash -euo pipefail -c "
  curl -LsSf https://astral.sh/uv/$uv_version/install.sh |
    sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
  # Unlike CI's runner, the VM has no Docker, so bootstrap installs it, as on a fresh Server. The
  # docker group is made now, and Docker's package keeps it, so the tests can drive Docker.
  sudo groupadd --system docker
  sudo usermod --append --groups docker ubuntu
"

echo "Running the server-setup tests, then the delivery tests on the Server they leave"
# A new login, so the docker group applies. As in CI, uv uses the system python3, which is
# Ubuntu 24.04's, as on a Server, and bootstrap's tests run first, by path: a plain
# `uv run pytest` leaves tests/server_setup out.
multipass exec "$vm" -- sudo -u ubuntu -i bash -euo pipefail -c "
  cd ~/deadlift
  export UV_PYTHON=/usr/bin/python3
  uv sync --locked
  uv run pytest -v tests/server_setup/test_bootstrap.py tests/server_setup/test_app.py
  DEADLIFT_TEST_BOOTSTRAPPED=true uv run pytest tests/delivery -rs
"
