// A fake `gh` CLI, standing in for GitHub's REST API in tests of deadlift's actions and starter
// workflows. Only `gh api` is supported, for the endpoints they use. It reads and writes the GitHub state
// in $FAKE_GITHUB_STATE (see fake-github.ts), so a test sees what the action changed.
//
// Behaves like the real `gh api` where the actions depend on it: it needs GH_TOKEN, pages with
// `per_page` and follows pages only with --paginate, applies --jq to each page, prints an error
// and exits 1 on an HTTP error.
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import type { CheckRun, GitHubState, PackageVersion } from "./fake-github.ts";

class HttpError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const statePath = process.env.FAKE_GITHUB_STATE;
if (!statePath) {
  process.stderr.write("fake gh: FAKE_GITHUB_STATE isn't set\n");
  process.exit(3);
}
const state: GitHubState = JSON.parse(readFileSync(statePath, "utf8"));

function main(argv: string[]): number {
  if (argv[0] !== "api") {
    process.stderr.write(`fake gh: only \`gh api\` is faked, not \`gh ${argv.join(" ")}\`\n`);
    return 3;
  }
  if (!process.env.GH_TOKEN && !process.env.GITHUB_TOKEN) {
    process.stderr.write(
      "gh: To use GitHub CLI in a GitHub Actions workflow, set the GH_TOKEN environment variable.\n",
    );
    return 4;
  }
  let method = "GET";
  let paginate = false;
  let jq: string | undefined;
  let path: string | undefined;
  const fields: Record<string, unknown> = {};
  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--method" || arg === "-X") method = argv[++i].toUpperCase();
    else if (arg === "-f" || arg === "--raw-field" || arg === "-F" || arg === "--field") {
      const field = argv[++i];
      const name = field.slice(0, field.indexOf("="));
      let value: unknown = field.slice(field.indexOf("=") + 1);
      // As gh does: -F reads true, false and numbers as JSON; -f keeps every value a string.
      if ((arg === "-F" || arg === "--field") && /^(true|false|-?\d+)$/.test(value as string)) {
        value = JSON.parse(value as string);
      }
      setField(fields, name, value);
    } else if (arg === "--paginate") paginate = true;
    else if (arg === "--jq" || arg === "-q") jq = argv[++i];
    else if (arg === "-H" || arg === "--header") i++;
    else if (arg.startsWith("-")) {
      process.stderr.write(`fake gh: unknown flag ${arg}\n`);
      return 3;
    } else if (path === undefined) path = arg;
    else {
      process.stderr.write(`fake gh: unexpected argument ${arg}\n`);
      return 3;
    }
  }
  if (path === undefined) {
    process.stderr.write("fake gh: no endpoint given\n");
    return 3;
  }
  const [route, query = ""] = path.replace(/^\//, "").split("?");
  const perPage = Math.min(
    Number(new URLSearchParams(query).get("per_page") ?? 30),
    state.maxPerPage ?? 100,
  );

  const failure = state.failures?.find(
    (f) => f.method === method && route.startsWith(f.pathPrefix),
  );
  try {
    if (failure) throw new HttpError(failure.status ?? 500, "Server Error");
    const body = handle(method, route, new URLSearchParams(query), fields);
    writeFileSync(statePath!, JSON.stringify(state, null, 2));
    if (body === undefined) return 0;
    const pages = Array.isArray(body) ? chunk(body, perPage) : [body];
    for (const page of paginate ? pages : pages.slice(0, 1)) {
      process.stdout.write(jq === undefined ? JSON.stringify(page) + "\n" : runJq(jq, page));
    }
    return 0;
  } catch (error) {
    if (!(error instanceof HttpError)) throw error;
    process.stdout.write(JSON.stringify({ message: error.message }) + "\n");
    process.stderr.write(`gh: ${error.message} (HTTP ${error.status})\n`);
    return 1;
  }
}

function handle(
  method: string,
  route: string,
  query: URLSearchParams,
  fields: Record<string, unknown>,
): unknown {
  let m: RegExpMatchArray | null;
  if (method === "GET" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/pulls$/))) {
    const state = query.get("state") ?? "open";
    return (repoOf(m[1]).pulls ?? []).filter((pr) => state === "all" || pr.state === state);
  }
  if (method === "GET" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/pulls\/(\d+)$/))) {
    const pr = (repoOf(m[1]).pulls ?? []).find((p) => p.number === Number(m![2]));
    if (!pr) throw new HttpError(404, "Not Found");
    return pr;
  }
  if ((m = route.match(/^repos\/([^/]+\/[^/]+)\/issues\/(\d+)\/comments$/))) {
    const repo = repoOf(m[1]);
    repo.comments ??= [];
    const issue = Number(m[2]);
    if (method === "GET") return repo.comments.filter((c) => c.issue === issue);
    if (method === "POST") {
      if (typeof fields.body !== "string") throw new HttpError(422, "body is required");
      const comment = { id: 1000 + repo.comments.length, issue, body: fields.body };
      repo.comments.push(comment);
      return comment;
    }
  }
  if (method === "PATCH" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/issues\/comments\/(\d+)$/))) {
    const comment = (repoOf(m[1]).comments ?? []).find((c) => c.id === Number(m![2]));
    if (!comment) throw new HttpError(404, "Not Found");
    if (typeof fields.body !== "string") throw new HttpError(422, "body is required");
    comment.body = fields.body;
    return comment;
  }
  if (method === "POST" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/check-runs$/))) {
    const repo = repoOf(m[1]);
    if (typeof fields.name !== "string" || typeof fields.head_sha !== "string") {
      throw new HttpError(422, "name and head_sha are required");
    }
    (repo.checkRuns ??= []).push(fields as unknown as CheckRun);
    return fields;
  }
  // The branch's history, newest first, following first parents as `git log --first-parent`.
  if (method === "GET" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/commits$/))) {
    const repo = repoOf(m[1]);
    let sha: string | undefined = repo.branches[query.get("sha") ?? ""] ?? query.get("sha") ?? undefined;
    if (!sha || !(sha in repo.commits)) throw new HttpError(404, "No commit found for SHA");
    const history = [];
    for (; sha && history.length <= Object.keys(repo.commits).length; sha = repo.commits[sha]?.[0]) {
      history.push({ sha });
    }
    return history;
  }
  if (method === "GET" && (m = route.match(/^repos\/([^/]+\/[^/]+)$/))) {
    const repo = repoOf(m[1]);
    return { full_name: m[1], owner: { login: m[1].split("/")[0], type: repo.ownerType } };
  }
  if (method === "GET" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/git\/ref\/heads\/(.+)$/))) {
    const sha = repoOf(m[1]).branches[m[2]];
    if (!sha) throw new HttpError(404, "Not Found");
    return { ref: `refs/heads/${m[2]}`, object: { type: "commit", sha } };
  }
  if (method === "GET" && (m = route.match(/^repos\/([^/]+\/[^/]+)\/compare\/([^.]+)\.\.\.([^.]+)$/))) {
    return compare(repoOf(m[1]).commits, m[2], m[3]);
  }
  m = route.match(/^(users|orgs)\/([^/]+)\/packages\/container\/([^/]+)\/versions(?:\/(\d+))?$/);
  if (m) {
    const [, kind, owner, name, id] = m;
    // As in GitHub's API, a package name's slashes must be encoded (shop%2Fweb).
    const pkg = lookup(state.packages, owner)?.[decodeURIComponent(name)];
    const ownerKind = lookup(state.owners, owner) === "Organization" ? "orgs" : "users";
    if (!pkg || kind !== ownerKind) throw new HttpError(404, "Package not found.");
    if (method === "GET" && id === undefined) {
      return [...pkg].sort((a, b) => b.created_at.localeCompare(a.created_at));
    }
    if (method === "DELETE" && id !== undefined) {
      const index = pkg.findIndex((v: PackageVersion) => v.id === Number(id));
      if (index < 0) throw new HttpError(404, "Not Found");
      pkg.splice(index, 1);
      return undefined;
    }
  }
  throw new HttpError(404, `Not Found: fake gh doesn't know ${method} ${route}`);
}

function repoOf(fullName: string) {
  const repo = lookup(state.repos, fullName);
  if (!repo) throw new HttpError(404, "Not Found");
  return repo;
}

// GitHub's compare: how `head` relates to `base` in the commit graph.
function compare(commits: Record<string, string[]>, base: string, head: string) {
  const resolve = (ref: string) => Object.keys(commits).find((sha) => sha.startsWith(ref));
  const b = resolve(base);
  const h = resolve(head);
  if (!b || !h) throw new HttpError(404, "No common ancestor between these commits.");
  const ancestors = (sha: string) => {
    const seen = new Set<string>();
    const todo = [sha];
    while (todo.length) {
      const next = todo.pop()!;
      if (seen.has(next)) continue;
      seen.add(next);
      todo.push(...(commits[next] ?? []));
    }
    return seen;
  };
  const ofHead = ancestors(h);
  const ofBase = ancestors(b);
  const status =
    b === h ? "identical" : ofHead.has(b) ? "ahead" : ofBase.has(h) ? "behind" : "diverged";
  return {
    status,
    ahead_by: [...ofHead].filter((sha) => !ofBase.has(sha)).length,
    behind_by: [...ofBase].filter((sha) => !ofHead.has(sha)).length,
  };
}

// GitHub's owner and repository names are case-insensitive.
function lookup<T>(map: Record<string, T> | undefined, name: string): T | undefined {
  const key = Object.keys(map ?? {}).find((k) => k.toLowerCase() === name.toLowerCase());
  return key === undefined ? undefined : map![key];
}

// gh's nested field names: output[title]=… sets {output: {title: …}}.
function setField(fields: Record<string, unknown>, name: string, value: unknown) {
  const [first, ...rest] = name.split("[").map((part) => part.replace(/\]$/, ""));
  let target = fields;
  for (const key of [first, ...rest].slice(0, -1)) {
    target = (target[key] ??= {}) as Record<string, unknown>;
  }
  target[[first, ...rest].at(-1)!] = value;
}

function chunk<T>(items: T[], size: number): T[][] {
  const pages: T[][] = [];
  for (let i = 0; i < items.length; i += size) pages.push(items.slice(i, i + size));
  return pages.length ? pages : [[]];
}

function runJq(filter: string, input: unknown): string {
  return execFileSync("jq", ["-r", filter], { input: JSON.stringify(input), encoding: "utf8" });
}

process.exit(main(process.argv.slice(2)));
