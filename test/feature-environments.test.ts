// Feature Environments sharing an App's Slots: redeploying a key, running out of Slots, and
// destroying one to free its Slot.
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

function destroy(server: Server, key: string) {
  return server.deliver(server.apps.a.name, ["destroy", `feature-${key}`]);
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

describe("destroy feature-<key>", () => {
  test("removes the Environment and its Service mapping, and frees its Slot", async () => {
    const server = await harness.server({ slots: 2 });
    const app = server.apps.a.name;
    await deploy(server, "first", 1);
    await deploy(server, "second", 2);

    const result = await destroy(server, "first");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.environment).toBe("feature-first");
    expect(result.reason).toMatch(/^deadlift: destroyed feature-first/);
    expect(await server.containers(app)).not.toMatch(/feature-first/);
    expect(await server.serveMappings()).toEqual([
      { service: `svc:${app}`, port: 10002, target: `http://127.0.0.1:${server.slotPort("a", 2)}` },
    ]);
    // Its Slot is free again for a new key, at the same address.
    const third = await deploy(server, "third", 1);
    expect(third.outcome, third.output).toBe("done");
    expect(third.url).toBe(`https://${app}.tailtest.ts.net:10001/`);
    expect(await get(server.slotPort("a", 2), "/version")).toBe("2");
  });

  test("removes its volumes, so the key deploys again on fresh data", async () => {
    const server = await harness.server();
    await deploy(server, "first", 2);

    await destroy(server, "first");
    // Version 1's migration refuses data with schema 2, so this passes only on fresh data.
    const again = await deploy(server, "first", 1);

    expect(again.outcome, again.output).toBe("done");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("removes an Environment whose deploy failed, which has no Service mapping", async () => {
    const server = await harness.server({ slots: 1 });
    const app = server.apps.a.name;
    await deploy(server, "first", 1, "failing-migration");

    const result = await destroy(server, "first");

    expect(result.outcome, result.output).toBe("done");
    expect(await server.containers(app)).toBe("");
    expect((await deploy(server, "second")).outcome).toBe("done");
  });

  test("is done, changing nothing, for a key that isn't deployed", async () => {
    const server = await harness.server();
    await deploy(server, "first");
    const mappings = await server.serveMappings();

    const result = await destroy(server, "other");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.reason).toMatch(/^deadlift: .*feature-other.*nothing to destroy/);
    expect(await server.serveMappings()).toEqual(mappings);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("leaves another App's Environment with the same key alone", async () => {
    const server = await harness.server();
    const b = server.apps.b.name;
    await deploy(server, "first");
    await server.deliver(b, ["deploy", "feature-first", "--image", `web=${await harness.image()}`]);

    await destroy(server, "first");

    expect(await get(server.slotPort("b", 1), "/version")).toBe("1");
    expect(await server.serveMappings()).toEqual([
      { service: `svc:${b}`, port: 10001, target: `http://127.0.0.1:${server.slotPort("b", 1)}` },
    ]);
  });

  test("refuses the Dev Environment, exit 2", async () => {
    const server = await harness.server();

    const result = await server.deliver(server.apps.a.name, ["destroy", "dev"]);

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: /);
  });
});

describe("a key that isn't up to 30 of a-z, 0-9 and -", () => {
  const keys = ["Upper", "under_score", "dot.ted", "-leading", "", "a".repeat(31)];

  test.each(keys)("is refused by deploy, exit 2, creating nothing: '%s'", async (key) => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await deploy(server, key);

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: .*key/);
    expect(await server.containers(app)).toBe("");
    expect(await server.serveMappings()).toEqual([]);
  });

  test.each(keys)("is refused by destroy, exit 2: '%s'", async (key) => {
    const server = await harness.server();
    await deploy(server, "first");

    const result = await destroy(server, key);

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: .*key/);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
  });

  test("is accepted at 30 characters", async () => {
    const server = await harness.server();

    const result = await deploy(server, "a".repeat(30));

    expect(result.outcome, result.output).toBe("done");
  });
});
