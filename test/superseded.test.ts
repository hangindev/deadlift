// The superseded action, driven through its script with a fake GitHub on PATH.
import { afterEach, describe, expect, it } from "vitest";
import { fakeGitHub, type GitHubState } from "./support/fake-github.ts";

const script = "actions/superseded/superseded.sh";
const repo = "acme/shop";

// A history on main: c1 <- c2 <- c3.
const c1 = "1".repeat(40);
const c2 = "2".repeat(40);
const c3 = "3".repeat(40);

function github(branches: Record<string, string>, commits: Record<string, string[]> = {}) {
  const state: GitHubState = {
    repos: {
      [repo]: {
        ownerType: "Organization",
        branches,
        commits: { [c1]: [], [c2]: [c1], [c3]: [c2], ...commits },
      },
    },
  };
  const fake = fakeGitHub(state);
  cleanups.push(fake.cleanup);
  return fake;
}

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

function superseded(fake: ReturnType<typeof github>, branch: string, commit: string) {
  return fake.run(script, [branch, commit], { GITHUB_REPOSITORY: repo });
}

describe("superseded", () => {
  it("says a commit at the tracked branch's head isn't superseded", () => {
    const run = superseded(github({ main: c3 }), "main", c3);

    expect(run.status, run.stderr).toBe(0);
    expect(run.outputs).toMatchObject({ superseded: "false", head: c3 });
  });

  it("says a commit the tracked branch has moved past is superseded", () => {
    const run = superseded(github({ main: c3 }), "main", c1);

    expect(run.status, run.stderr).toBe(0);
    expect(run.outputs).toMatchObject({ superseded: "true", head: c3 });
    expect(run.outputs.reason).toMatch(/^deadlift: main has moved past 1{40} to 3{40}/);
  });

  it("tracks any branch, not only main", () => {
    const fake = github({ main: c3, "release/2.x": c2 });

    expect(superseded(fake, "release/2.x", c2).outputs).toMatchObject({ superseded: "false" });
    expect(superseded(fake, "release/2.x", c1).outputs).toMatchObject({ superseded: "true" });
  });

  // A force-push replaced c3 with x3, or reset main back to c2.
  const x3 = "a".repeat(40);
  it.each([
    ["was force-pushed past", { main: x3 }],
    ["was reset back behind", { main: c2 }],
  ])("says a commit is superseded when the tracked branch %s it", (_, branches) => {
    const run = superseded(github(branches, { [x3]: [c2] }), "main", c3);

    expect(run.status, run.stderr).toBe(0);
    expect(run.outputs).toMatchObject({ superseded: "true", head: branches.main });
    expect(run.outputs.reason).toMatch(/^deadlift: 3{40} isn't in main's history any more/);
  });

  it("fails, saying nothing either way, when the tracked branch doesn't exist", () => {
    const run = superseded(github({ main: c3 }), "trunk", c3);

    expect(run.status).toBe(1);
    expect(run.outputs).toEqual({});
    expect(run.stderr).toMatch(/^deadlift: couldn't find trunk's head in acme\/shop/m);
  });

  it("fails, saying nothing either way, when GitHub can't compare the commits", () => {
    const fake = github({ main: c3 });
    const state = fake.state();
    state.failures = [{ method: "GET", pathPrefix: `repos/${repo}/compare/` }];
    const failing = fakeGitHub(state);
    cleanups.push(failing.cleanup);

    const run = superseded(failing, "main", c1);

    expect(run.status).toBe(1);
    expect(run.outputs).toEqual({});
    expect(run.stderr).toMatch(/^deadlift: couldn't compare 1{40} with main's head 3{40}/m);
  });

  it.each([
    ["no commit", ["main"]],
    ["a commit that isn't a SHA", ["main", "HEAD~1"]],
    ["an empty branch", ["", c3]],
    ["a branch that isn't a branch name", ["main/../../x", c3]],
    ["a branch with a query in it", ["main?x=1", c3]],
  ])("refuses %s", (_, args) => {
    const fake = github({ main: c3 });

    const run = fake.run(script, args, { GITHUB_REPOSITORY: repo });

    expect(run.status).toBe(2);
    expect(run.outputs).toEqual({});
    expect(run.stderr).toMatch(/^deadlift: /);
  });
});
