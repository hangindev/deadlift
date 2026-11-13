// The Sweep: destroying every Feature Environment of an App that isn't in a list to keep, on its
// own or first thing in a Feature deploy.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { get, startHarness } from "./harness.ts";

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

describe("what a Sweep never touches", () => {
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
