// The prune-registry action, driven through its script with a fake GitHub on PATH.
import { afterEach, describe, expect, it } from "vitest";
import { fakeGitHub, type GitHubState, type PackageVersion } from "./support/fake-github.ts";

const script = "actions/prune-registry/prune-registry.py";

let nextId = 1;
/** A GHCR version pushed `ageInDays` ago with these tags. */
function version(tags: string[], ageInDays = 0): PackageVersion {
  const id = nextId++;
  return {
    id,
    name: `sha256:${id.toString(16).padStart(64, "0")}`,
    created_at: new Date(Date.UTC(2026, 10, 13) - ageInDays * 86_400_000).toISOString(),
    metadata: { package_type: "container", container: { tags } },
  };
}

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

/** A fake GitHub where acme/shop's images are in GHCR as acme's packages shop/<image>. */
function github(
  images: Record<string, PackageVersion[]>,
  {
    ownerType = "Organization" as "User" | "Organization",
    failures = [] as GitHubState["failures"],
    maxPerPage = 100,
  } = {},
) {
  const fake = fakeGitHub({
    repos: { "acme/shop": { ownerType, branches: {}, commits: {} } },
    owners: { acme: ownerType },
    packages: {
      acme: Object.fromEntries(Object.entries(images).map(([image, v]) => [`shop/${image}`, v])),
    },
    failures,
    maxPerPage,
  });
  cleanups.push(fake.cleanup);
  return {
    ...fake,
    prune: (...args: string[]) => fake.run(script, args, { GITHUB_REPOSITORY: "acme/shop" }),
    /** The tags of each version of an image still in GHCR. */
    tags: (image: string) =>
      fake.state().packages!.acme[`shop/${image}`].map((v) => v.metadata.container.tags.join(",")).sort(),
  };
}

describe("prune-registry", () => {
  it("deletes an image's versions except the ones it's told to keep", () => {
    const fake = github({
      web: [version(["sha-c1"], 3), version(["sha-c2"], 2), version(["sha-c3"], 1)],
    });

    const run = fake.prune("--keep", "sha-c2,sha-c3", "web");

    expect(run.status, run.stderr).toBe(0);
    expect(fake.tags("web")).toEqual(["sha-c2", "sha-c3"]);
  });

  it("prunes images of a repository a user owns", () => {
    const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] }, { ownerType: "User" });

    const run = fake.prune("--keep", "sha-c2", "web");

    expect(run.status, run.stderr).toBe(0);
    expect(fake.tags("web")).toEqual(["sha-c2"]);
  });

  it("prunes every image it's given, however many pages of versions each has", () => {
    const many = (prefix: string) => Array.from({ length: 7 }, (_, i) => version([`${prefix}-${i}`], i));
    const fake = github(
      { web: many("sha-w"), worker: many("sha-k"), sandbox: many("sha-s") },
      { maxPerPage: 3 },
    );

    const run = fake.prune("--keep", "sha-w-6,sha-k-6", "web", "worker");

    expect(run.status, run.stderr).toBe(0);
    expect(fake.tags("web")).toEqual(["sha-w-6"]);
    expect(fake.tags("worker")).toEqual(["sha-k-6"]);
    expect(fake.tags("sandbox")).toHaveLength(7);
  });

  it("keeps a version by any of its tags or by its digest, and deletes untagged ones", () => {
    const byDigest = version(["sha-c1"], 4);
    const fake = github({
      web: [byDigest, version([], 3), version(["sha-c2", "nightly"], 2), version(["sha-c3"], 1)],
    });

    const run = fake.prune("--keep", `nightly ${byDigest.name}`, "web");

    expect(run.status, run.stderr).toBe(0);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2,nightly"]);
  });

  // A workflow that builds its keep list from a failed API call passes an empty one.
  it.each([[["--keep", ""]], [["--keep", " , "]], [[]]])(
    "refuses an empty keep list, deleting nothing (%j)",
    (args) => {
      const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] });

      const run = fake.prune(...args, "web");

      expect(run.status).toBe(2);
      expect(run.stderr).toMatch(/^deadlift: .*keep-none/m);
      expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
    },
  );

  // Such as bare commits where the tags are sha-<commit>: every version would go.
  it("refuses a keep list that names none of an image's versions, deleting nothing", () => {
    const fake = github({
      web: [version(["sha-c1"], 2), version(["sha-c2"], 1)],
      worker: [version(["sha-c1"], 2), version(["sha-c2"], 1)],
      sandbox: [version(["sha-c2"], 1)],
    });

    const run = fake.prune("--keep", "c2,sha-c1", "web", "worker", "sandbox");

    expect(run.status).toBe(2);
    expect(run.stderr).toMatch(/^deadlift: .*none of sandbox's 1 versions/m);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
    expect(fake.tags("worker")).toEqual(["sha-c1", "sha-c2"]);
  });

  it("fails, deleting nothing, when an image's versions can't be listed", () => {
    const fake = github(
      { web: [version(["sha-c1"], 2), version(["sha-c2"], 1)], worker: [version(["sha-c2"], 1)] },
      { failures: [{ method: "GET", pathPrefix: "orgs/acme/packages/container/shop%2Fworker/", status: 403 }] },
    );

    const run = fake.prune("--keep", "sha-c2", "web", "worker");

    expect(run.status).toBe(1);
    expect(run.stderr).toMatch(/^deadlift: couldn't list worker's versions .*nothing was deleted/m);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
  });

  it("fails, deleting nothing, when GitHub can't say who owns the repository", () => {
    const fake = github(
      { web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] },
      { failures: [{ method: "GET", pathPrefix: "repos/acme/shop" }] },
    );

    const run = fake.prune("--keep", "sha-c2", "web");

    expect(run.status).toBe(1);
    expect(run.stderr).toMatch(/^deadlift: couldn't look up acme\/shop .*nothing was deleted/m);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
  });

  it("fails, deleting nothing, for an image that isn't in GHCR", () => {
    const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] });

    const run = fake.prune("--keep", "sha-c2", "web", "wrker");

    expect(run.status).toBe(1);
    expect(run.stderr).toMatch(/^deadlift: couldn't list wrker's versions .*nothing was deleted/m);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
  });

  it("fails, saying what it deleted, when a deletion fails", () => {
    const fake = github({
      web: [version(["sha-c1"], 3), version(["sha-c2"], 2), version(["sha-c3"], 1)],
      worker: [version(["sha-c1"], 3), version(["sha-c3"], 1)],
    }, { failures: [{ method: "DELETE", pathPrefix: "orgs/acme/packages/container/shop%2Fworker/" }] });

    const run = fake.prune("--keep", "sha-c3", "web", "worker");

    expect(run.status).toBe(1);
    expect(run.stderr).toMatch(/^deadlift: couldn't delete worker:sha-c1\b.*2 versions were deleted before it/m);
    expect(fake.tags("web")).toEqual(["sha-c3"]);
    expect(fake.tags("worker")).toEqual(["sha-c1", "sha-c3"]);
  });

  it("reports what it deleted", () => {
    const untagged = version([], 3);
    const fake = github({
      web: [untagged, version(["sha-c2"], 2), version(["sha-c3"], 1)],
      worker: [version(["sha-c3"], 1)],
    });

    const run = fake.prune("--keep", "sha-c3", "web", "worker");

    expect(run.status, run.stderr).toBe(0);
    expect(run.stdout).toContain("Deleted web:sha-c2\n");
    expect(run.stdout).toContain(`Deleted web@${untagged.name}\n`);
    expect(run.stdout).toMatch(/^deadlift: deleted 2 of 4 versions of web, worker\.$/m);
  });

  it("says what it would delete, and deletes nothing, on a dry run", () => {
    const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] });

    const run = fake.prune("--dry-run", "--keep", "sha-c2", "web");

    expect(run.status, run.stderr).toBe(0);
    expect(run.stdout).toContain("Would delete web:sha-c1\n");
    expect(run.stdout).toMatch(/^deadlift: would delete 1 of 2 versions of web \(a dry run\)\.$/m);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
  });

  it("finds the images of a repository whose name has capitals, as GHCR lowercases them", () => {
    const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] });

    const run = fake.run(script, ["--keep", "sha-c2", "web"], { GITHUB_REPOSITORY: "Acme/Shop" });

    expect(run.status, run.stderr).toBe(0);
    expect(fake.tags("web")).toEqual(["sha-c2"]);
  });

  it.each([
    ["no images", ["--keep", "sha-c1"], {}],
    ["an image name that isn't one", ["--keep", "sha-c1", "../web"], {}],
    ["an unknown option", ["--keep", "sha-c1", "--force", "web"], {}],
    ["both keep and keep-none", ["--keep", "sha-c1", "--keep-none", "web"], {}],
    ["a repository that isn't <owner>/<name>", ["--keep", "sha-c1", "web"], { GITHUB_REPOSITORY: "shop" }],
  ])("refuses %s, deleting nothing", (_, args, env) => {
    const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] });

    const run = fake.run(script, args, { GITHUB_REPOSITORY: "acme/shop", ...env });

    expect(run.status).toBe(2);
    expect(run.stderr).toMatch(/^deadlift: /);
    expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
  });

  describe("as an action", () => {
    const action = "actions/prune-registry";
    const context = { repository: "acme/shop" };
    const two = () => ({
      web: [version(["sha-c1"], 2), version(["sha-c2"], 1)],
      worker: [version(["sha-c1"], 2), version(["sha-c2"], 1)],
    });

    it("prunes the images it's given, keeping the listed versions, and writes a summary", () => {
      const fake = github(two());

      const run = fake.runAction(action, { images: "web\nworker\n", keep: "sha-c2\n" }, context);

      expect(run.status, run.stderr).toBe(0);
      expect(fake.tags("web")).toEqual(["sha-c2"]);
      expect(fake.tags("worker")).toEqual(["sha-c2"]);
      expect(run.summary).toMatch(/^deadlift: deleted 2 of 4 versions of web, worker\.$/m);
    });

    it("refuses an empty keep list, failing the step", () => {
      const fake = github(two());

      const run = fake.runAction(action, { images: "web worker", keep: "" }, context);

      expect(run.status).toBe(2);
      expect(run.summary).toMatch(/^deadlift: .*keep-none/m);
      expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
    });

    it("deletes every version with keep-none", () => {
      const fake = github(two());

      const run = fake.runAction(action, { images: "web", "keep-none": "true" }, context);

      expect(run.status, run.stderr).toBe(0);
      expect(fake.tags("web")).toEqual([]);
      expect(fake.tags("worker")).toHaveLength(2);
    });

    it("deletes nothing on a dry run", () => {
      const fake = github(two());

      const run = fake.runAction(action, { images: "web", keep: "sha-c2", "dry-run": "true" }, context);

      expect(run.status, run.stderr).toBe(0);
      expect(run.summary).toContain("Would delete web:sha-c1");
      expect(fake.tags("web")).toEqual(["sha-c1", "sha-c2"]);
    });
  });

  it("deletes every version when told to keep none", () => {
    const fake = github({ web: [version(["sha-c1"], 2), version(["sha-c2"], 1)] });

    const run = fake.prune("--keep-none", "web");

    expect(run.status, run.stderr).toBe(0);
    expect(fake.tags("web")).toEqual([]);
  });
});
