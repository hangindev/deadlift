// A fake GitHub for testing deadlift's actions: a `gh` on PATH (fake-gh.ts) backed by a JSON
// state file. Tests set up the state, run an action's script, then read the state back to see
// what the action did to GitHub. No network.
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { type ActionRun, parseOutputFile, runAction } from "./composite-action.ts";

export interface PackageVersion {
  id: number;
  /** The manifest's digest, as GHCR names a version. */
  name: string;
  created_at: string;
  metadata: { package_type: "container"; container: { tags: string[] } };
}

export interface PullRequest {
  number: number;
  state: "open" | "closed";
  head: { sha: string; repo: { full_name: string } };
  labels: { name: string }[];
}

export interface IssueComment {
  id: number;
  /** The issue or pull request it's on. */
  issue: number;
  body: string;
}

export interface CheckRun {
  name: string;
  head_sha: string;
  status: string;
  conclusion?: string;
  details_url?: string;
  output?: { title?: string; summary?: string };
}

export interface GitHubState {
  repos?: Record<
    string,
    {
      ownerType: "User" | "Organization";
      /** Branch name to the commit it points at. */
      branches: Record<string, string>;
      /** Commit to its parents, the first parent first. */
      commits: Record<string, string[]>;
      pulls?: PullRequest[];
      /** Comments on its issues and pull requests. */
      comments?: IssueComment[];
      /** Check runs created on its commits, in order. */
      checkRuns?: CheckRun[];
    }
  >;
  /** Owner login to its type, which decides whether its packages are under users/ or orgs/. */
  owners?: Record<string, "User" | "Organization">;
  /** Owner login to package name (as GHCR names it, e.g. `shop/web`) to its versions. */
  packages?: Record<string, Record<string, PackageVersion[]>>;
  /** The most items GitHub returns per page, 100 by default; set it lower to test paging. */
  maxPerPage?: number;
  /** Requests that fail, matched by method and the start of the path. */
  failures?: { method: string; pathPrefix: string; status?: number }[];
}

export interface Run {
  status: number | null;
  stdout: string;
  stderr: string;
  /** What the script wrote to $GITHUB_OUTPUT. */
  outputs: Record<string, string>;
}

const repoRoot = resolve(import.meta.dirname, "../..");

export function fakeGitHub(initial: GitHubState) {
  const dir = mkdtempSync(join(tmpdir(), "deadlift-test-gh-"));
  const statePath = join(dir, "state.json");
  writeFileSync(statePath, JSON.stringify(initial, null, 2));
  const gh = join(dir, "gh");
  writeFileSync(
    gh,
    `#!/usr/bin/env bash\nexec node ${JSON.stringify(join(import.meta.dirname, "fake-gh.ts"))} "$@"\n`,
  );
  chmodSync(gh, 0o755);

  return {
    state: (): GitHubState => JSON.parse(readFileSync(statePath, "utf8")),
    /** Changes the state, as GitHub changes while a run waits. */
    update(change: (state: GitHubState) => void) {
      const state: GitHubState = JSON.parse(readFileSync(statePath, "utf8"));
      change(state);
      writeFileSync(statePath, JSON.stringify(state, null, 2));
    },
    /** The environment a job needs to reach this fake through `gh`. */
    env: { PATH: `${dir}:${process.env.PATH}`, HOME: dir, FAKE_GITHUB_STATE: statePath },
    /** Runs a script from the repo, with this fake's `gh` first on PATH. */
    run(script: string, args: string[], env: Record<string, string> = {}): Run {
      const outputFile = join(dir, "github-output");
      writeFileSync(outputFile, "");
      const result = spawnSync("bash", [join(repoRoot, script), ...args], {
        encoding: "utf8",
        env: {
          PATH: `${dir}:${process.env.PATH}`,
          HOME: dir,
          FAKE_GITHUB_STATE: statePath,
          GH_TOKEN: "fake-token",
          GITHUB_OUTPUT: outputFile,
          ...env,
        },
      });
      return {
        status: result.status,
        stdout: result.stdout,
        stderr: result.stderr,
        outputs: parseOutputFile(readFileSync(outputFile, "utf8")),
      };
    },
    /** Runs a composite action from the repo, with this fake's `gh` first on PATH. */
    runAction(
      action: string,
      inputs: Record<string, string>,
      github: Record<string, string> = {},
    ): ActionRun {
      return runAction(action, {
        inputs,
        github: { token: "fake-token", ...github },
        env: { PATH: `${dir}:${process.env.PATH}`, HOME: dir, FAKE_GITHUB_STATE: statePath },
      });
    },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}
