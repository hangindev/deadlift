"""``deadlift app add|secrets|remove`` and ``deadlift server status``: the maintainer's half of an
App's place on a Server. The Server's half is its app, installed by bootstrap, which these run
over ``ssh root@<server>``.

app add and app remove reach the Tailscale API with the maintainer's own credential, an API
access token in TAILSCALE_API_KEY, for the tailnet in TAILSCALE_TAILNET (the token's own by
default). The Server never holds a Tailscale credential (ADR 0003).

Settings, for the tests only: DEADLIFT_TAILSCALE_API, the Tailscale API's address.
"""

import json
import os
import re
import shlex
import ssl
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from deadlift.cli.common import (
    Exited,
    is_server_name,
    maintainer_env,
    usage,
)
from deadlift.core.names import app_name_problem
from deadlift.core.outcome import OUTPUT_PREFIX, fail, say, tell
from deadlift.core.process import NOT_FOUND, Stream, Target, run

# Installed by bootstrap: the Server half of the app and server status commands.
APP_SCRIPT = "/usr/local/lib/deadlift/app"
SSH_UNREACHABLE = 255

_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_SLOTS = re.compile(r"[1-9]")
_SERVER_OPTIONS = ("--slots", "--snapshots", "--memory", "--cpus", "--pids")


def check_server(server: str) -> None:
    if not is_server_name(server):
        usage(f"'{server}' isn't a host name or address")


@dataclass
class AppArgs:
    """``<app> [positional...] --server <server> [options]``: the other ``--name value`` pairs,
    and ``--yes``, are in ``options``, in order."""

    app: str
    server: str
    positional: list[str] = field(default_factory=list[str])
    options: list[str] = field(default_factory=list[str])


def parse_app_args(argv: Sequence[str]) -> AppArgs:
    """Parses an app command's arguments, refusing a bad App name or Server."""
    app = argv[0] if argv else ""
    rest = list(argv[1:])
    server = ""
    positional: list[str] = []
    options: list[str] = []
    while rest:
        arg = rest[0]
        if arg == "--server":
            if len(rest) < 2 or not rest[1]:
                usage("--server needs a value")
            server = rest[1]
            del rest[:2]
        elif arg == "--yes":
            options.append(arg)
            del rest[0]
        elif arg.startswith("--"):
            if len(rest) < 2:
                usage(f"{arg} needs a value")
            options += rest[:2]
            del rest[:2]
        else:
            positional.append(arg)
            del rest[0]
    problem = app_name_problem(app)
    if problem is not None:
        usage(problem)
    if not server:
        usage("--server is required")
    check_server(server)
    return AppArgs(app, server, positional, options)


def on_server(server: str, args: Sequence[str], *, stdin: Target = Stream.DEVNULL) -> list[str]:
    """Runs the installed app on the Server as root, with ``stdin`` passed on. Its stderr is
    shown as it comes, its stdout once it's done, except for its ``deadlift-output:`` lines, which
    it returns. Stops, as app does, when it fails."""
    remote = f"{APP_SCRIPT} " + "".join(f"{shlex.quote(arg)} " for arg in args)
    done = run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", f"root@{server}", remote],
        env=maintainer_env(),
        stdin=stdin,
        stdout=Stream.CAPTURE,
    )
    outputs: list[str] = []
    for line in _lines(done.stdout):
        if line.startswith(OUTPUT_PREFIX):
            outputs.append(line)
        else:
            say(line)
    status = done.code
    if status == SSH_UNREACHABLE:
        fail(f"couldn't reach {server} over SSH as root, so nothing was changed.")
    if status == NOT_FOUND:
        fail(
            f"{server} has no {APP_SCRIPT}. Run `deadlift server bootstrap {server}` to bring it"
            " up to date, then run this again."
        )
    if status != 0:
        raise Exited(status)
    return outputs


def _lines(text: str) -> list[str]:
    # Lines as grep reads them: split at line feeds only, a last one unended or not.
    lines = text.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


@dataclass(frozen=True)
class ApiAnswer:
    """The HTTP status (0 when the API couldn't be reached) and body of a Tailscale API call."""

    status: int
    body: str

    def shown(self) -> str:
        """As a reason line quotes it: ``HTTP <status>: <body>``, curl's 000 when unreached."""
        return f"HTTP {self.status:03d}: {self.body}"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Takes a redirect as the answer, as curl does without --location."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def tailscale_api(method: str, path: str, body: object = None) -> ApiAnswer:
    """A Tailscale API call: ``method`` on ``path`` under the tailnet, with ``body`` as JSON."""
    api = os.environ.get("DEADLIFT_TAILSCALE_API") or "https://api.tailscale.com"
    tailnet = os.environ.get("TAILSCALE_TAILNET") or "-"
    headers = {"Authorization": f"Bearer {os.environ.get('TAILSCALE_API_KEY', '')}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 (https, or the tests' own address)
        f"{api}/api/v2/tailnet/{tailnet}/{path}", data=data, headers=headers, method=method
    )
    try:
        with _OPENER.open(request) as response:
            return ApiAnswer(response.status, _text(response.read()))
    except urllib.error.HTTPError as error:
        return ApiAnswer(error.code, _text(error.read()))
    except (urllib.error.URLError, OSError, ValueError) as error:
        why = error.reason if isinstance(error, urllib.error.URLError) else error
        tell(f"{api}: {why}")
        if isinstance(why, ssl.SSLCertVerificationError):
            tell(
                "The Python running deadlift may have no CA certificates. A python.org Python"
                " on macOS gets them from its Install Certificates.command, in its folder in"
                " /Applications."
            )
        return ApiAnswer(0, "")


def _text(body: bytes) -> str:
    # As Bash's $(cat file): trailing line breaks dropped.
    return body.decode("utf-8", errors="replace").rstrip("\n")


def require_api_key(command: str) -> None:
    if not os.environ.get("TAILSCALE_API_KEY"):
        usage(
            f"{command} needs a Tailscale API access token in TAILSCALE_API_KEY, from the admin"
            " console's Settings, Keys. Nothing was changed"
        )


SERVICE_TAGS = ["tag:deadlift-svc"]


def service_ports(slots: int) -> list[str]:
    """The App's Service's ports: 443, for Dev, and each Slot's port, 10000 + <slot>."""
    return ["tcp:443", *(f"tcp:{10000 + slot}" for slot in range(1, slots + 1))]


def service_matches(body: str, ports: list[str]) -> bool:
    """Whether the Service the API answered with lists ``ports`` and deadlift's tag already, in
    any order."""
    try:
        found: object = json.loads(body)
    except ValueError:
        return False
    if not isinstance(found, dict):
        return False
    service = cast(dict[str, object], found)
    found_ports, found_tags = service.get("ports"), service.get("tags")
    if not isinstance(found_ports, list) or not isinstance(found_tags, list):
        return False
    try:
        return (
            sorted(cast(list[str], found_ports)) == sorted(ports)
            and sorted(cast(list[str], found_tags)) == SERVICE_TAGS
        )
    except TypeError:  # not all strings
        return False


def ensure_service(app: str, slots: int) -> None:
    """Creates the App's Service, or brings its ports and tags up to date. Leaves it alone when
    it's right already. Says what it did on stderr."""
    service = f"svc:{app}"
    ports = service_ports(slots)
    wanted = {
        "name": service,
        "comment": f"deadlift: the App {app}",
        "ports": ports,
        "tags": SERVICE_TAGS,
    }
    found = tailscale_api("GET", f"vip-services/{service}")
    if found.status == 200 and service_matches(found.body, ports):
        tell(
            f"deadlift: the Tailscale Service {service} is there already, listing 443 and the ports"
            f" of {app}'s {slots} Slots."
        )
        return
    if found.status not in (200, 404):
        fail(
            f"couldn't read the Tailscale Service {service} from the Tailscale API"
            f" ({found.shown()}). {app}'s place on the Server is ready; run `deadlift app add`"
            " again to create its Service"
        )
    put = tailscale_api("PUT", f"vip-services/{service}", wanted)
    if put.status != 200:
        fail(
            f"couldn't create the Tailscale Service {service} through the Tailscale API"
            f" ({put.shown()}). {app}'s place on the Server is ready; run `deadlift app add` again"
            " to create its Service"
        )
    tell(
        f"deadlift: created the Tailscale Service {service}, tagged tag:deadlift-svc, listing 443"
        f" and the ports of {app}'s {slots} Slots."
    )


def app_policy(app: str) -> dict[str, object]:
    """The App's own entries in the tailnet policy."""
    tag = f"tag:deadlift-ci-{app}"
    return {
        "tagOwners": {tag: ["autogroup:admin"]},
        "grants": [{"src": [tag], "dst": ["tag:deadlift-server"], "ip": ["tcp:22"]}],
        "ssh": [
            {
                "action": "accept",
                "src": [tag],
                "dst": ["tag:deadlift-server"],
                "users": [f"deadlift-{app}"],
            }
        ],
    }


def policy_text(app: str, repo: str) -> str:
    """The App's own entries in the tailnet policy, and its CI's federated credential."""
    tag = f"tag:deadlift-ci-{app}"
    entries = json.dumps(app_policy(app), indent=2)
    return f"""
Merge these entries into your tailnet policy. They let {app}'s CI, as {tag}, reach only the
Server's SSH port, and log in only as {app}'s deploy user. Its Service, svc:{app}, needs none: the
provision wizard's entries cover every Service tagged tag:deadlift-svc.

{entries}

Then give {app}'s CI a federated credential, so it joins the tailnet through GitHub's OIDC token
with no key to leak. In the Tailscale admin console, Settings, Trust credentials, add an OpenID
Connect credential:
  Issuer:    GitHub (https://token.actions.githubusercontent.com)
  Subject:   repo:{repo}:*
             (if the repo customises its subject claim, use that form, from
             `gh api repos/{repo}/actions/oidc/customization/sub`)
  Scopes:    Keys, Auth Keys: Write, with the tag {tag}
and set its Client ID and Audience as the repo's variables, which deadlift's deliver action reads:
  gh variable set TS_OAUTH_CLIENT_ID --repo {repo} --body <Client ID>
  gh variable set TS_AUDIENCE --repo {repo} --body <Audience>
"""


def server_status(argv: Sequence[str]) -> None:
    if len(argv) != 1:
        usage("server status needs one <server>")
    check_server(argv[0])
    on_server(argv[0], ["status"])


def app_add(argv: Sequence[str]) -> None:
    args = parse_app_args(argv)
    app, server = args.app, args.server
    if args.positional:
        usage(f"unknown argument '{args.positional[0]}' for app add")
    repo = ""
    server_args: list[str] = []
    options = args.options
    while options:
        name = options[0]
        if name == "--repo":
            repo = options[1]
        elif name in _SERVER_OPTIONS:
            server_args += options[:2]
        else:
            usage(f"unknown option {name} for app add")
        options = options[2:]
    if not _REPO.fullmatch(repo):
        usage("--repo needs the App's GitHub repository, as <owner>/<name>")
    require_api_key("app add")

    say(f"Adding {app} to {server}")
    outputs = on_server(server, ["add", app, *server_args])
    slots = "\n".join(
        line.removeprefix(f"{OUTPUT_PREFIX}slots=")
        for line in outputs
        if line.startswith(f"{OUTPUT_PREFIX}slots=")
    )
    if not _SLOTS.fullmatch(slots):
        fail(
            f"{server} didn't say how many Slots {app} has. Run `deadlift server bootstrap"
            f" {server}`, then this again"
        )
    ensure_service(app, int(slots))
    say(policy_text(app, repo), end="")


def app_secrets(argv: Sequence[str]) -> None:
    args = parse_app_args(argv)
    if args.options:
        usage(f"unknown option {args.options[0]} for app secrets")
    if len(args.positional) != 2:
        usage("app secrets needs dev or feature, and a file")
    kind, file = args.positional
    if kind not in ("dev", "feature"):
        usage(f"app secrets is for dev or feature, not '{kind}'")
    path = Path(file)
    if not path.is_file() or not os.access(path, os.R_OK):
        usage(f"there's no file to read at {file}")
    with path.open("rb") as secrets:
        on_server(args.server, ["secrets", args.app, kind], stdin=secrets)


def app_remove(argv: Sequence[str]) -> None:
    args = parse_app_args(argv)
    app, server = args.app, args.server
    if args.positional:
        usage(f"unknown argument '{args.positional[0]}' for app remove")
    if " ".join(args.options) != "--yes":
        usage(
            f"app remove destroys every Environment of {app} with its data, for good. Run it again"
            " with --yes to go ahead. Nothing was changed"
        )
    require_api_key("app remove")

    say(f"Removing {app} from {server}")
    on_server(server, ["remove", app])
    deleted = tailscale_api("DELETE", f"vip-services/svc:{app}")
    if deleted.status in (200, 204):
        tell(f"deadlift: deleted the Tailscale Service svc:{app}.")
    elif deleted.status == 404:
        tell(f"deadlift: there's no Tailscale Service svc:{app}, so there was none to delete.")
    else:
        fail(
            f"{app} is off {server}, but its Tailscale Service svc:{app} couldn't be deleted"
            f" ({deleted.shown()}). Run `deadlift app remove` again"
        )
