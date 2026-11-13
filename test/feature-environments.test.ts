// Feature Environments sharing an App's Slots: redeploying a key and running out of Slots.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { get, startHarness, type ImageKind } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

type Server = Awaited<ReturnType<typeof harness.server>>;

/** Delivers the example App, at `version`, to the Feature Environment `key` of App a. */
async function deploy(server: Server, key: string, version = 1, kind: ImageKind = "healthy") {
  return server.deliver(server.apps.a.name, [
    "deploy",
    `feature-${key}`,
    "--image",
    `web=${await harness.image(kind, version)}`,
  ]);
}

describe("Slots", () => {
  test("each Feature Environment takes a free Slot, with its loopback and public ports", async () => {
    const server = await harness.server({ slots: 3 });
    const app = server.apps.a.name;

    const first = await deploy(server, "first", 1);
    const second = await deploy(server, "second", 2);

    expect(first.outcome, first.output).toBe("done");
    expect(second.outcome, second.output).toBe("done");
    expect(first.url).toBe(`https://${app}.tailtest.ts.net:10001/`);
    expect(second.url).toBe(`https://${app}.tailtest.ts.net:10002/`);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
    expect(await get(server.slotPort("a", 2), "/version")).toBe("2");
    expect(await server.serveMappings()).toEqual(
      expect.arrayContaining([
        { service: `svc:${app}`, port: 10001, target: `http://127.0.0.1:${server.slotPort("a", 1)}` },
        { service: `svc:${app}`, port: 10002, target: `http://127.0.0.1:${server.slotPort("a", 2)}` },
      ]),
    );
  });

  test("redeploying a key keeps its Slot, its URL and its data", async () => {
    const server = await harness.server({ slots: 3 });
    await deploy(server, "first", 1);
    const first = await deploy(server, "second", 1);

    const redeployed = await deploy(server, "second", 2);

    expect(redeployed.outcome, redeployed.output).toBe("done");
    expect(redeployed.url).toBe(first.url);
    expect(await get(server.slotPort("a", 2), "/version")).toBe("2");
    // The data now has schema 2, which version 1's migration refuses: so it was kept.
    const back = await deploy(server, "second", 1);
    expect(back.outcome, back.output).toBe("failed");
    expect(back.reason).toMatch(/migrate/);
  });

  test("a failed redeploy leaves that Environment stopped, and the App's others running", async () => {
    const server = await harness.server({ slots: 3 });
    const app = server.apps.a.name;
    await deploy(server, "first", 1);
    await deploy(server, "second", 1);

    const result = await deploy(server, "second", 1, "unhealthy");

    expect(result.code, result.output).toBe(1);
    expect(result.reason).toMatch(/^deadlift: .*feature-second.*stopped/);
    const running = await server.runningContainers(app);
    expect(running).not.toMatch(/feature-second/);
    expect(running).toMatch(/feature-first/);
  });

  test("with every Slot held, a new key is skipped, naming the holders, and nothing is created", async () => {
    const server = await harness.server({ slots: 2 });
    const app = server.apps.a.name;
    await deploy(server, "first");
    await deploy(server, "second");
    const mappings = await server.serveMappings();

    const result = await deploy(server, "third");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("skipped");
    expect(result.reason).toMatch(
      /^deadlift: .*held, by feature-first and feature-second, so feature-third wasn't deployed/,
    );
    expect(await server.containers(app)).not.toMatch(/feature-third/);
    expect(await server.serveMappings()).toEqual(mappings);
  });
});

