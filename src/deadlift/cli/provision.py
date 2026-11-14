"""``deadlift server provision``: gets a new Server, walking the maintainer through each step only
they can do, in a browser or a terminal, and doing the rest over SSH from their own computer. In
order: the tailnet policy, buying the machine, the provider's firewall, bootstrap, joining the
tailnet, and closing public SSH. Built on the wizard library (deadlift.cli.wizard).

Hetzner Cloud appears only in these instructions. Bootstrap needs nothing of it: any fresh
Ubuntu 24.04 machine with a public IPv4 address and root SSH by key will do.

The tailnet policy entries it prints (cli/tailscale-policy.hujson) are set once per tailnet, by
tag, so a second Server on the same tailnet, and every App's Service, need nothing new there.
It never reads or writes the policy: the maintainer, or the setup-deadlift skill, merges them.

Safe to stop and run again: answers are kept in the env file (``provision.env`` under
``$XDG_STATE_HOME/deadlift``, ``~/.local/state`` by default) and offered again, and each stage
checks what's done. A run that finishes with nothing left to do by hand forgets them, so the next
run provisions a new Server.

Every ssh here that runs a command reading nothing gets /dev/null for stdin, or it would swallow
what the human types next.
"""

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from deadlift.cli import server_bootstrap
from deadlift.cli.common import ROOT, maintainer_env, run_command
from deadlift.cli.wizard import Wizard
from deadlift.core.outcome import Exit, Stop
from deadlift.core.process import Stream, run

SERVER_TAG = "tag:deadlift-server"
SSH_OPTIONS = ("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")
POLICY = ROOT / "cli" / "tailscale-policy.hujson"

_SERVER_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_IPV4 = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}")
_MAGIC_DNS_SUFFIX = re.compile(r'.*"MagicDNSSuffix": *"([^"]*)"')


def env_file() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or f"{os.environ.get('HOME', '')}/.local/state"
    return Path(state) / "deadlift" / "provision.env"


def policy_entries(hujson: str) -> str:
    """The policy file as the wizard prints it: its top comment lines dropped, indented."""
    return "".join(f"    {line}\n" for line in _lines(hujson) if not line.startswith("//"))


def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def magic_dns_suffix(status: str) -> str:
    """The tailnet DNS name in ``tailscale status --json``'s output, or ""."""
    for line in _lines(status):
        found = _MAGIC_DNS_SUFFIX.match(line)
        if found:
            return found.group(1)
    return ""


def _quiet(argv: Sequence[str]) -> bool:
    """Runs ``argv`` reading nothing and showing nothing: whether it succeeded."""
    done = run(argv, env=maintainer_env(), stdout=Stream.DEVNULL, stderr=Stream.DEVNULL)
    return done.ok


def _captured(argv: Sequence[str]) -> str:
    """What ``argv`` prints, reading nothing, its errors hidden ("" when it fails to run)."""
    return run(argv, env=maintainer_env(), stdout=Stream.CAPTURE, stderr=Stream.DEVNULL).stdout


def _stopped() -> Stop:
    # The Bash wizard's `exit 1`, with nothing more to say.
    return Stop(Exit.FAILED, None)


@dataclass
class Answers:
    """Answers kept in the env file that a later stage needs even when an earlier one is
    skipped."""

    server_name: str = ""
    server_ip: str = ""
    tailnet: str = ""
    policy_saved: str = ""
    firewall_created: str = ""


class Provision:
    def __init__(self) -> None:
        self.w = Wizard(6, env_file())
        self.a = Answers()

    def server_host(self) -> str:
        """The Server's name on the tailnet."""
        return f"{self.a.server_name}.{self.a.tailnet}"

    def tailnet_ready(self) -> bool:
        """Whether root on the Server answers over the tailnet yet."""
        return bool(self.a.tailnet and self.a.server_name) and _quiet(
            ["ssh", "-o", "BatchMode=yes", *SSH_OPTIONS, f"root@{self.server_host()}", "true"]
        )

    def public_ssh_answers(self) -> bool:
        """Whether root on the machine's public address still answers SSH."""
        return _quiet(
            ["ssh", "-o", "BatchMode=yes", *SSH_OPTIONS, f"root@{self.a.server_ip}", "true"]
        )

    def run_bootstrap(self) -> bool:
        """Runs ``deadlift server bootstrap`` over whichever route answers: the tailnet once the
        Server has joined it, otherwise the public address."""
        host = self.server_host() if self.tailnet_ready() else self.a.server_ip
        return server_bootstrap.main([host]) == 0

    def join_tailnet(self) -> bool:
        # -t: a terminal, so the link shows as soon as it's printed.
        joined = run(
            [
                "ssh",
                "-t",
                *SSH_OPTIONS,
                f"root@{self.a.server_ip}",
                "tailscale",
                "up",
                "--ssh",
                f"--hostname={self.a.server_name}",
                f"--advertise-tags={SERVER_TAG}",
            ],
            env=maintainer_env(),
            stdin=Stream.INHERIT,
            stdout=Stream.INHERIT,
        )
        return joined.ok

    def until_it_works(self, attempt: Callable[[], bool]) -> None:
        """Runs ``attempt`` until it succeeds, or stops the wizard when the human stops trying."""
        while not attempt():
            self.w.warn("that didn't work")
            if not self.w.confirm("Try again?"):
                raise _stopped()

    def ask(self, key: str, prompt: str) -> str:
        """An answer, as the Bash wizard's ``ask`` under ``set -e``: the end of input stops."""
        answer = self.w.ask(key, prompt)
        if answer is None:
            raise _stopped()
        return answer

    def run(self) -> None:
        w, a = self.w, self.a
        w.env_file.parent.mkdir(parents=True, exist_ok=True)
        a.server_name = w.existing("SERVER_NAME")
        a.server_ip = w.existing("SERVER_IP")
        a.tailnet = w.existing("TAILNET")
        a.policy_saved = w.existing("POLICY_SAVED")
        a.firewall_created = w.existing("FIREWALL_CREATED")

        w.banner("Provision a deadlift Server")
        self.policy()
        self.buy()
        self.firewall()
        self.bootstrap()
        self.join()
        self.close_public_ssh()
        self.end()

    # ── 1 ─────────────────────────────────────────────────────────────────────
    def policy(self) -> None:
        w = self.w
        w.stage("Tailscale: the tailnet policy")
        if self.a.policy_saved:
            w.note(
                "You saved deadlift's entries in the tailnet policy in an earlier run, so there's"
                " nothing to add."
            )
            w.pause("Continue?")
        elif f'"{SERVER_TAG}"' in _captured(["tailscale", "status", "--json"]):
            # This computer's tailnet already has a deadlift Server, whose policy entries every
            # other Server shares.
            w.say(
                "This tailnet already has a deadlift Server, so its policy already holds every"
                " entry a"
            )
            w.say(
                "Server needs. They're set once per tailnet, so there's nothing to add for this"
                " one."
            )
            w.pause("Continue?")
            w.write_env("POLICY_SAVED", "yes")
        else:
            w.say(
                "The policy decides who reaches a Server, before it exists. These entries are"
                " set once"
            )
            w.say(
                "per tailnet: every deadlift Server, and every App's Service, is covered by its"
                " tag."
            )
            w.note(
                f"Servers are tagged {SERVER_TAG}, and each App's Service tag:deadlift-svc."
                " Servers serve"
            )
            w.note(
                "any App's Service without approval by hand, and everyone on the tailnet reaches"
                " every"
            )
            w.note("App at 443 and its Slots' ports. Members SSH to Servers as root.")
            w.open_url("https://login.tailscale.com/admin/acls/file")
            w.step(
                "Merge each section below into the section of the same name, keeping what's there:"
            )
            print()
            print(policy_entries(POLICY.read_text(encoding="utf-8")), end="")
            print(flush=True)
            w.step("Save. (The same text is in cli/tailscale-policy.hujson.)")
            w.pause("Saved?")
            w.write_env("POLICY_SAVED", "yes")

    # ── 2 ─────────────────────────────────────────────────────────────────────
    def buy(self) -> None:
        w, a = self.w, self.a
        w.stage("Hetzner: buy the Server")
        w.say(
            "Any fresh Ubuntu 24.04 machine with a public IPv4 address and root SSH by key will do."
        )
        w.say("These steps buy one at Hetzner Cloud. Bought it already? Give its name and address.")
        w.note("Its name, at Hetzner and on the tailnet: a-z, 0-9 and -, such as deadlift-1.")
        while True:
            a.server_name = self.ask("SERVER_NAME", "The Server's name:")
            if _SERVER_NAME.fullmatch(a.server_name):
                break
            w.warn("that isn't a name: a-z, 0-9 and -, not starting or ending with -")
        w.write_env("SERVER_NAME", a.server_name)
        w.open_url("https://console.hetzner.cloud/")
        w.step("Open your project, then Add Server (or Create Resource > Servers)")
        w.step("Location: any. Image: Ubuntu 24.04")
        w.step(
            "Type: Shared vCPU, x86 (Intel/AMD). CX23 (2 vCPU, 4 GB RAM, 40 GB disk) fits a small"
        )
        w.note("  App or two; CX33 (4 vCPU, 8 GB, 80 GB) a few more")
        w.step("Networking: public IPv4 on. SSH keys: tick this computer's, so root logs in by key")
        w.note(
            "  None there yet? Add Key with ~/.ssh/id_ed25519.pub (ssh-keygen -t ed25519 makes one)"
        )
        w.step(f"Name: {a.server_name}. Then Create & Buy now")
        while True:
            a.server_ip = self.ask("SERVER_IP", "The Server's public IPv4 address:")
            if _IPV4.fullmatch(a.server_ip):
                break
            w.warn("that isn't an IPv4 address, like 203.0.113.7")
        w.write_env("SERVER_IP", a.server_ip)

    # ── 3 ─────────────────────────────────────────────────────────────────────
    def firewall(self) -> None:
        w, a = self.w, self.a
        w.stage("Hetzner: the firewall")
        w.say(
            "Nothing on a Server faces the internet: the provider's firewall blocks every inbound"
        )
        w.say("connection. Tailscale needs none, as it connects out. SSH stays open for now, for")
        w.say("bootstrap and joining the tailnet, and the last stage closes it.")
        if a.firewall_created:
            w.note("You created and applied it in an earlier run.")
            w.pause("Continue?")
        else:
            w.open_url("https://console.hetzner.cloud/")
            w.step(f"In your project: Firewalls > Create Firewall. Name: {a.server_name}")
            w.step("Inbound: delete every rule but SSH (TCP, port 22)")
            w.step("Outbound: leave it empty, which allows everything")
            w.step(f"Apply to: the {a.server_name} server. Then Create Firewall")
            w.pause("Created and applied?")
            w.write_env("FIREWALL_CREATED", "yes")

    # ── 4 ─────────────────────────────────────────────────────────────────────
    def bootstrap(self) -> None:
        w = self.w
        w.stage("Bootstrap the Server")
        w.say(
            "deadlift server bootstrap installs Docker with userns-remap, Tailscale, the"
            " firewall for"
        )
        w.say(
            "containers, the entry command and the deploy script, from this checkout of deadlift."
        )
        w.say("It runs as root over SSH, and is safe to run again.")
        self.until_it_works(self.run_bootstrap)
        w.pause("Bootstrapped? Continue?")

    # ── 5 ─────────────────────────────────────────────────────────────────────
    def join(self) -> None:
        w, a = self.w, self.a
        w.stage("Tailscale: join the tailnet")
        if self.tailnet_ready():
            w.note(f"root@{self.server_host()} already answers over the tailnet.")
        else:
            w.say(
                f"The Server asks to join as {a.server_name}, tagged {SERVER_TAG}, with Tailscale"
                " SSH on."
            )
            w.step(
                "Open the link it prints and sign in as an admin of the tailnet, who owns the tag."
            )
            w.note("It returns once you have.")
            self.until_it_works(self.join_tailnet)
            # This computer's own tailnet, if its tailscale is signed in.
            guess = magic_dns_suffix(_captured(["tailscale", "status", "--json"]))
            w.open_url("https://login.tailscale.com/admin/dns")
            w.step("Copy the Tailnet DNS name, like tail1a2b3c.ts.net")
            if guess:
                w.note(f"This computer's tailnet is {guess}: Enter takes it.")
            answer = w.ask("TAILNET", "Your tailnet DNS name:")
            if answer is not None:
                a.tailnet = answer
            a.tailnet = (a.tailnet or guess).removeprefix(".")
            w.write_env("TAILNET", a.tailnet)
            self.until_it_works(self.tailnet_ready)
        status = _captured(
            ["ssh", *SSH_OPTIONS, f"root@{self.server_host()}", "tailscale", "status", "--json"]
        )
        if f'"{SERVER_TAG}"' in status:
            w.done(f"root@{self.server_host()} answers over Tailscale SSH, tagged {SERVER_TAG}")
        else:
            w.warn(
                f"{self.server_host()} answers, but isn't tagged {SERVER_TAG}. Check it under"
                " Machines."
            )
            w.skipped.append(
                f"tagging {a.server_name} {SERVER_TAG}: Machines > {a.server_name} > Edit ACL tags"
            )
        w.pause("Continue?")

    # ── 6 ─────────────────────────────────────────────────────────────────────
    def close_public_ssh(self) -> None:
        w, a = self.w, self.a
        w.stage("Hetzner: close public SSH")
        w.say(
            f"Everything now goes over the tailnet, as root@{self.server_host()} with Tailscale"
            " SSH, so"
        )
        w.say("the public address needs no inbound connection at all.")
        if self.public_ssh_answers():
            w.open_url("https://console.hetzner.cloud/")
            w.step(f"In your project: Firewalls > {a.server_name} > Rules")
            w.step("Inbound: delete the SSH rule, leaving none. Then Save")
            w.pause("Deleted?")
            while self.public_ssh_answers():
                w.warn(
                    f"SSH on {a.server_ip} still answers. Check the firewall has no inbound rule"
                    f" and is applied to {a.server_name}."
                )
                if not w.confirm("Check again?"):
                    w.skipped.append(
                        f"closing public SSH on {a.server_ip}: delete the firewall's SSH rule"
                    )
                    break
        if not self.public_ssh_answers():
            w.done(f"SSH on {a.server_ip} no longer answers")

    def end(self) -> None:
        w, a = self.w, self.a
        w.finish()
        w.say(f"{a.server_name} is a Server, at root@{self.server_host()}. Next, give it an App:")
        w.say(f"  deadlift app add <app> --server {a.server_name} --repo <owner/name>")
        w.note(
            "Bring it up to date with this checkout any time: deadlift server bootstrap"
            f" {self.server_host()}"
        )
        w.note(
            "On Linux, a device with Tailscale 1.93 or earlier needs --accept-routes to reach an"
            " App."
        )
        if not w.skipped:
            w.env_file.unlink(missing_ok=True)
            w.note("This run's answers are forgotten, so the next run provisions a new Server.")
        print(flush=True)


def main(argv: Sequence[str]) -> int:
    del argv  # it asks for what it needs
    return run_command(lambda: Provision().run())
