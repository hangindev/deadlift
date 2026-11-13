// An App's repository on the maintainer's computer: a fresh git repository, where `deadlift init`
// runs as the maintainer would run it.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const deadlift = resolve(import.meta.dirname, "../../bin/deadlift");

export function appRepository() {
  const dir = mkdtempSync(join(tmpdir(), "deadlift-test-app-"));
  spawnSync("git", ["init", "--quiet", dir]);
  return {
    dir,
    /** Runs `deadlift init` with these arguments, in the repository or `cwd`. */
    init(args: string[], cwd = dir) {
      const result = spawnSync(deadlift, ["init", ...args], { cwd, encoding: "utf8" });
      return { status: result.status, stdout: result.stdout, stderr: result.stderr };
    },
    /** A workflow's path, by name without `.yml`. */
    workflow: (name: string) => join(dir, ".github/workflows", `${name}.yml`),
    read: (name: string) => readFileSync(join(dir, ".github/workflows", `${name}.yml`), "utf8"),
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}
