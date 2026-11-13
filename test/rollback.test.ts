// Rollback: the Dev Environment goes back one version on request. It keeps today's data when the
// previous version is healthy on it, and discards it, keeping a Volume Snapshot of it first, only
// when told to with --discard-newer-data.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { exampleTemplate, get, putNote, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

type Server = Awaited<ReturnType<typeof harness.server>>;

/** The example App's template with `RELEASE` set on its web service, so a test can tell two
 * versions of the same image apart through `/env/RELEASE`. */
async function templateForRelease(release: string): Promise<string> {
  return (await exampleTemplate()).replace(
    "  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
    `  web:\n    image: \${DEADLIFT_IMAGE_WEB}\n    environment:\n      RELEASE: "${release}"\n`,
  );
}

/** Deploys version `version` of the example App's healthy image to app a's Dev Environment. */
async function deployDev(server: Server, version: number, options: { template?: string } = {}) {
  const result = await server.deliver(
    server.apps.a.name,
    ["deploy", "dev", "--image", `web=${await harness.image("healthy", version)}`],
    options,
  );
  expect(result.outcome, result.output).toBe("done");
}

function rollback(server: Server, ...args: string[]) {
  return server.deliver(server.apps.a.name, ["rollback", ...args]);
}

describe("rollback dev", () => {
  test("starts the previous version on today's data, and keeps it when it's healthy", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    // Two releases of the same image: the previous one is healthy on today's data.
    await deployDev(server, 1, { template: await templateForRelease("first") });
    await deployDev(server, 1, { template: await templateForRelease("second") });
    await putNote(server.devPort("a"), "written today");

    const result = await rollback(server, "dev");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.environment).toBe("dev");
    expect(result.url).toBe(`https://${app}.tailtest.ts.net/`);
    expect(result.reason).toMatch(/^deadlift: rolled dev back/);
    expect(await get(server.devPort("a"), "/env/RELEASE")).toBe("first");
    expect(await get(server.devPort("a"), "/note")).toBe("written today");
  });

  test("is refused, naming --discard-newer-data, and starts the current version again when the previous one isn't healthy on today's data", async () => {
    const server = await harness.server();
    await deployDev(server, 1);
    // Version 2 migrates the data to its schema, which version 1's migration refuses.
    await deployDev(server, 2);
    await putNote(server.devPort("a"), "written on v2");

    const result = await rollback(server, "dev");

    expect(result.code, result.output).toBe(2);
    expect(result.outcome).toBe("failed");
    expect(result.reason).toMatch(/^deadlift: .*--discard-newer-data/);
    expect(await get(server.devPort("a"), "/version")).toBe("2");
    expect(await get(server.devPort("a"), "/note")).toBe("written on v2");
  });

  test("with --discard-newer-data, keeps a Volume Snapshot of today's data, then starts the previous version on the data from before the current one", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    await deployDev(server, 1);
    await putNote(server.devPort("a"), "written on v1");
    await deployDev(server, 2);
    await putNote(server.devPort("a"), "written on v2");
    const before = await server.volumeSnapshots(app);

    const result = await rollback(server, "dev", "--discard-newer-data");

    expect(result.code, result.output).toBe(0);
    expect(result.outcome).toBe("done");
    expect(result.reason).toMatch(/^deadlift: rolled dev back/);
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await get(server.devPort("a"), "/note")).toBe("written on v1");
    const taken = (await server.volumeSnapshots(app)).filter((name) => !before.includes(name));
    expect(taken).toHaveLength(1);
    expect(await server.fileInVolumeSnapshot(app, taken[0] ?? "", "note")).toBe("written on v2");
    expect(result.reason).toContain(taken[0]);
  });

  test("goes back one step only", async () => {
    const server = await harness.server();
    await deployDev(server, 1, { template: await templateForRelease("first") });
    await deployDev(server, 1, { template: await templateForRelease("second") });
    await deployDev(server, 1, { template: await templateForRelease("third") });
    expect((await rollback(server, "dev")).outcome).toBe("done");
    const before = await server.startedAt(server.apps.a.name, "dev", "web");

    const result = await rollback(server, "dev");

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: .*no previous version/);
    expect(await get(server.devPort("a"), "/env/RELEASE")).toBe("second");
    expect(await server.startedAt(server.apps.a.name, "dev", "web")).toBe(before);
  });
});

test("rollback is refused for a Feature Environment", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;
  const deployed = await server.deliver(app, ["deploy", "feature-42", "--image", `web=${await harness.image()}`]);
  expect(deployed.outcome, deployed.output).toBe("done");
  const before = await server.startedAt(app, "feature-42", "web");

  const result = await rollback(server, "feature-42");

  expect(result.code, result.output).toBe(2);
  expect(result.reason).toMatch(/^deadlift: .*only .*Dev Environment/);
  expect(await server.startedAt(app, "feature-42", "web")).toBe(before);
});
