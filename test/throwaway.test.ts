// The guard that keeps Seam B, and the Seam A tests that need a bootstrapped Server, off any
// machine that isn't thrown away after the run.
import { describe, expect, test } from "vitest";
import { whyNotThrowaway } from "./support/throwaway.ts";

const runner = { GITHUB_ACTIONS: "true", RUNNER_ENVIRONMENT: "github-hosted", ImageOS: "ubuntu24" };

describe("the throwaway-machine guard", () => {
  test("lets a GitHub-hosted ubuntu-24.04 runner through", () => {
    expect(whyNotThrowaway(runner, false)).toBeUndefined();
  });

  test("lets a Multipass VM marked by test/seam-b/multipass.sh through", () => {
    expect(whyNotThrowaway({}, true)).toBeUndefined();
  });

  test.each([
    ["a developer's machine", {}],
    ["a self-hosted runner", { ...runner, RUNNER_ENVIRONMENT: "self-hosted" }],
    ["a GitHub-hosted runner of another image", { ...runner, ImageOS: "ubuntu22" }],
    ["a machine that only claims to be in GitHub Actions", { GITHUB_ACTIONS: "true" }],
  ])("stops %s", (_, env) => {
    expect(whyNotThrowaway(env, false)).toMatch(/isn't a throwaway machine/);
  });
});
