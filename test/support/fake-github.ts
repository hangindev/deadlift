// A fake GitHub for testing deadlift's actions: a `gh` on PATH (fake-gh.ts) backed by a JSON
// state file. Tests set up the state, run an action's script, then read the state back to see
// what the action did to GitHub. No network.
import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

export interface PackageVersion {
  id: number;
  /** The manifest's digest, as GHCR names a version. */
  name: string;
  created_at: string;
  metadata: { package_type: "container"; container: { tags: string[] } };
}

export interface GitHubState {
  repos?: Record<
    string,
    {
      ownerType: "User" | "Organization";
      /** Branch name to the commit it points at. */
      branches: Record<string, string>;
      /** Commit to its parents. */
      commits: Record<string, string[]>;
    }
  >;
  /** Owner login to its type, which decides whether its packages are under users/ or orgs/. */
  owners?: Record<string, "User" | "Organization">;
  /** Owner login to package name (as GHCR names it, e.g. `shop/web`) to its versions. */
  packages?: Record<string, Record<string, PackageVersion[]>>;
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
        outputs: parseOutputs(readFileSync(outputFile, "utf8")),
      };
    },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

/** Parses the `key=value` lines an action's script wrote to $GITHUB_OUTPUT. */
function parseOutputs(file: string): Record<string, string> {
  return Object.fromEntries(
    file
      .split("\n")
      .filter((line) => line.includes("="))
      .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
  );
}
