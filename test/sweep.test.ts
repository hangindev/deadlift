// The Sweep: destroying every Feature Environment of an App that isn't in a list to keep, on its
// own or first thing in a Feature deploy.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { get, putNote, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

type Server = Awaited<ReturnType<typeof harness.server>>;

/** Delivers the example App, at `version`, to the Feature Environment `key` of `app`. */
async function deploy(server: Server, key: string, extra: string[] = [], app = server.apps.a.name) {
  return server.deliver(app, [
    "deploy",
    `feature-${key}`,
    "--image",
    `web=${await harness.image()}`,
    ...extra,
  ]);
}

function sweep(server: Server, ...args: string[]) {
  return server.deliver(server.apps.a.name, ["sweep", ...args]);
}

describe("sweep --keep <keys>", () => {
  test("destroys every Feature Environment not listed, and reports the swept keys", async () => {
    const server = await harness.server({ slots: 3 });
    const app = server.apps.a.name;
    await deploy(server, "first");
    await deploy(server, "second");
    await deploy(server, "third");

    const result = await sweep(server, "--keep", "second,other");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.swept).toBe("first,third");
    expect(result.reason).toMatch(/^deadlift: swept feature-first and feature-third/);
    expect(result.summary).toMatch(/Swept: first, third/);
    const containers = await server.containers(app);
    expect(containers).not.toMatch(/feature-first|feature-third/);
    expect(await get(server.slotPort("a", 2), "/version")).toBe("1");
    expect(await server.serveMappings()).toEqual([
      { service: `svc:${app}`, port: 10002, target: `http://127.0.0.1:${server.slotPort("a", 2)}` },
    ]);
  });

  test("is done, sweeping nothing, when every Feature Environment is kept", async () => {
    const server = await harness.server();
    await deploy(server, "first");

    const result = await sweep(server, "--keep", "first");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.swept).toBe("");
    expect(result.reason).toMatch(/^deadlift: swept nothing/);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test.each(["Upper", "under_score", "a".repeat(31)])(
    "refuses a kept key that isn't one, exit 2, sweeping nothing: '%s'",
    async (key) => {
      const server = await harness.server();
      await deploy(server, "first");

      const result = await sweep(server, "--keep", `other,${key}`);

      expect(result.code, result.output).toBe(2);
      expect(result.reason).toMatch(/^deadlift: .*key/);
      expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
    },
  );
});

describe("the guard on an empty keep list", () => {
  test.each([
    ["no list at all", []],
    ["an empty --keep", ["--keep", ""]],
    ["a --keep of only commas", ["--keep", ",,"]],
  ])("refuses %s, exit 2, sweeping nothing", async (_, args) => {
    const server = await harness.server();
    await deploy(server, "first");

    const result = await sweep(server, ...args);

    expect(result.code, result.output).toBe(2);
    expect(result.outcome).toBe("failed");
    expect(result.reason).toMatch(/^deadlift: .*--keep-none/);
    expect(result.swept).toBe("");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("--keep-none destroys every Feature Environment of the App", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    await deploy(server, "first");
    await deploy(server, "second");

    const result = await sweep(server, "--keep-none");

    expect(result.code, result.output).toBe(0);
    expect(result.swept).toBe("first,second");
    expect(await server.containers(app)).toBe("");
    expect(await server.serveMappings()).toEqual([]);
  });

  test("refuses both --keep and --keep-none, exit 2, sweeping nothing", async () => {
    const server = await harness.server();
    await deploy(server, "first");

    const result = await sweep(server, "--keep", "other", "--keep-none");

    expect(result.code, result.output).toBe(2);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });
});

describe("deploy feature-<key> --keep <keys>", () => {
  test("sweeps unlisted Feature Environments first, so a full set of stale ones doesn't skip it", async () => {
    const server = await harness.server({ slots: 2 });
    const app = server.apps.a.name;
    await deploy(server, "stale");
    await deploy(server, "open");

    const result = await deploy(server, "new", ["--keep", "open,new"]);

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.environment).toBe("feature-new");
    expect(result.swept).toBe("stale");
    expect(result.reason).toMatch(/^deadlift: deployed feature-new.*swept feature-stale/);
    // The stale one's Slot, at its address.
    expect(result.url).toBe(`https://${app}.tailtest.ts.net:10001/`);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
    expect(await get(server.slotPort("a", 2), "/version")).toBe("1");
    expect(await server.containers(app)).not.toMatch(/feature-stale/);
  });

  test("always keeps the Environment it deploys, even when it isn't listed", async () => {
    const server = await harness.server();
    await deploy(server, "first");
    await deploy(server, "second");

    const result = await deploy(server, "second", ["--keep-none"]);

    expect(result.outcome, result.output).toBe("done");
    expect(result.swept).toBe("first");
    expect(result.url).toMatch(/:10002\/$/);
    expect(await get(server.slotPort("a", 2), "/version")).toBe("1");
  });

  test("is skipped, sweeping nothing, when every Slot is held by kept keys", async () => {
    const server = await harness.server({ slots: 1 });
    await deploy(server, "first");

    const result = await deploy(server, "second", ["--keep", "first"]);

    expect(result.outcome, result.output).toBe("skipped");
    expect(result.swept).toBe("");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("sweeps nothing when the template is refused", async () => {
    const server = await harness.server();
    await deploy(server, "stale");

    const result = await server.deliver(
      server.apps.a.name,
      ["deploy", "feature-new", "--image", `web=${await harness.image()}`, "--keep", "new"],
      { template: "services:\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n" },
    );

    expect(result.code, result.output).toBe(2);
    expect(result.swept).toBe("");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("refuses an empty --keep, exit 2, sweeping and deploying nothing", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    await deploy(server, "stale");

    const result = await deploy(server, "new", ["--keep", ""]);

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: .*--keep-none/);
    expect(await server.containers(app)).not.toMatch(/feature-new/);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });
});

describe("what a Sweep never touches", () => {
  test("the Dev Environment, or its data", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    const dev = await server.deliver(app, ["deploy", "dev", "--image", `web=${await harness.image()}`]);
    expect(dev.outcome, dev.output).toBe("done");
    await putNote(server.devPort("a"), "kept");
    await deploy(server, "first");

    const result = await sweep(server, "--keep-none");

    expect(result.outcome, result.output).toBe("done");
    expect(result.swept).toBe("first");
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await get(server.devPort("a"), "/note")).toBe("kept");
    expect(await server.serveMappings()).toEqual([
      { service: `svc:${app}`, port: 443, target: `http://127.0.0.1:${server.devPort("a")}` },
    ]);
  });

  test("deploy dev refuses --keep and --keep-none, exit 2, sweeping nothing", async () => {
    const server = await harness.server();
    await deploy(server, "first");

    for (const keep of [["--keep", "other"], ["--keep-none"]]) {
      const result = await server.deliver(server.apps.a.name, [
        "deploy",
        "dev",
        "--image",
        `web=${await harness.image()}`,
        ...keep,
      ]);

      expect(result.code, result.output).toBe(2);
      expect(result.reason).toMatch(/^deadlift: .*Feature deploy/);
    }
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("another App's Feature Environments, even with keys the App doesn't keep", async () => {
    const server = await harness.server();
    const b = server.apps.b.name;
    await deploy(server, "first");
    await deploy(server, "first", [], b);
    await deploy(server, "second", [], b);

    const result = await sweep(server, "--keep-none");

    expect(result.swept).toBe("first");
    expect(await get(server.slotPort("b", 1), "/version")).toBe("1");
    expect(await get(server.slotPort("b", 2), "/version")).toBe("1");
    expect((await server.serveMappings()).map(({ service, port }) => `${service}:${port}`)).toEqual([
      `svc:${b}:10001`,
      `svc:${b}:10002`,
    ]);
  });
});
