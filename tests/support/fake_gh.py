"""A fake `gh` CLI, standing in for GitHub's REST API in tests of deadlift's actions and starter
workflows. Only `gh api` is supported, for the endpoints they use. It reads and writes the GitHub
state in $FAKE_GITHUB_STATE (see fake_github.py), so a test sees what the action changed.

Behaves like the real `gh api` where the actions depend on it: it needs GH_TOKEN, pages with
`per_page` and follows pages only with --paginate, applies --jq to each page (with jq), prints an
error and exits 1 on an HTTP error.

Run as a program (fake_github.py puts a `gh` that runs it on PATH), so it imports only the
standard library.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

type Json = Any
type Query = dict[str, str]


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def main(argv: list[str], state: dict[str, Json], state_path: Path) -> int:
    if not argv or argv[0] != "api":
        sys.stderr.write(f"fake gh: only `gh api` is faked, not `gh {' '.join(argv)}`\n")
        return 3
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        sys.stderr.write(
            "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment"
            " variable.\n"
        )
        return 4
    method = "GET"
    paginate = False
    jq: str | None = None
    path: str | None = None
    fields: dict[str, Json] = {}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("--method", "-X"):
            i += 1
            method = argv[i].upper()
        elif arg in ("-f", "--raw-field", "-F", "--field"):
            i += 1
            name, _, raw = argv[i].partition("=")
            value: Json = raw
            # As gh does: -F reads true, false and numbers as JSON; -f keeps every value a string.
            if arg in ("-F", "--field") and re.fullmatch(r"true|false|-?\d+", raw):
                value = json.loads(raw)
            set_field(fields, name, value)
        elif arg == "--paginate":
            paginate = True
        elif arg in ("--jq", "-q"):
            i += 1
            jq = argv[i]
        elif arg in ("-H", "--header"):
            i += 1
        elif arg.startswith("-"):
            sys.stderr.write(f"fake gh: unknown flag {arg}\n")
            return 3
        elif path is None:
            path = arg
        else:
            sys.stderr.write(f"fake gh: unexpected argument {arg}\n")
            return 3
        i += 1
    if path is None:
        sys.stderr.write("fake gh: no endpoint given\n")
        return 3
    route, _, query_string = path.removeprefix("/").partition("?")
    query = {k: v[0] for k, v in parse_qs(query_string).items()}
    max_per_page = state.get("maxPerPage")
    per_page = min(int(query.get("per_page", 30)), 100 if max_per_page is None else max_per_page)

    failure = next(
        (
            f
            for f in listed(state.get("failures"))
            if f["method"] == method and route.startswith(f["pathPrefix"])
        ),
        None,
    )
    try:
        if failure is not None:
            status = failure.get("status")
            raise HttpError(500 if status is None else status, "Server Error")
        body = handle(state, method, route, query, fields)
        state_path.write_text(json.dumps(state, indent=2))
        if body is None:
            return 0
        pages: list[Json] = chunk(listed(body), per_page) if isinstance(body, list) else [body]
        for page in pages if paginate else pages[:1]:
            sys.stdout.write(compact(page) + "\n" if jq is None else run_jq(jq, page))
        return 0
    except HttpError as error:
        sys.stdout.write(compact({"message": error.message}) + "\n")
        sys.stderr.write(f"gh: {error.message} (HTTP {error.status})\n")
        return 1


def handle(state: dict[str, Json], method: str, route: str, query: Query, fields: Json) -> Json:
    if method == "GET" and (m := re.fullmatch(r"repos/([^/]+/[^/]+)/pulls", route)):
        wanted = query.get("state", "open")
        pulls = listed(repo_of(state, m[1]).get("pulls"))
        return [pr for pr in pulls if wanted == "all" or pr["state"] == wanted]
    if method == "GET" and (m := re.fullmatch(r"repos/([^/]+/[^/]+)/pulls/(\d+)", route)):
        number = int(m[2])
        pr = next(
            (p for p in listed(repo_of(state, m[1]).get("pulls")) if p["number"] == number), None
        )
        if pr is None:
            raise HttpError(404, "Not Found")
        return pr
    if m := re.fullmatch(r"repos/([^/]+/[^/]+)/issues/(\d+)/comments", route):
        repo = repo_of(state, m[1])
        comments: list[Json] = repo.setdefault("comments", [])
        issue = int(m[2])
        if method == "GET":
            return [c for c in comments if c["issue"] == issue]
        if method == "POST":
            if not isinstance(fields.get("body"), str):
                raise HttpError(422, "body is required")
            comment = {"id": 1000 + len(comments), "issue": issue, "body": fields["body"]}
            comments.append(comment)
            return comment
    if method == "PATCH" and (
        m := re.fullmatch(r"repos/([^/]+/[^/]+)/issues/comments/(\d+)", route)
    ):
        number = int(m[2])
        comments = listed(repo_of(state, m[1]).get("comments"))
        comment = next((c for c in comments if c["id"] == number), None)
        if comment is None:
            raise HttpError(404, "Not Found")
        if not isinstance(fields.get("body"), str):
            raise HttpError(422, "body is required")
        comment["body"] = fields["body"]
        return comment
    if method == "POST" and (m := re.fullmatch(r"repos/([^/]+/[^/]+)/check-runs", route)):
        repo = repo_of(state, m[1])
        if not isinstance(fields.get("name"), str) or not isinstance(fields.get("head_sha"), str):
            raise HttpError(422, "name and head_sha are required")
        repo.setdefault("checkRuns", []).append(fields)
        return fields
    # The branch's history, newest first, following first parents as `git log --first-parent`.
    if method == "GET" and (m := re.fullmatch(r"repos/([^/]+/[^/]+)/commits", route)):
        repo = repo_of(state, m[1])
        commits: dict[str, list[str]] = repo["commits"]
        given = query.get("sha")
        sha: str | None = repo["branches"].get(given or "", given)
        if not sha or sha not in commits:
            raise HttpError(404, "No commit found for SHA")
        history: list[Json] = []
        while sha and len(history) <= len(commits):
            history.append({"sha": sha})
            parents = commits.get(sha) or []
            sha = parents[0] if parents else None
        return history
    if method == "GET" and (m := re.fullmatch(r"repos/([^/]+/[^/]+)", route)):
        repo = repo_of(state, m[1])
        return {
            "full_name": m[1],
            "owner": {"login": m[1].split("/")[0], "type": repo["ownerType"]},
        }
    if method == "GET" and (m := re.fullmatch(r"repos/([^/]+/[^/]+)/git/ref/heads/(.+)", route)):
        sha = repo_of(state, m[1])["branches"].get(m[2])
        if not sha:
            raise HttpError(404, "Not Found")
        return {"ref": f"refs/heads/{m[2]}", "object": {"type": "commit", "sha": sha}}
    if method == "GET" and (
        m := re.fullmatch(r"repos/([^/]+/[^/]+)/compare/([^.]+)\.\.\.([^.]+)", route)
    ):
        return compare(repo_of(state, m[1])["commits"], m[2], m[3])
    m = re.fullmatch(r"(users|orgs)/([^/]+)/packages/container/([^/]+)/versions(?:/(\d+))?", route)
    if m:
        kind, owner, name, version_id = m.groups()
        # As in GitHub's API, a package name's slashes must be encoded (shop%2Fweb).
        packages: dict[str, Json] = lookup(state.get("packages"), owner) or {}
        pkg: list[Json] | None = packages.get(unquote(name))
        owner_kind = "orgs" if lookup(state.get("owners"), owner) == "Organization" else "users"
        if pkg is None or kind != owner_kind:
            raise HttpError(404, "Package not found.")
        if method == "GET" and version_id is None:
            return sorted(pkg, key=lambda v: v["created_at"], reverse=True)
        if method == "DELETE" and version_id is not None:
            index = next((i for i, v in enumerate(pkg) if v["id"] == int(version_id)), None)
            if index is None:
                raise HttpError(404, "Not Found")
            del pkg[index]
            return None
    raise HttpError(404, f"Not Found: fake gh doesn't know {method} {route}")


def repo_of(state: dict[str, Json], full_name: str) -> Json:
    repo = lookup(state.get("repos"), full_name)
    if repo is None:
        raise HttpError(404, "Not Found")
    return repo


def compare(commits: dict[str, list[str]], base: str, head: str) -> Json:
    """GitHub's compare: how `head` relates to `base` in the commit graph."""

    def resolve(ref: str) -> str | None:
        return next((sha for sha in commits if sha.startswith(ref)), None)

    b = resolve(base)
    h = resolve(head)
    if not b or not h:
        raise HttpError(404, "No common ancestor between these commits.")

    def ancestors(sha: str) -> set[str]:
        seen: set[str] = set()
        todo = [sha]
        while todo:
            following = todo.pop()
            if following in seen:
                continue
            seen.add(following)
            todo.extend(commits.get(following) or [])
        return seen

    of_head = ancestors(h)
    of_base = ancestors(b)
    status = (
        "identical"
        if b == h
        else "ahead"
        if b in of_head
        else "behind"
        if h in of_base
        else "diverged"
    )
    return {
        "status": status,
        "ahead_by": len(of_head - of_base),
        "behind_by": len(of_base - of_head),
    }


def listed(value: Json) -> list[Json]:
    """A list from the state, or an empty one where the state has none."""
    return value or []


def lookup(table: dict[str, Json] | None, name: str) -> Json:
    """GitHub's owner and repository names are case-insensitive."""
    for key, value in (table or {}).items():
        if key.lower() == name.lower():
            return value
    return None


def set_field(fields: dict[str, Json], name: str, value: Json) -> None:
    """gh's nested field names: output[title]=… sets {output: {title: …}}."""
    keys = [part.removesuffix("]") for part in name.split("[")]
    target = fields
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value


def chunk(items: list[Json], size: int) -> list[list[Json]]:
    pages = [items[i : i + size] for i in range(0, len(items), size)]
    return pages or [[]]


def compact(value: Json) -> str:
    """As JavaScript's JSON.stringify."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def run_jq(jq_filter: str, page: Json) -> str:
    # jq's errors go to our stderr, and fail this fake.
    return subprocess.run(
        ["jq", "-r", jq_filter],
        input=compact(page),
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout


if __name__ == "__main__":
    state_file = os.environ.get("FAKE_GITHUB_STATE")
    if not state_file:
        sys.stderr.write("fake gh: FAKE_GITHUB_STATE isn't set\n")
        sys.exit(3)
    sys.exit(main(sys.argv[1:], json.loads(Path(state_file).read_text()), Path(state_file)))
