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
});
