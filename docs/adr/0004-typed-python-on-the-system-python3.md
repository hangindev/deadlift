---
status: accepted
---

# deadlift is typed Python, run on the system python3

deadlift was first written in Bash, with its tests in TypeScript. As the deploy script, the template check and the CLI grew, Bash's error handling and quoting, and parsing JSON with `jq` and YAML with an inline Python, became the hard part of every change. We decided to move everything deadlift ships, and its tests, to typed Python: the Server's code (the entry command, the deploy script, the template check, bootstrap, the firewall, the watchdog), the maintainer's CLI, and the composite actions' scripts. Behaviour, the CLI's commands and flags, reason lines (`deadlift: `), `deadlift-output:` lines, exit codes (0, 1, 2, 75) and the security properties of ADRs 0001 to 0003 stay as they are.

Python 3.12, checked by pyright in strict mode and tested with pytest. Nothing that ships needs uv, pip or a virtualenv: uv only manages the tooling in a checkout. What ships runs on the system `python3` of the machine it's on, Ubuntu 24.04's on a Server and a CI runner, so it uses the standard library only, with one exception: the Server's code may use PyYAML, from apt (`python3-yaml`). Bootstrap uses the standard library only, because it runs on a fresh machine before `python3-yaml` is installed. `jq` is no longer used anywhere.

## Rules for the Server's code

The Server's code runs as root on behalf of deploy users who are trusted only with their own App (ADR 0001). So:

- **Every privileged entry point runs as `python3 -I`**: the entry command, the deploy script, the watchdog, the firewall, the Server half of `deadlift app`, and anything else run as root. Isolated mode ignores every `PYTHON*` variable and the user's site-packages, and puts neither the working directory nor the script's own directory on `sys.path`.
- **deadlift's modules load from a fixed, root-owned place, named explicitly.** Because `-I` drops the script's directory from `sys.path`, an entry point can't import its neighbours the usual way, and must not work out where they are from its own path or the environment either. Bootstrap installs the Server's modules under `/usr/local/lib/deadlift/python`, owned by root and writable only by root, and installs each entry point as a launcher at a fixed path (such as `/usr/local/sbin/deadlift-entry` or `/usr/local/lib/deadlift/deploy`): a few lines, `#!/usr/bin/python3 -I`, with that directory written in as a constant, added to `sys.path` before importing the entry point's module. Nothing a deploy user can write is ever on `sys.path`: the rest of it is the standard library and the system's root-owned `dist-packages`. A launcher refuses to run unless isolated, and writes no bytecode beside the modules.
- **Every child gets a scrubbed, explicit environment.** No child of the Server's code inherits its environment: each is given only the variables it needs, starting from PATH (sudo's `secure_path`, or systemd's) and HOME. Compose's `${VAR}` substitution prefers the shell's environment to `--env-file`, so anything else could reach an App's template.
- **The Server's lock never leaks into a background process.** The lock is `flock(2)` on `/srv/deadlift/lock`, the same lock the Bash scripts took with `flock(1)`. Its file descriptor is close-on-exec (Python's default), never made inheritable and never passed to a child, and children are started with `close_fds`, so a `docker` or `tailscale` process still running after an operation can't keep the Server locked.
- **Children are run in one place**, `deadlift.core.process`, which enforces the last two rules and never uses a shell.

## Considered Options

- **Keep Bash.** Rejected: see above. The scripts were past the size where Bash stays readable and safe.
- **Ship a virtualenv or a zipapp with pinned dependencies.** Rejected: it adds an install step and an upgrade path to every Server and runner for little gain, and a Server already has what deadlift needs from apt.
- **Find modules relative to the launcher, or through an environment variable.** Rejected: both make what root imports depend on something other than a fixed path that only root can write.

## Consequences

- The port runs script by script, with the TypeScript suite, which drives deadlift only from outside, as the oracle that the Python behaves the same. Then the suite moves to pytest, and the TypeScript tooling is removed once pytest covers everything.
- A maintainer's computer needs Python 3.12 or later for the CLI.
