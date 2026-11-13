// Seam B: setting up a Server. Runs the real `deadlift server bootstrap` against this machine
// over real SSH as root, with only `tailscale` faked, so it reconfigures this machine's Docker and
// firewall for good. It refuses to start anywhere but a throwaway machine: the GitHub-hosted
// ubuntu-24.04 runner in CI, or a Multipass VM (test/seam-b/multipass.sh).
//
// In CI, Seam A then runs on the Server this leaves behind (test/isolation.test.ts).
import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { whyNotThrowaway } from "../support/throwaway.ts";

const exec = promisify(execFile);
const repo = new URL("../..", import.meta.url).pathname;
const prefix = `deadlift-test-${randomBytes(4).toString("hex")}`;

let work = "";
const created: string[] = [];

type Result = { code: number; output: string };

/** Runs a command, collecting its exit code and its stdout and stderr together. */
async function run(command: string, args: string[], env = process.env): Promise<Result> {
  try {
    const { stdout, stderr } = await exec(command, args, { env, maxBuffer: 64 * 1024 * 1024 });
    return { code: 0, output: stdout + stderr };
  } catch (error) {
    const failed = error as { code?: number; stdout?: string; stderr?: string };
    return { code: failed.code ?? -1, output: `${failed.stdout ?? ""}${failed.stderr ?? ""}` };
  }
}

/** Runs a command that must succeed, returning its stdout. */
async function ok(command: string, ...args: string[]): Promise<string> {
  const { stdout } = await exec(command, args, { maxBuffer: 64 * 1024 * 1024 });
  return stdout.trim();
}

/** `deadlift server bootstrap`, as a maintainer runs it, at this machine. */
function bootstrap(): Promise<Result> {
  return run(join(repo, "bin/deadlift"), ["server", "bootstrap", "localhost"], {
    ...process.env,
    PATH: `${join(work, "bin")}:${process.env.PATH}`,
  });
}

/** This machine's firewall: every table's rules, without packet counters. */
async function firewall(): Promise<string> {
  const rules = [await ok("sudo", "iptables-save"), await ok("sudo", "ip6tables-save")].join("\n");
  return rules
    .split("\n")
    .filter((line) => !line.startsWith("#"))
    .map((line) => line.replace(/\[\d+:\d+\]/, ""))
    .join("\n");
}

beforeAll(async () => {
  const why = whyNotThrowaway();
  if (why) throw new Error(`Refusing to run Seam B: ${why}.`);
  work = await mkdtemp(join(tmpdir(), `${prefix}-`));
  // The maintainer's SSH, with a key of its own that root on this machine accepts.
  await ok("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", join(work, "key"));
  await ok("sudo", "bash", join(repo, "test/seam-b/prepare.sh"), join(work, "key.pub"));
  await ok("mkdir", join(work, "bin"));
  await writeFile(
    join(work, "bin/ssh"),
    `#!/bin/sh\nexec /usr/bin/ssh -i ${join(work, "key")} -o IdentitiesOnly=yes -o UserKnownHostsFile=${join(work, "known_hosts")} "$@"\n`,
  );
  await chmod(join(work, "bin/ssh"), 0o755);
});

afterAll(async () => {
  for (const name of created) await run("docker", ["rm", "--force", name]);
  if (work) await rm(work, { recursive: true, force: true });
});

describe("deadlift server bootstrap", () => {
  test("turns this machine into a Server", async () => {
    const result = await bootstrap();

    expect(result.code, result.output).toBe(0);
    const reasons = result.output.split("\n").filter((line) => line.startsWith("deadlift: "));
    expect(reasons.at(-1)).toMatch(/is a Server/);
    // The daemon really remaps users, so root in a container is unprivileged on the host.
    expect(await ok("docker", "info", "--format", "{{json .SecurityOptions}}")).toMatch(
      /name=userns/,
    );
    // What runs as root is what bootstrap installed, owned by root and writable by no one else.
    for (const [installed, source] of [
      ["/usr/local/sbin/deadlift-entry", "server/entry.sh"],
      ["/usr/local/lib/deadlift/deploy.sh", "server/deploy.sh"],
    ] as const) {
      expect(await ok("stat", "--format", "%U:%G %a", installed)).toBe("root:root 755");
      expect(await readFile(installed, "utf8")).toBe(await readFile(join(repo, source), "utf8"));
    }
    // The Server-wide settings, with the disk thresholds' defaults.
    const settings = await ok("sudo", "cat", "/srv/deadlift/settings");
    expect(settings).toMatch(/^feature_deploy_disk_limit=80$/m);
    expect(settings).toMatch(/^watchdog_disk_limit=90$/m);
  });

  test("changes no file and restarts nothing when run again", async () => {
    // An Environment's container, running across the second run.
    const container = `${prefix}-running`;
    created.push(container);
    await ok("docker", "run", "--detach", "--name", container, "alpine:3.20", "sleep", "600");
    const startedAt = () => ok("docker", "inspect", "--format", "{{.State.StartedAt}}", container);
    const dockerPid = () => ok("systemctl", "show", "docker.service", "--property=MainPID", "--value");
    const before = { started: await startedAt(), docker: await dockerPid(), rules: await firewall() };
    const marker = join(work, "marker");
    await writeFile(marker, "");

    const result = await bootstrap();

    expect(result.code, result.output).toBe(0);
    // Everywhere bootstrap installs: system config, root's commands, and the Server's state.
    const changed = await ok(
      "sudo",
      "find",
      "/etc",
      "/usr/local/sbin",
      "/usr/local/bin",
      "/usr/local/lib/deadlift",
      "/srv/deadlift",
      "-xdev",
      "-newer",
      marker,
    );
    expect(changed).toBe("");
    expect(await dockerPid()).toBe(before.docker);
    expect(await startedAt()).toBe(before.started);
    expect(await ok("docker", "inspect", "--format", "{{.State.Running}}", container)).toBe("true");
    expect(await firewall()).toBe(before.rules);
  });

  test("keeps a Server-wide setting the maintainer changed", async () => {
    await ok("sudo", "sed", "-i", "s/^feature_deploy_disk_limit=.*/feature_deploy_disk_limit=70/", "/srv/deadlift/settings");

    const result = await bootstrap();

    expect(result.code, result.output).toBe(0);
    expect(await ok("sudo", "cat", "/srv/deadlift/settings")).toMatch(/^feature_deploy_disk_limit=70$/m);
    await ok("sudo", "sed", "-i", "s/^feature_deploy_disk_limit=.*/feature_deploy_disk_limit=80/", "/srv/deadlift/settings");
  });
});
