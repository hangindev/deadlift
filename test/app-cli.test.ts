// `deadlift app` refusing on the maintainer's computer, before it reaches the Server or the
// Tailscale API. What it does on a Server, the server-setup tests check
// (test/server-setup/app.test.ts).
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, test } from "vitest";

const repo = new URL("..", import.meta.url).pathname;
const dir = mkdtempSync(join(tmpdir(), "deadlift-test-cli-"));
const sshCalls = join(dir, "ssh-calls");
// An ssh that records that it was called, and reaches nothing.
writeFileSync(join(dir, "ssh"), `#!/bin/sh\necho "$*" >>${sshCalls}\nexit 255\n`);
chmodSync(join(dir, "ssh"), 0o755);
// Nothing listens here, so any call to the Tailscale API fails.
const env = { PATH: `${dir}:${process.env.PATH}`, HOME: dir, DEADLIFT_TAILSCALE_API: "http://127.0.0.1:9" };

afterAll(() => rmSync(dir, { recursive: true, force: true }));

function deadlift(args: string[], extra: Record<string, string> = {}) {
  rmSync(sshCalls, { force: true });
  const result = spawnSync(join(repo, "bin/deadlift"), args, { encoding: "utf8", env: { ...env, ...extra } });
  const reasons = result.stderr.split("\n").filter((line) => line.startsWith("deadlift: "));
  return { code: result.status, reason: reasons.at(-1) ?? "", reachedServer: existsSync(sshCalls) };
}

const key = { TAILSCALE_API_KEY: "tskey-api-fake" };

describe("deadlift app, refusing before anything changes", () => {
  test.each([
    ["an App name with capitals", ["app", "add", "MyApp", "--server", "s", "--repo", "o/r"], /isn't an App name/],
    ["an App name too long for its deploy user", ["app", "add", "a".repeat(24), "--server", "s", "--repo", "o/r"], /isn't an App name/],
    ["an App name that could clash with another's Feature Environments", ["app", "add", "shop-feature", "--server", "s", "--repo", "o/r"], /isn't an App name/],
    ["app add with no repo", ["app", "add", "shop", "--server", "s"], /--repo/],
    ["app add with no Server", ["app", "add", "shop", "--repo", "o/r"], /--server is required/],
    ["app secrets for an Environment kind that isn't dev or feature", ["app", "secrets", "shop", "prod", "/dev/null", "--server", "s"], /dev or feature/],
    ["app secrets with no file there", ["app", "secrets", "shop", "dev", join(dir, "missing.env"), "--server", "s"], /no file/],
    ["app remove without --yes", ["app", "remove", "shop", "--server", "s"], /--yes/],
  ])("refuses %s", (_, args, why) => {
    const result = deadlift(args, key);

    expect(result.code).toBe(2);
    expect(result.reason).toMatch(why);
    expect(result.reachedServer).toBe(false);
  });

  test.each([
    ["app add", ["app", "add", "shop", "--server", "s", "--repo", "o/r"]],
    ["app remove", ["app", "remove", "shop", "--server", "s", "--yes"]],
  ])("refuses %s without the maintainer's Tailscale credential", (_, args) => {
    const result = deadlift(args);

    expect(result.code).toBe(2);
    expect(result.reason).toMatch(/TAILSCALE_API_KEY/);
    expect(result.reachedServer).toBe(false);
  });

  test("says when the Server can't be reached", () => {
    const result = deadlift(["app", "add", "shop", "--server", "s", "--repo", "o/r"], key);

    expect(result.code).toBe(1);
    expect(result.reason).toMatch(/couldn't reach s over SSH as root, so nothing was changed/);
  });
});
