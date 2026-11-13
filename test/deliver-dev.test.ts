// The Dev Environment: an App's CI delivers each merge to it, at the root of the App's Service.
// A Volume Snapshot is taken before each deploy, and Recovery puts it back, with the previous
// version, when a deploy fails after Dev stopped.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { docker, exampleTemplate, get, putNote, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

type Server = Awaited<ReturnType<typeof harness.server>>;

/** Deploys the example App's `kind` image at `version` to app a's Dev Environment. */
async function deployDev(server: Server, kind: Parameters<typeof harness.image>[0], version: number) {
  return server.deliver(server.apps.a.name, [
    "deploy",
    "dev",
    "--image",
    `web=${await harness.image(kind, version)}`,
  ]);
}

/** A Server whose app a has Dev up on version 1 of the example App, holding a note. */
async function devOnVersion1(options: { snapshots?: number } = {}) {
  const server = await harness.server(options);
  const v1 = await deployDev(server, "healthy", 1);
  expect(v1.outcome, v1.output).toBe("done");
  await putNote(server.devPort("a"), "written on v1");
  return server;
}

describe("deploy dev", () => {
  test("brings the example App up healthy at the root of the App's Service, and deliver reports its URL", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;

    const result = await deployDev(server, "healthy", 1);

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.environment).toBe("dev");
    expect(result.url).toBe(`https://${app}.tailtest.ts.net/`);
    expect(result.reason).toMatch(/^deadlift: /);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await server.serveMappings()).toEqual([
      { service: `svc:${app}`, port: 443, target: `http://127.0.0.1:${server.devPort("a")}` },
    ]);
  });

  test("replaces the running version with the new one, keeping Dev's data", async () => {
    const server = await devOnVersion1();

    const result = await deployDev(server, "healthy", 2);

    expect(result.outcome, result.output).toBe("done");
    expect(await get(server.devPort("a"), "/version")).toBe("2");
    expect(await get(server.devPort("a"), "/note")).toBe("written on v1");
  });

  test("leaves Dev running and unchanged when an image can't be pulled", async () => {
    const server = await devOnVersion1();
    const before = await server.startedAt(server.apps.a.name, "dev", "web");
    const missing = (await harness.image("healthy", 1)).replace(/:[^:/]+$/, ":no-such-tag");

    const result = await server.deliver(server.apps.a.name, ["deploy", "dev", "--image", `web=${missing}`]);

    expect(result.code, result.output).toBe(1);
    expect(result.outcome).toBe("failed");
    expect(result.reason).toMatch(/^deadlift: .*no-such-tag.*Nothing was changed/);
    expect(await server.startedAt(server.apps.a.name, "dev", "web")).toBe(before);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
  });

  test("leaves Dev running and unchanged when the template is refused", async () => {
    const server = await devOnVersion1();
    const before = await server.startedAt(server.apps.a.name, "dev", "web");
    const template = (await exampleTemplate()).replace(/^x-deadlift:\n.*\n/m, "");

    const result = await server.deliver(
      server.apps.a.name,
      ["deploy", "dev", "--image", `web=${await harness.image("healthy", 2)}`],
      { template },
    );

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/x-deadlift\.route/);
    expect(await server.startedAt(server.apps.a.name, "dev", "web")).toBe(before);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
  });

  test("keeps the App's configured number of Volume Snapshots", async () => {
    const server = await devOnVersion1({ snapshots: 2 });

    for (const version of [2, 3, 4]) {
      expect((await deployDev(server, "healthy", version)).outcome).toBe("done");
    }

    expect(await server.volumeSnapshots(server.apps.a.name)).toHaveLength(2);
  });

  test("keeps 3 Volume Snapshots when the App doesn't say", async () => {
    const server = await devOnVersion1();

    for (const version of [2, 3, 4]) {
      expect((await deployDev(server, "healthy", version)).outcome).toBe("done");
    }

    expect(await server.volumeSnapshots(server.apps.a.name)).toHaveLength(3);
  });
});

describe("Recovery", () => {
  test("puts back the previous version and its data when a migration fails after Dev stopped", async () => {
    const server = await devOnVersion1();

    const result = await deployDev(server, "failing-migration", 2);

    expect(result.code, result.output).toBe(1);
    expect(result.outcome).toBe("failed");
    expect(result.reason).toMatch(/^deadlift: dev /);
    expect(result.reason).toMatch(/migrate/);
    expect(result.reason).toMatch(/Recovery succeeded/);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await get(server.devPort("a"), "/note")).toBe("written on v1");
  });

  test("puts back the data as it was before the deploy, which the previous version needs", async () => {
    const server = await devOnVersion1();

    // Version 2 migrates the data to its schema, then never reports healthy. Version 1 refuses
    // data migrated past its own schema, so it only starts again on the data from before.
    const result = await deployDev(server, "unhealthy", 2);

    expect(result.code, result.output).toBe(1);
    expect(result.reason).toMatch(/unhealthy/);
    expect(result.reason).toMatch(/Recovery succeeded/);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await get(server.devPort("a"), "/note")).toBe("written on v1");
  });

  test("leaves Dev stopped, saying so, when the first deploy fails", async () => {
    const server = await harness.server();

    const result = await deployDev(server, "failing-migration", 1);

    expect(result.code, result.output).toBe(1);
    expect(result.reason).toMatch(/^deadlift: dev .*no previous version.*Dev is stopped/);
    expect(await server.runningContainers(server.apps.a.name)).toBe("");
  });

  test("says Recovery failed, and that Dev is stopped, when the previous version doesn't start either", async () => {
    const server = await harness.server();
    const previous = await harness.image("healthy", 9);
    expect((await deployDev(server, "healthy", 9)).outcome).toBe("done");
    // Gone from the Server, so Recovery can't start it again.
    await docker("image", "rm", "--force", previous);

    const result = await deployDev(server, "failing-migration", 10);

    expect(result.code, result.output).toBe(1);
    expect(result.reason).toMatch(/^deadlift: dev .*Recovery failed.*Dev is stopped/);
    expect(await server.runningContainers(server.apps.a.name)).toBe("");
  });
});
