// Seam A on a bootstrapped Server: what an App's containers can reach, and who owns what they
// write (ADR 0001). Only a Server that `deadlift server bootstrap` set up has the firewall and
// userns-remap, so these run in CI on the runner Seam B bootstrapped, and are skipped elsewhere.
// They change the machine's network for their length, so they also need a throwaway machine.
//
// DEADLIFT_TEST_BOOTSTRAPPED=true, as the CI job sets after Seam B, makes them required: they
// fail instead of being skipped when the machine isn't a bootstrapped Server.
import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createServer, connect, type Server } from "node:net";
import { networkInterfaces } from "node:os";
import { promisify } from "node:util";
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { docker, startHarness } from "./harness.ts";
import { whyNotThrowaway } from "./support/throwaway.ts";

const exec = promisify(execFile);
const outside = new URL("support/outside.sh", import.meta.url).pathname;

const remapped = await docker("info", "--format", "{{json .SecurityOptions}}").then(
  (options) => options.includes("name=userns"),
  () => false,
);
const why = whyNotThrowaway() ?? (remapped ? undefined : "Docker here doesn't run with userns-remap");
const required = process.env.DEADLIFT_TEST_BOOTSTRAPPED === "true";

test.runIf(required)("this machine is a bootstrapped Server, as CI says", () => {
  expect(why).toBeUndefined();
});

/** Where something answers TCP, from the host's side or a container's. */
type Target = { host: string; port: number };

/** "reached", "timeout" (dropped) or the connection's error code, as seen from the host. */
function reachFromHost({ host, port }: Target): Promise<string> {
  return new Promise((resolve) => {
    const socket = connect({ host, port, timeout: 3000 });
    socket.on("connect", () => {
      socket.destroy();
      resolve("reached");
    });
    socket.on("timeout", () => {
      socket.destroy();
      resolve("timeout");
    });
    socket.on("error", (error: NodeJS.ErrnoException) => resolve(error.code ?? "error"));
  });
}

// The same, run by Node inside a container, for every target at once.
const probe = `
const net = require("node:net");
const targets = JSON.parse(process.argv[1]);
Promise.all(targets.map(({ host, port }) => new Promise((resolve) => {
  const socket = net.connect({ host, port, timeout: 3000 });
  socket.on("connect", () => { socket.destroy(); resolve("reached"); });
  socket.on("timeout", () => { socket.destroy(); resolve("timeout"); });
  socket.on("error", (error) => resolve(error.code || "error"));
}))).then((results) => console.log(JSON.stringify(results)));
`;

describe.skipIf(why !== undefined)("an App's containers on a bootstrapped Server", () => {
  let harness: Awaited<ReturnType<typeof startHarness>>;
  let server: Awaited<ReturnType<typeof harness.server>>;
  let app = "";
  let listener: Server;
  let hostPort = 0;
  const outsidePort = 18000 + Math.floor(Math.random() * 1000);
  const outsideName = randomBytes(3).toString("hex");

  /** Runs the probe in the App's routed container, as the App's own code could. */
  async function reachFromContainer(targets: Target[]): Promise<string[]> {
    const web = (await server.containers(app)).split("\n").find((name) => name.includes("-web-"));
    if (!web) throw new Error(`no web container among ${await server.containers(app)}`);
    return JSON.parse(await docker("exec", web, "node", "-e", probe, JSON.stringify(targets)));
  }

  beforeAll(async () => {
    harness = await startHarness();
    server = await harness.server();
    app = server.apps.a.name;
    await exec("sudo", ["bash", outside, "up", outsideName, String(outsidePort)]);
    listener = createServer((socket) => socket.end("the host\n"));
    await new Promise<void>((resolve) => listener.listen(0, "0.0.0.0", resolve));
    const address = listener.address();
    if (typeof address !== "object" || address === null) throw new Error("no port");
    hostPort = address.port;
    const result = await server.deliver(app, [
      "deploy",
      "feature-42",
      "--image",
      `web=${await harness.image()}`,
    ]);
    expect(result.outcome, result.output).toBe("done");
  });

  afterAll(async () => {
    listener?.close();
    await exec("sudo", ["bash", outside, "down", outsideName]).catch(() => undefined);
    await harness?.remove();
  });

  test("can't reach any of the host's own addresses", async () => {
    const targets = Object.values(networkInterfaces())
      .flat()
      .filter((address) => address && address.family === "IPv4" && !address.internal)
      .map((address) => ({ host: address!.address, port: hostPort }));
    // Among them, a tailnet address of the host's own, as tailscale0 has on a Server.
    expect(targets.map((target) => target.host)).toContain("100.80.214.1");
    // The host's listener answers on every one of them...
    for (const target of targets) expect(await reachFromHost(target), target.host).toBe("reached");

    // ...and a container's attempts are dropped.
    const results = await reachFromContainer(targets);

    expect(Object.fromEntries(targets.map((target, i) => [target.host, results[i]]))).toEqual(
      Object.fromEntries(targets.map((target) => [target.host, "timeout"])),
    );
  });

  test("can't reach the tailnet, 169.254.169.254 or private ranges beyond the host", async () => {
    const targets = [
      { host: "100.80.213.2", port: outsidePort },
      { host: "169.254.169.254", port: outsidePort },
      { host: "10.213.0.2", port: outsidePort },
      { host: "172.16.213.2", port: outsidePort },
      { host: "192.168.213.2", port: outsidePort },
    ];
    // Each answers the host, so only the firewall stands between it and a container.
    for (const target of targets) expect(await reachFromHost(target), target.host).toBe("reached");

    const results = await reachFromContainer(targets);

    expect(Object.fromEntries(targets.map((target, i) => [target.host, results[i]]))).toEqual(
      Object.fromEntries(targets.map((target) => [target.host, "timeout"])),
    );
  });

  test("can reach the internet", async () => {
    expect(await reachFromContainer([{ host: "github.com", port: 443 }])).toEqual(["reached"]);
  });

  test("a file root makes in a container is owned by an unprivileged user on the host", async () => {
    const web = (await server.containers(app)).split("\n").find((name) => name.includes("-web-"))!;
    await docker("exec", "--user", "root", web, "touch", "/data/made-by-root");
    expect(await docker("exec", web, "stat", "-c", "%u", "/data/made-by-root")).toBe("0");

    const volume = `deadlift-${app}-feature-42_data`;
    const mountpoint = await docker("volume", "inspect", "--format", "{{.Mountpoint}}", volume);
    const { stdout: owner } = await exec("sudo", ["stat", "-c", "%u", `${mountpoint}/made-by-root`]);
    // Root in a container is the first of the user ids the Server gave Docker's remap user.
    const { stdout: subuid } = await exec("grep", ["^dockremap:", "/etc/subuid"]);
    expect(Number(owner.trim())).not.toBe(0);
    expect(owner.trim()).toBe(subuid.split(":")[1]);
  });
});
