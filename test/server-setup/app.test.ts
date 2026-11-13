// The server-setup tests: an App's place on a Server. Runs the real `deadlift app add`, `app
// secrets`, `app remove` and `server status` against this machine over real SSH as root, after the
// real bootstrap, with only `tailscale` and the Tailscale API faked. A delivery then reaches the
// App's Dev Environment over real SSH as its deploy user, through its real sudo rule. Like
// bootstrap.test.ts, it refuses to start anywhere but a throwaway machine.
import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { chmod, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { fakeApiKey, startFakeTailscaleApi } from "../support/fake-tailscale-api.ts";
import { whyNotThrowaway } from "../support/throwaway.ts";

const exec = promisify(execFile);
const repo = new URL("../..", import.meta.url).pathname;
const hex = randomBytes(4).toString("hex");
const prefix = `deadlift-test-${hex}`;
// Two Apps, named so nothing else on the machine could be theirs.
const appA = `t${hex}a`;
const appB = `t${hex}b`;

let work = "";
let api: Awaited<ReturnType<typeof startFakeTailscaleApi>>;

type Result = { code: number; stdout: string; stderr: string; output: string };

/** Runs a command, collecting its exit code, stdout and stderr. */
async function run(command: string, args: string[], options: { env?: NodeJS.ProcessEnv; cwd?: string } = {}): Promise<Result> {
  try {
    const { stdout, stderr } = await exec(command, args, { ...options, maxBuffer: 64 * 1024 * 1024 });
    return { code: 0, stdout, stderr, output: stdout + stderr };
  } catch (error) {
    const failed = error as { code?: number; stdout?: string; stderr?: string };
    const stdout = failed.stdout ?? "";
    const stderr = failed.stderr ?? "";
    return { code: failed.code ?? -1, stdout, stderr, output: stdout + stderr };
  }
}

/** Runs a command that must succeed, returning its stdout. */
async function ok(command: string, ...args: string[]): Promise<string> {
  const { stdout } = await exec(command, args, { maxBuffer: 64 * 1024 * 1024 });
  return stdout.trim();
}

/** The maintainer's environment: their SSH, and their Tailscale credential for the fake API. */
function maintainerEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PATH: `${join(work, "bin")}:${process.env.PATH}`,
    TAILSCALE_API_KEY: fakeApiKey,
    DEADLIFT_TAILSCALE_API: api.url,
  };
}

/** `deadlift <args>`, as the maintainer runs it, at this machine. */
function deadlift(...args: string[]): Promise<Result> {
  return run(join(repo, "bin/deadlift"), args, { env: maintainerEnv() });
}

function addA(): Promise<Result> {
  return deadlift(
    "app", "add", appA, "--server", "localhost", "--repo", "owner/app-a",
    "--slots", "2", "--snapshots", "1", "--memory", "256m", "--cpus", "0.5", "--pids", "100",
  );
}

/** The last reason line a command printed. */
function reason(result: Result): string | undefined {
  return result.output.split("\n").filter((line) => line.startsWith("deadlift: ")).at(-1);
}

/** The tailnet policy entries `app add` printed, as JSON. */
function printedPolicy(stdout: string): unknown {
  const lines = stdout.split("\n");
  const start = lines.indexOf("{");
  const end = lines.indexOf("}", start);
  expect(start, stdout).toBeGreaterThanOrEqual(0);
  return JSON.parse(lines.slice(start, end + 1).join("\n"));
}

/** What `deadlift server status` shows for one App: its summary line and its Environments. */
async function statusOf(app: string): Promise<string[] | undefined> {
  const result = await deadlift("server", "status", "localhost");
  expect(result.code, result.output).toBe(0);
  const lines = result.stdout.split("\n");
  const start = lines.findIndex((line) => line.startsWith(`${app}: `));
  if (start < 0) return undefined;
  const end = lines.findIndex((line, i) => i > start && !line.startsWith("  "));
  return lines.slice(start, end < 0 ? undefined : end);
}

/** The block of loopback ports `server status` shows for an App. */
async function portBlock(app: string): Promise<[number, number]> {
  const [summary = ""] = (await statusOf(app)) ?? [];
  const match = /ports (\d+)-(\d+)/.exec(summary);
  expect(match, summary).not.toBeNull();
  return [Number(match?.[1]), Number(match?.[2])];
}

/** Delivers `template` to an App's Dev Environment as its CI would, through `deliver`, over real
 * SSH as the App's deploy user. */
async function deliverDev(app: string, template: string): Promise<Result> {
  const job = await mkdtemp(join(work, "job-"));
  await writeFile(join(job, "compose.yaml"), template);
  const result = await run(
    join(repo, "actions/deliver/deliver.sh"),
    ["--server", "localhost", "--app", app, "deploy", "dev", "--image", "web=alpine:3.20"],
    { cwd: job, env: { ...maintainerEnv(), DEADLIFT_REGISTRY_TOKEN: "test-registry-token" } },
  );
  await rm(job, { recursive: true, force: true });
  return result;
}

/** A template whose routed service idles, healthy, with a named volume and a secret in its
 * environment. */
function template(version: number): string {
  return `services:
  web:
    image: \${DEADLIFT_IMAGE_WEB}
    command: ["sh", "-c", "trap 'exit 0' TERM; sleep 600 & wait"]
    environment:
      GREETING: \${GREETING:-unset}
      VERSION: "${version}"
    volumes:
      - data:/data
    healthcheck:
      test: ["CMD", "true"]
      interval: 1s
      retries: 3
volumes:
  data:
x-deadlift:
  route: web:8080
`;
}

/** Every request that changed something through the Tailscale API. */
function apiWrites() {
  return api.requests.filter((request) => request.method !== "GET");
}

beforeAll(async () => {
  const why = whyNotThrowaway();
  if (why) throw new Error(`Refusing to run the server-setup tests: ${why}.`);
  work = await mkdtemp(join(tmpdir(), `${prefix}-`));
  await ok("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", join(work, "key"));
  await ok("sudo", "bash", join(repo, "test/server-setup/prepare.sh"), join(work, "key.pub"));
  await mkdir(join(work, "bin"));
  await writeFile(
    join(work, "bin/ssh"),
    `#!/bin/sh\nexec /usr/bin/ssh -i ${join(work, "key")} -o IdentitiesOnly=yes -o UserKnownHostsFile=${join(work, "known_hosts")} "$@"\n`,
  );
  await chmod(join(work, "bin/ssh"), 0o755);
  api = await startFakeTailscaleApi();
  // A Server to add Apps to. Bootstrap changes nothing when bootstrap.test.ts ran first.
  const bootstrap = await deadlift("server", "bootstrap", "localhost");
  expect(bootstrap.code, bootstrap.output).toBe(0);
  await ok("docker", "pull", "--quiet", "alpine:3.20");
});

afterAll(async () => {
  if (api) {
    for (const app of [appA, appB]) await deadlift("app", "remove", app, "--server", "localhost", "--yes");
    await api.close();
  }
  if (work) await rm(work, { recursive: true, force: true });
});

describe("deadlift app add", () => {
  let firstStdout = "";

  test("gives an App its deploy user, its one sudo rule, its Service and its policy entries", async () => {
    const result = await addA();

    expect(result.code, result.output).toBe(0);
    firstStdout = result.stdout;
    // The deploy user, outside the docker group.
    const groups = (await ok("id", "-nG", `deadlift-${appA}`)).split(" ");
    expect(groups).not.toContain("docker");
    // Its one sudo rule, for the entry command and nothing else, and sudo's rules all still parse.
    const sudo = await ok("sudo", "-l", "-U", `deadlift-${appA}`);
    const commands = sudo.slice(sudo.indexOf("may run the following commands")).split("\n").slice(1);
    expect(commands.map((line) => line.trim()).filter(Boolean)).toEqual([
      "(root) NOPASSWD: /usr/local/sbin/deadlift-entry",
    ]);
    expect((await run("sudo", ["visudo", "-c"])).code).toBe(0);
    // Its Service, through the API with the maintainer's credential, tagged for deadlift and
    // listing Dev's 443 and each of its 2 Slots' ports.
    const service = api.services.get(`svc:${appA}`);
    expect(service?.tags).toEqual(["tag:deadlift-svc"]);
    expect(service?.ports.toSorted()).toEqual(["tcp:10001", "tcp:10002", "tcp:443"]);
    expect(apiWrites().every((request) => request.authorization === `Bearer ${fakeApiKey}`)).toBe(true);
    // Only the App's own policy entries: its CI tag, that tag's grant to the Server's SSH port,
    // and its SSH rule as the deploy user. Nothing for the Service.
    expect(printedPolicy(result.stdout)).toEqual({
      tagOwners: { [`tag:deadlift-ci-${appA}`]: ["autogroup:admin"] },
      grants: [{ src: [`tag:deadlift-ci-${appA}`], dst: ["tag:deadlift-server"], ip: ["tcp:22"] }],
      ssh: [
        {
          action: "accept",
          src: [`tag:deadlift-ci-${appA}`],
          dst: ["tag:deadlift-server"],
          users: [`deadlift-${appA}`],
        },
      ],
    });
    // The federated credential its CI joins with, and the variables deliver reads.
    expect(result.stdout).toContain("https://token.actions.githubusercontent.com");
    expect(result.stdout).toContain("repo:owner/app-a:*");
    expect(result.stdout).toMatch(new RegExp(`Auth Keys: Write, with the tag tag:deadlift-ci-${appA}`));
    expect(result.stdout).toContain("gh variable set TS_OAUTH_CLIENT_ID --repo owner/app-a");
    expect(result.stdout).toContain("gh variable set TS_AUDIENCE --repo owner/app-a");
  });

  test("gives a second App a block of ports that doesn't overlap the first's", async () => {
    const result = await deadlift("app", "add", appB, "--server", "localhost", "--repo", "owner/app-b");

    expect(result.code, result.output).toBe(0);
    const [aFrom, aTo] = await portBlock(appA);
    const [bFrom, bTo] = await portBlock(appB);
    expect(aTo - aFrom).toBe(99);
    expect(bTo - bFrom).toBe(99);
    expect(bTo < aFrom || bFrom > aTo).toBe(true);
    // The default 3 Slots.
    expect(api.services.get(`svc:${appB}`)?.ports.toSorted()).toEqual([
      "tcp:10001",
      "tcp:10002",
      "tcp:10003",
      "tcp:443",
    ]);
  });

  test("changes nothing when run again", async () => {
    const writes = apiWrites().length;
    const marker = join(work, "marker");
    await writeFile(marker, "");

    const result = await addA();

    expect(result.code, result.output).toBe(0);
    const changed = await ok(
      "sudo", "find", "/etc", "/srv/deadlift", `/home/deadlift-${appA}`, "-xdev", "-newer", marker,
    );
    expect(changed).toBe("");
    expect(apiWrites().length).toBe(writes);
    expect(result.stdout).toBe(firstStdout);
  });

  test("refuses an App name that could clash with another App's Environments", async () => {
    const result = await deadlift("app", "add", `${appA}-feature`, "--server", "localhost", "--repo", "owner/x");

    expect(result.code).toBe(2);
    expect(reason(result)).toMatch(/isn't an App name/);
    expect(await statusOf(`${appA}-feature`)).toBeUndefined();
  });
});

describe("an App's settings and secrets on the Server", () => {
  test("reach its Environments' deploys", async () => {
    // The deploy user logs in with the test's key, as Tailscale SSH would let its CI.
    const home = `/home/deadlift-${appA}`;
    await ok("sudo", "install", "-d", "-m", "0700", "-o", `deadlift-${appA}`, "-g", `deadlift-${appA}`, `${home}/.ssh`);
    await ok("sudo", "install", "-m", "0600", "-o", `deadlift-${appA}`, "-g", `deadlift-${appA}`, join(work, "key.pub"), `${home}/.ssh/authorized_keys`);
    const secrets = join(work, "dev.env");
    await writeFile(secrets, "GREETING=hello from app secrets\n");

    const set = await deadlift("app", "secrets", appA, "dev", secrets, "--server", "localhost");
    expect(set.code, set.output).toBe(0);
    const first = await deliverDev(appA, template(1));
    expect(first.code, first.output).toBe(0);
    const second = await deliverDev(appA, template(2));
    expect(second.code, second.output).toBe(0);

    const container = `deadlift-${appA}-dev-web-1`;
    expect(await ok("docker", "exec", container, "printenv", "GREETING")).toBe("hello from app secrets");
    // The limits app add was given, on the App's container.
    const limits = await ok(
      "docker", "inspect", "--format", "{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}}", container,
    );
    expect(limits).toBe(`${256 * 1024 * 1024} 500000000 100`);
    // Two Dev deploys, and only the 1 Volume Snapshot app add said to keep.
    const status = (await statusOf(appA)) ?? [];
    expect(status[0]).toMatch(/Slots in use: 0 of 2, Volume Snapshots: 1$/);
    expect(status.find((line) => line.trim().startsWith("dev "))).toMatch(
      new RegExp(`1 of 1 running.*https://${appA}\\.tailtest\\.ts\\.net/`),
    );
  });
});

describe("deadlift app remove", () => {
  test("refuses without --yes, changing nothing", async () => {
    const result = await deadlift("app", "remove", appA, "--server", "localhost");

    expect(result.code).toBe(2);
    expect(reason(result)).toMatch(/--yes/);
    expect(await statusOf(appA)).toBeDefined();
  });

  test("leaves nothing of the App, and the other App as it was", async () => {
    const result = await deadlift("app", "remove", appA, "--server", "localhost", "--yes");

    expect(result.code, result.output).toBe(0);
    const byLabel = ["--filter", `label=deadlift.app=${appA}`, "--quiet"];
    expect(await ok("docker", "container", "ls", "--all", ...byLabel)).toBe("");
    expect(await ok("docker", "volume", "ls", ...byLabel)).toBe("");
    expect(await ok("docker", "network", "ls", "--quiet", "--filter", `name=deadlift-${appA}-`)).toBe("");
    expect((await run("id", [`deadlift-${appA}`])).code).not.toBe(0);
    expect(await ok("sudo", "sh", "-c", `grep -rl deadlift-${appA} /etc/sudoers /etc/sudoers.d || true`)).toBe("");
    expect(api.services.has(`svc:${appA}`)).toBe(false);
    const serve = JSON.parse(await ok("sudo", "tailscale", "serve", "status", "--json")) as {
      Services?: Record<string, unknown>;
    };
    expect(Object.keys(serve.Services ?? {})).not.toContain(`svc:${appA}`);
    // Its Environments and Volume Snapshots are gone with it, and the other App is still there.
    expect(await statusOf(appA)).toBeUndefined();
    expect(await statusOf(appB)).toBeDefined();
    expect(api.services.has(`svc:${appB}`)).toBe(true);
  });

  test("is done, changing nothing, when the App is gone already", async () => {
    const result = await deadlift("app", "remove", appA, "--server", "localhost", "--yes");

    expect(result.code, result.output).toBe(0);
    expect(reason(result)).toMatch(/nothing to remove|no Tailscale Service/);
  });
});
