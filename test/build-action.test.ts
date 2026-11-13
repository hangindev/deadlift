// The build action, run through its action.yml on a fake runner whose `docker` stands in for
// Docker and GHCR, recording what was pushed.
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { fakeRunner } from "./support/fake-runner.ts";

const cleanups: (() => void)[] = [];
afterEach(() => cleanups.splice(0).forEach((cleanup) => cleanup()));

const commit = "0123456789abcdef0123456789abcdef01234567";

/** A runner whose checkout holds these files (Dockerfiles, by path). */
function runner(files: string[] = ["Dockerfile"]) {
  const fake = fakeRunner();
  cleanups.push(fake.cleanup);
  for (const file of files) {
    mkdirSync(dirname(join(fake.workspace, file)), { recursive: true });
    writeFileSync(join(fake.workspace, file), "FROM scratch\n");
  }
  return {
    ...fake,
    build: (inputs: Record<string, string>) =>
      fake.runAction("actions/build", inputs, {
        github: {
          sha: commit,
          repository: "Acme/Shop",
          actor: "octocat",
          server_url: "https://github.com",
        },
      }),
  };
}

describe("the build action", () => {
  it("pushes each named image to GHCR as sha-<commit>, and outputs their references for deliver", () => {
    const fake = runner(["Dockerfile", "worker/Dockerfile"]);

    const run = fake.build({ images: "web\nworker context=worker\n" });

    expect(run.status, run.stderr).toBe(0);
    // GHCR's names are lowercase.
    expect(fake.pushed().map((image) => image.ref)).toEqual([
      `ghcr.io/acme/shop/web:sha-${commit}`,
      `ghcr.io/acme/shop/worker:sha-${commit}`,
    ]);
    expect(run.outputs.images).toBe(
      `web=ghcr.io/acme/shop/web:sha-${commit}\nworker=ghcr.io/acme/shop/worker:sha-${commit}`,
    );
  });

  it("pushes one image for the Server's platform with no attestations, which prune-registry needs", () => {
    const fake = runner();

    fake.build({ images: "web" });
    fake.build({ images: "web", platform: "linux/arm64" });

    expect(fake.pushed()).toMatchObject([
      { platform: "linux/amd64", provenance: "false", sbom: "false" },
      { platform: "linux/arm64", provenance: "false", sbom: "false" },
    ]);
  });

  it("pushes with the job's token, and leaves no login behind on the runner", () => {
    const fake = runner();

    const run = fake.build({ images: "web" });

    expect(run.status, run.stderr).toBe(0);
    expect(fake.pushed()[0].login).toEqual({
      registry: "ghcr.io",
      username: "octocat",
      password: "fake-job-token",
    });
    expect(existsSync(join(fake.home, ".docker/config.json"))).toBe(false);
  });

  it("labels each image with its repository and commit, so GHCR links the package to the repository", () => {
    const fake = runner();

    fake.build({ images: "web" });

    expect(fake.pushed()[0].labels).toEqual(
      expect.arrayContaining([
        "org.opencontainers.image.source=https://github.com/Acme/Shop",
        `org.opencontainers.image.revision=${commit}`,
      ]),
    );
  });

  it("builds an image from the Dockerfile, context and stage it names", () => {
    const fake = runner(["Dockerfile", "docker/sandbox.Dockerfile"]);

    const run = fake.build({ images: "web\n\nsandbox_runner file=docker/sandbox.Dockerfile target=runtime\n" });

    expect(run.status, run.stderr).toBe(0);
    expect(fake.pushed()).toMatchObject([
      { ref: `ghcr.io/acme/shop/web:sha-${commit}`, context: ".", dockerfile: "./Dockerfile", target: "" },
      {
        ref: `ghcr.io/acme/shop/sandbox_runner:sha-${commit}`,
        context: ".",
        dockerfile: "docker/sandbox.Dockerfile",
        target: "runtime",
      },
    ]);
  });

  it.each([
    ["an image name deliver couldn't pass on", { images: "Web" }, /'Web' isn't an image name/],
    ["an image name GHCR can't hold", { images: "web_" }, /'web_' isn't an image name/],
    ["the same image twice", { images: "web\nweb context=web" }, /the image web is given twice/],
    ["an unknown setting", { images: "web dockerfile=x" }, /'dockerfile=x' isn't a setting of the image web/],
    ["no image", { images: "\n" }, /no image was named/],
    ["several platforms", { images: "web", platform: "linux/amd64,linux/arm64" }, /--platform needs one platform/],
    ["a commit that isn't a full SHA", { images: "web", commit: "main" }, /--commit needs a full commit SHA/],
  ])("refuses %s, building nothing", (_, inputs, reason) => {
    const fake = runner();

    const run = fake.build(inputs);

    expect(run.status).toBe(2);
    expect(run.stderr).toMatch(new RegExp(`^deadlift: ${reason.source}.*, so nothing was built\\.$`, "m"));
    expect(fake.pushed()).toEqual([]);
  });

  it("fails when an image doesn't build, saying which and what was pushed before it", () => {
    const fake = runner(["Dockerfile"]);

    const run = fake.build({ images: "web\nworker context=worker\nsandbox" });

    expect(run.status).toBe(1);
    expect(run.stderr).toMatch(
      new RegExp(`^deadlift: couldn't build and push the image worker\\. Pushed before it: ghcr\\.io/acme/shop/web:sha-${commit}\\.$`, "m"),
    );
    expect(fake.pushed().map((image) => image.ref)).toEqual([`ghcr.io/acme/shop/web:sha-${commit}`]);
    expect(run.outputs.images).toBe("");
  });

  it("lists what it pushed in the job summary", () => {
    const fake = runner();

    const run = fake.build({ images: "web" });

    expect(run.summary).toContain(`ghcr.io/acme/shop/web:sha-${commit}`);
  });
});
