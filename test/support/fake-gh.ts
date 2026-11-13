// A fake `gh` CLI, standing in for GitHub's REST API in tests of deadlift's actions. Only
// `gh api` is supported, for the endpoints the actions use. It reads and writes the GitHub state
// in $FAKE_GITHUB_STATE (see fake-github.ts), so a test sees what the action changed.
//
// Behaves like the real `gh api` where the actions depend on it: it needs GH_TOKEN, pages with
// `per_page` and follows pages only with --paginate, applies --jq to each page, prints an error
// and exits 1 on an HTTP error.
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import type { GitHubState, PackageVersion } from "./fake-github.ts";

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
  for (let i = 1; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--method" || arg === "-X") method = argv[++i].toUpperCase();
    else if (arg === "--paginate") paginate = true;
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
  const perPage = Number(new URLSearchParams(query).get("per_page") ?? 30);

  const failure = state.failures?.find(
    (f) => f.method === method && route.startsWith(f.pathPrefix),
  );
  try {
    if (failure) throw new HttpError(failure.status ?? 500, "Server Error");
    const body = handle(method, decodeURIComponent(route));
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

function handle(method: string, route: string): unknown {
  let m: RegExpMatchArray | null;
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
    const pkg = state.packages?.[owner]?.[name];
    const ownerKind = state.owners?.[owner] === "Organization" ? "orgs" : "users";
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
  const repo = state.repos?.[fullName];
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

function chunk<T>(items: T[], size: number): T[][] {
  const pages: T[][] = [];
  for (let i = 0; i < items.length; i += size) pages.push(items.slice(i, i + size));
  return pages.length ? pages : [[]];
}

function runJq(filter: string, input: unknown): string {
  return execFileSync("jq", ["-r", filter], { input: JSON.stringify(input), encoding: "utf8" });
}

process.exit(main(process.argv.slice(2)));
