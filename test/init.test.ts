// `deadlift init`, run in an App's repository as its maintainer would. What the workflows it
// writes do is in starter-workflows.test.ts.
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { parse } from "yaml";
import { appRepository as newAppRepository } from "./support/app-repository.ts";

const deadlift = resolve(import.meta.dirname, "../bin/deadlift");
const starters = ["deadlift-feature", "deadlift-dev", "deadlift-sweep", "deadlift-rollback"];

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

function appRepository() {
  const repo = newAppRepository();
  cleanups.push(repo.cleanup);
  return repo;
}

describe("deadlift init", () => {
  it("writes the four starter workflows into the App's repository", () => {
    const repo = appRepository();

    const run = repo.init(["shop", "--server", "deadlift-1"]);

    expect(run.status, run.stderr).toBe(0);
    for (const name of starters) {
      expect(existsSync(repo.workflow(name)), name).toBe(true);
      expect(parse(repo.read(name)).name).toBe(name);
      expect(run.stdout).toContain(`.github/workflows/${name}.yml`);
    }
  });

  it("writes them at the repository's root, from anywhere in it", () => {
    const repo = appRepository();
    mkdirSync(join(repo.dir, "src"));

    const run = repo.init(["shop", "--server", "deadlift-1"], join(repo.dir, "src"));

    expect(run.status, run.stderr).toBe(0);
    expect(existsSync(repo.workflow("deadlift-dev"))).toBe(true);
    expect(existsSync(join(repo.dir, "src/.github"))).toBe(false);
  });

  it("refuses outside a git repository, writing nothing", () => {
    const dir = mkdtempSync(join(tmpdir(), "deadlift-test-not-a-repo-"));
    cleanups.push(() => rmSync(dir, { recursive: true, force: true }));

    const result = spawnSync(deadlift, ["init", "shop", "--server", "deadlift-1"], { cwd: dir, encoding: "utf8" });

    expect(result.status).toBe(2);
    expect(result.stderr).toMatch(/^deadlift: .*git repository/m);
    expect(existsSync(join(dir, ".github"))).toBe(false);
  });

  it.each([
    [["Shop", "--server", "deadlift-1"], /isn't an App name/],
    [["shop"], /--server/],
    [["shop", "--server", "bad host"], /isn't a host name/],
    [["shop", "--server", "deadlift-1", "--branch", "-x"], /isn't a branch/],
    [["shop", "--server", "deadlift-1", "--image", "Web"], /isn't an image name/],
  ])("refuses %j, writing nothing", (args, reason) => {
    const repo = appRepository();

    const run = repo.init(args);

    expect(run.status).toBe(2);
    expect(run.stderr).toMatch(/^deadlift: /);
    expect(run.stderr).toMatch(reason);
    expect(existsSync(join(repo.dir, ".github"))).toBe(false);
  });

  it("leaves a workflow the App has changed alone when run again, and says so", () => {
    const repo = appRepository();
    repo.init(["shop", "--server", "deadlift-1"]);
    const changed = repo.read("deadlift-feature").replace("skip-deploy", "no-preview");
    writeFileSync(repo.workflow("deadlift-feature"), changed);

    const run = repo.init(["shop", "--server", "deadlift-2"]);

    expect(run.status, run.stderr).toBe(0);
    expect(repo.read("deadlift-feature")).toBe(changed);
    expect(run.stdout).toMatch(/Kept \.github\/workflows\/deadlift-feature\.yml.*changed/);
    // The ones the App hasn't changed follow the new arguments.
    expect(repo.read("deadlift-dev")).toContain("deadlift-2");
    expect(repo.read("deadlift-dev")).not.toContain("deadlift-1");
  });

  it("leaves a workflow of the App's own by a starter's name alone", () => {
    const repo = appRepository();
    mkdirSync(join(repo.dir, ".github/workflows"), { recursive: true });
    writeFileSync(repo.workflow("deadlift-dev"), "name: ours\n");

    const run = repo.init(["shop", "--server", "deadlift-1"]);

    expect(run.status, run.stderr).toBe(0);
    expect(repo.read("deadlift-dev")).toBe("name: ours\n");
    expect(existsSync(repo.workflow("deadlift-feature"))).toBe(true);
  });

  it("changes nothing when run again with the same arguments", () => {
    const repo = appRepository();
    repo.init(["shop", "--server", "deadlift-1"]);
    const before = starters.map(repo.read);

    const run = repo.init(["shop", "--server", "deadlift-1"]);

    expect(run.status, run.stderr).toBe(0);
    expect(starters.map(repo.read)).toEqual(before);
    expect(run.stdout).not.toMatch(/Kept/);
  });

  it("tells the maintainer what the workflows need from the repository", () => {
    const run = appRepository().init(["shop", "--server", "deadlift-1"]);

    expect(run.stdout).toContain("TS_OAUTH_CLIENT_ID");
    expect(run.stdout).toContain("TS_AUDIENCE");
    expect(run.stdout).toContain("skip-deploy");
  });
});
