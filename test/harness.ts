// The delivery tests: a delivery end to end. A test delivers as an App's CI would, through the
// `deliver` client script, to a fake Server: an `ssh` shim logs in as the App's deploy user, a fake
// `sudo` allows only the entry command, and the entry command runs the deploy script installed on
// the fake Server against real Docker. Around it: a fake `tailscale`, a local registry holding the
// example App's images, and a temporary state directory set up for two Apps.
//
// This machine may be shared, and several runs may overlap. Everything a run creates carries one
// random prefix, `deadlift-test-<hex>`, and cleanup removes only what that prefix names. Ports
// are picked free at runtime.
import { execFile, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  chmod,
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);
const repo = new URL("..", import.meta.url).pathname;
const fakes = join(repo, "test/fakes");
const exampleApp = join(repo, "test/example-app");
const registryImage =
  "registry:3@sha256:38907510915c3f1604c7e83d6ab602ab3020d092f73bdaed8696773693ff768c";

export async function docker(...args: string[]): Promise<string> {
  const { stdout } = await run("docker", args, { maxBuffer: 64 * 1024 * 1024 });
  return stdout.trim();
}

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  await new Promise((resolve) => server.close(resolve));
  if (typeof address !== "object" || address === null) throw new Error("no port");
  return address.port;
}

/** Whether a loopback port is free right now. */
async function isFree(port: number): Promise<boolean> {
  const server = createServer();
  const listening = await new Promise<boolean>((resolve) => {
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => resolve(true));
  });
  if (listening) await new Promise((resolve) => server.close(resolve));
  return listening;
}

/** A block of 100 loopback ports, as `deadlift app add` gives an App, whose Environments' ports
 * (the base, and base + 10s for each Slot s) are all free and not in `taken`. */
async function freePortBlock(taken: number[]): Promise<number> {
  for (;;) {
    const base = 30000 + 100 * Math.floor(Math.random() * 300);
    if (taken.includes(base)) continue;
    const ports = Array.from({ length: 10 }, (_, s) => base + 10 * s);
    if ((await Promise.all(ports.map(isFree))).every(Boolean)) return base;
  }
}

async function requireTools() {
  for (const tool of ["flock", "jq"]) {
    try {
      await run("sh", ["-c", `command -v ${tool}`]);
    } catch {
      throw new Error(`The delivery tests need ${tool}, which every Server has`);
    }
  }
  try {
    await run("python3", ["-I", "-c", "import yaml"]);
  } catch {
    throw new Error(
      "The delivery tests need python3 with PyYAML (python3-yaml), which every Server has",
    );
  }
}

/** The example App's images: healthy, one whose health check never passes, and one whose
 * migration fails. */
export type ImageKind = "healthy" | "unhealthy" | "failing-migration";

export type Result = { code: number | null; output: string };

/** What `deliver` reports, as an App's workflow reads it. */
export type Delivery = Result & {
  outcome?: string;
  reason?: string;
  url?: string;
  environment?: string;
  swept?: string;
  /** What it wrote to the job summary. */
  summary: string;
};

/** An App's limits for each of its containers, as `deadlift app add` sets them: memory as Docker
 * writes it (`512m`), CPUs, and processes. */
export type Limits = { memory?: string; cpus?: string; pids?: number };

export type ServeMapping ={ service: string; port: number; target: string };

/** Runs a command, collecting its stdout and stderr together. */
function collect(
  command: string,
  args: string[],
  options: { env: NodeJS.ProcessEnv; cwd?: string; input?: Buffer },
): Promise<Result> {
  const child = spawn(command, args, { env: options.env, cwd: options.cwd });
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk;
  });
  child.stderr.on("data", (chunk) => {
    output += chunk;
  });
  child.stdin.on("error", () => {
    // The command may refuse before reading all its input.
  });
  child.stdin.end(options.input ?? Buffer.alloc(0));
  return new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, output }));
  });
}

/** Parses what a step wrote to $GITHUB_OUTPUT, in its `name=value` and heredoc forms. */
function parseOutputs(text: string): Record<string, string> {
  const outputs: Record<string, string> = {};
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    const heredoc = /^([^=<]+)<<(.+)$/.exec(line);
    if (heredoc) {
      const [, name = "", marker] = heredoc;
      const value: string[] = [];
      for (i++; i < lines.length && lines[i] !== marker; i++) value.push(lines[i] ?? "");
      outputs[name] = value.join("\n");
    } else if (line.includes("=")) {
      outputs[line.slice(0, line.indexOf("="))] = line.slice(line.indexOf("=") + 1);
    }
  }
  return outputs;
}

/** The example App's Compose template, as its repo holds it. */
export function exampleTemplate(): Promise<string> {
  return readFile(join(exampleApp, "compose.yaml"), "utf8");
}

/** One test run: a local registry and the example App's images in it. Call `remove` when done. */
export async function startHarness() {
  await requireTools();
  const prefix = `deadlift-test-${randomBytes(4).toString("hex")}`;
  const root = await mkdtemp(join(tmpdir(), `${prefix}-`));
  // The registry shares the daemon's network and listens on its loopback, which Docker trusts
  // without TLS. Its debug server would take :5001, so a second run couldn't start. On a
  // bootstrapped Server, Docker remaps users, and shares the host's network only with a container
  // that opts out of the remap.
  const registryPort = await freePort();
  const registryDebugPort = await freePort();
  await docker(
    "run",
    "--detach",
    "--name",
    `${prefix}-registry`,
    "--userns",
    "host",
    "--network",
    "host",
    "--env",
    `REGISTRY_HTTP_ADDR=127.0.0.1:${registryPort}`,
    "--env",
    `REGISTRY_HTTP_DEBUG_ADDR=127.0.0.1:${registryDebugPort}`,
    registryImage,
  );
  const repository = `127.0.0.1:${registryPort}/${prefix}/example`;
  const images = new Map<string, Promise<string>>();
  const servers: Array<() => Promise<void>> = [];
  let serverCount = 0;

  /** Builds an image and pushes it, leaving no local copy, so a deploy must pull it. */
  async function build(kind: ImageKind, version: number) {
    const ref = `${repository}:v${version}-${kind}`;
    await docker(
      "build",
      "--quiet",
      "--build-arg",
      `VERSION=${version}`,
      "--build-arg",
      `HEALTH=${kind === "unhealthy" ? "fail" : "ok"}`,
      "--build-arg",
      `MIGRATE=${kind === "failing-migration" ? "fail" : "ok"}`,
      "--tag",
      ref,
      exampleApp,
    );
    // Retried: a fresh registry can refuse the first push for a moment.
    for (let attempt = 1; ; attempt++) {
      try {
        await docker("push", "--quiet", ref);
        break;
      } catch (error) {
        if (attempt === 5) throw error;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    await docker("image", "rm", "--force", ref);
    return ref;
  }

  /** A reference to one of the example App's images, built and pushed on first use. */
  function image(kind: ImageKind = "healthy", version = 1): Promise<string> {
    const key = `${kind}-${version}`;
    let ref = images.get(key);
    if (!ref) {
      ref = build(kind, version);
      images.set(key, ref);
    }
    return ref;
  }

  /** A fresh Server holding two Apps, `a` and `b`, with no Environments yet. Each App has
   * `slots` Slots and, when given, the number of Volume Snapshots to keep and the per-container
   * `limits` `deadlift app add` sets. Its disk is `diskUsed` percent full, 50 unless given, in
   * place of measuring this machine's, which may be fuller than a Server's thresholds allow.
   * `settings` is the Server-wide settings file as bootstrap writes it; without it there's none. */
  async function server(
    options: {
      slots?: number;
      snapshots?: number;
      limits?: Limits;
      diskUsed?: number;
      settings?: string;
    } = {},
  ) {
    const n = ++serverCount;
    const dir = join(root, `server-${n}`);
    const state = join(dir, "srv");
    const bin = join(dir, "bin");
    const clientBin = join(dir, "client-bin");
    // What bootstrap installs: the entry command, the deploy script and the template check
    // beside it, never delivered.
    const sbin = join(dir, "sbin");
    const lib = join(dir, "lib");
    for (const d of [state, bin, clientBin, sbin, lib, join(dir, "home")]) {
      await mkdir(d, { recursive: true });
    }
    await copyFile(join(repo, "server/entry.sh"), join(sbin, "deadlift-entry"));
    await copyFile(join(repo, "server/deploy.sh"), join(lib, "deploy.sh"));
    await copyFile(join(repo, "server/template.sh"), join(lib, "template.sh"));
    await copyFile(join(fakes, "sudo"), join(bin, "sudo"));
    await copyFile(join(fakes, "tailscale"), join(bin, "tailscale"));
    await copyFile(join(fakes, "ssh"), join(clientBin, "ssh"));
    for (const file of [
      join(sbin, "deadlift-entry"),
      join(lib, "deploy.sh"),
      join(lib, "template.sh"),
      join(bin, "sudo"),
      join(bin, "tailscale"),
      join(clientBin, "ssh"),
    ]) {
      await chmod(file, 0o755);
    }
    /** Sets how full the fake Server's disk is, in percent, as the deploy script will read it. */
    async function setDiskUsed(percent: number) {
      await writeFile(
        join(dir, "server.env"),
        `DEADLIFT_STATE_DIR=${state}\nDEADLIFT_LIB_DIR=${lib}\nDEADLIFT_DISK_USED=${percent}\n`,
      );
    }
    await setDiskUsed(options.diskUsed ?? 50);
    if (options.settings !== undefined) await writeFile(join(state, "settings"), options.settings);

    // What `deadlift app add` gives each App: its own block of ports, its Slots and its limits.
    const apps: Record<"a" | "b", { name: string; portBase: number }> = {
      a: { name: `${prefix.slice("deadlift-".length)}-${n}a`, portBase: 0 },
      b: { name: `${prefix.slice("deadlift-".length)}-${n}b`, portBase: 0 },
    };
    const limits = Object.entries(options.limits ?? {})
      .map(([name, value]) => `${name}=${value}\n`)
      .join("");
    for (const app of Object.values(apps)) {
      app.portBase = await freePortBlock(Object.values(apps).map((other) => other.portBase));
      await mkdir(join(state, "apps", app.name), { recursive: true });
      await writeFile(
        join(state, "apps", app.name, "app.conf"),
        `port_base=${app.portBase}\nslots=${options.slots ?? 3}\n${limits}` +
          (options.snapshots ? `snapshots=${options.snapshots}\n` : ""),
      );
    }

    /** Removes every Docker object of this Server's Apps: their Compose projects are all named
     * deadlift-<app>-<environment>. */
    async function cleanup() {
      for (const app of Object.values(apps)) {
        const byName = ["--filter", `name=^deadlift-${app.name}-`];
        await removeListed(["ps", "--all", ...byName], ["rm", "--force", "--volumes"]);
        await removeListed(["volume", "ls", ...byName], ["volume", "rm"]);
        await removeListed(["network", "ls", ...byName], ["network", "rm"]);
      }
      await rm(dir, { recursive: true, force: true });
    }
    servers.push(cleanup);

    const clientEnv = (extra: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv => ({
      PATH: `${clientBin}:${process.env.PATH}`,
      HOME: process.env.HOME,
      ...(process.env.DOCKER_HOST ? { DOCKER_HOST: process.env.DOCKER_HOST } : {}),
      DEADLIFT_TEST_SERVER: dir,
      ...extra,
    });

    /**
     * Runs `deliver` as the App's CI job would, from a checkout holding `template` as its
     * compose.yaml (the example App's by default), and returns what it reported.
     */
    async function deliver(
      app: string,
      args: string[],
      options: { template?: string; token?: string } = {},
    ): Promise<Delivery> {
      const job = await mkdtemp(join(dir, "job-"));
      await writeFile(join(job, "compose.yaml"), options.template ?? (await exampleTemplate()));
      const outputFile = join(job, "github-output");
      await writeFile(outputFile, "");
      const summaryFile = join(job, "step-summary");
      await writeFile(summaryFile, "");
      // As the deliver action runs it.
      const result = await collect(
        "python3",
        [
          "-I",
          join(repo, "actions/deliver/deliver.py"),
          ...["--server", "server.tailtest.ts.net", "--app", app, ...args],
        ],
        {
          cwd: job,
          env: clientEnv({
            GITHUB_OUTPUT: outputFile,
            GITHUB_STEP_SUMMARY: summaryFile,
            DEADLIFT_REGISTRY_TOKEN: options.token ?? "test-registry-token",
          }),
        },
      );
      const outputs = parseOutputs(await readFile(outputFile, "utf8"));
      const summary = await readFile(summaryFile, "utf8");
      await rm(job, { recursive: true, force: true });
      return { ...result, ...outputs, summary };
    }

    /**
     * Calls the entry command over the `ssh` shim as `user`, with `input` on stdin, the way a
     * client other than `deliver` could. `args` follow the entry command.
     */
    function send(user: string, args: string[], input: Buffer): Promise<Result> {
      const command = ["sudo", "--non-interactive", "/usr/local/sbin/deadlift-entry", ...args];
      return collect("ssh", [`${user}@server.tailtest.ts.net`, ...command.map(shellQuote)], {
        env: clientEnv(),
        input,
      });
    }

    /** Puts an App's secrets file for its Dev or Feature Environments on the Server, as
     * `deadlift app secrets <app> dev|feature <file>` does. Returns where it is on the Server. */
    async function secrets(app: string, kind: "dev" | "feature", contents: string) {
      const file = join(state, "apps", app, `${kind}.env`);
      await writeFile(file, contents, { mode: 0o600 });
      return file;
    }

    /** What the fake `tailscale` serves now, for every App's Service. */
    async function serveMappings(): Promise<ServeMapping[]> {
      const { stdout } = await run(join(bin, "tailscale"), ["serve", "status", "--json"]);
      const status = JSON.parse(stdout) as {
        Services?: Record<string, { Web: Record<string, { Handlers: { "/": { Proxy: string } } }> }>;
      };
      return Object.entries(status.Services ?? {}).flatMap(([service, config]) =>
        Object.entries(config.Web).map(([host, web]) => ({
          service,
          port: Number(host.slice(host.lastIndexOf(":") + 1)),
          target: web.Handlers["/"].Proxy,
        })),
      );
    }

    /** Every call the fake `tailscale` got, one per line. */
    async function tailscaleCalls(): Promise<string[]> {
      const log = await readFile(join(dir, "tailscale/calls.log"), "utf8").catch(() => "");
      return log.split("\n").filter(Boolean);
    }

    /** Holds the Server's lock, as another operation in progress would, until released. */
    async function holdLock(): Promise<() => Promise<void>> {
      const holder = spawn("flock", [join(state, "lock"), "cat"], { stdio: ["pipe", "pipe", "inherit"] });
      // flock prints nothing, so wait until the lock is visibly taken.
      for (;;) {
        const free = await run("flock", ["--nonblock", join(state, "lock"), "true"]).then(
          () => true,
          () => false,
        );
        if (!free) break;
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      return () =>
        new Promise<void>((resolve) => {
          holder.on("close", () => resolve());
          holder.stdin.end();
        });
    }

    /** Runs the watchdog as the Server's systemd timer does: the installed deploy script, as
     * root, in a fresh environment. */
    async function watchdog(): Promise<Result> {
      const settings = (await readFile(join(dir, "server.env"), "utf8")).split("\n").filter(Boolean);
      return collect(
        "env",
        [
          "-i",
          `PATH=${bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`,
          ...(process.env.DOCKER_HOST ? [`DOCKER_HOST=${process.env.DOCKER_HOST}`] : []),
          ...settings,
          join(lib, "deploy.sh"),
          "watchdog",
        ],
        { env: {} },
      );
    }

    /** Whether the Server's Docker holds the image `ref`, as `docker image ls` there would show. */
    function hasImage(ref: string): Promise<boolean> {
      return docker("image", "inspect", ref).then(
        () => true,
        () => false,
      );
    }

    /** The names of one App's Docker containers, running or not. */
    function containers(app: string) {
      return docker("ps", "--all", "--format", "{{.Names}}", "--filter", `name=^deadlift-${app}-`);
    }

    /** The names of one App's running Docker containers. */
    function runningContainers(app: string) {
      return docker("ps", "--format", "{{.Names}}", "--filter", `name=^deadlift-${app}-`);
    }

    /** When a container of one App's Environment last started, to tell whether it was touched. */
    function startedAt(app: string, environment: string, service: string) {
      return docker("inspect", "--format", "{{.State.StartedAt}}", `deadlift-${app}-${environment}-${service}-1`);
    }

    /** The Volume Snapshots the Server keeps of one App's Dev Environment, as an operator listing
     * the Server's state would see them. Nothing else shows them yet. */
    async function volumeSnapshots(app: string): Promise<string[]> {
      const dir = join(state, "apps", app, "environments/dev/snapshots");
      return (await readdir(dir).catch(() => [])).sort();
    }

    /** A file as one of `app`'s Volume Snapshots holds it, at `path` in one of Dev's named
     * volumes, as an operator unpacking it would read it. Empty when no volume in it has one. */
    async function fileInVolumeSnapshot(app: string, snapshot: string, path: string): Promise<string> {
      const dir = join(state, "apps", app, "environments/dev/snapshots", snapshot);
      for (const tar of (await readdir(dir)).filter((name) => name.endsWith(".tar"))) {
        const contents = await run("tar", ["-xOf", join(dir, tar), `./${path}`]).then(
          ({ stdout }) => stdout,
          () => undefined,
        );
        if (contents !== undefined) return contents;
      }
      return "";
    }

    return {
      apps,
      startedAt,
      volumeSnapshots,
      fileInVolumeSnapshot,
      /** Where the Dev Environment of `app` listens on the host's loopback: the App's base. */
      devPort: (app: "a" | "b") => apps[app].portBase,
      /** Where Slot `slot` of `app` listens on the host's loopback: the App's base + 10 × slot. */
      slotPort: (app: "a" | "b", slot: number) => apps[app].portBase + 10 * slot,
      deliver,
      send,
      secrets,
      setDiskUsed,
      watchdog,
      hasImage,
      serveMappings,
      tailscaleCalls,
      holdLock,
      containers,
      runningContainers,
    };
  }

  /** Removes every Server made so far. */
  async function removeServers() {
    await Promise.all(servers.splice(0).map((cleanup) => cleanup()));
  }

  /** Removes everything this run made. */
  async function remove() {
    await removeServers();
    await docker("rm", "--force", "--volumes", `${prefix}-registry`).catch(() => "");
    const refs = await docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}", repository);
    if (refs) await docker("image", "rm", "--force", ...refs.split("\n")).catch(() => "");
    await rm(root, { recursive: true, force: true });
  }

  return { image, server, removeServers, remove };
}

/** Removes what Docker lists for one filter, if anything. */
async function removeListed(list: string[], remove: string[]) {
  const ids = await docker(...list, "--quiet");
  if (ids) await docker(...remove, ...ids.split("\n"));
}

/** Quotes a word for the remote shell, as `printf %q` would. */
function shellQuote(word: string): string {
  return /^[\w@%+=:,./-]+$/.test(word) ? word : `'${word.replaceAll("'", "'\\''")}'`;
}

/** A member of a hand-made delivery: a file's contents, or a symlink or directory. */
export type Member = string | { symlink: string } | { directory: true };

/** A tar archive holding `members`, in that order, as a client other than `deliver` could send. */
export async function tarOf(members: Record<string, Member>): Promise<Buffer> {
  const dir = await mkdtemp(join(tmpdir(), "deadlift-test-tar-"));
  try {
    for (const [name, member] of Object.entries(members)) {
      if (typeof member === "string") await writeFile(join(dir, name), member);
      else if ("symlink" in member) await symlink(member.symlink, join(dir, name));
      else await mkdir(join(dir, name));
    }
    const { stdout } = await run("tar", ["-C", dir, "-cf", "-", ...Object.keys(members)], {
      encoding: "buffer",
    });
    return stdout;
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

/** Writes a note through the example App on the host's loopback, into its named volume. */
export async function putNote(port: number, note: string): Promise<void> {
  const response = await fetch(`http://127.0.0.1:${port}/note`, { method: "PUT", body: note });
  if (!response.ok) throw new Error(`PUT /note answered ${response.status}`);
}

/** Fetches a path from an Environment on the host's loopback. */
export async function get(port: number, path: string): Promise<string> {
  const response = await fetch(`http://127.0.0.1:${port}${path}`);
  return response.text();
}
