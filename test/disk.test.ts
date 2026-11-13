// A Server under disk pressure keeps its Dev Environments working: over the lower threshold
// Feature deploys are skipped, over the upper one the watchdog stops every Feature Environment,
// and images no Environment uses are pruned after each deploy.
import { afterAll, afterEach, beforeAll, describe, expect, test } from "vitest";
import { get, startHarness, type ImageKind } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterEach(() => harness.removeServers());
afterAll(() => harness.remove());

type Server = Awaited<ReturnType<typeof harness.server>>;

/** Delivers the example App's `kind` image at `version` to `environment` of `app`. */
async function deploy(
  server: Server,
  app: "a" | "b",
  environment: string,
  version = 1,
  kind: ImageKind = "healthy",
) {
  return server.deliver(server.apps[app].name, [
    "deploy",
    environment,
    "--image",
    `web=${await harness.image(kind, version)}`,
  ]);
}

describe("the lower threshold", () => {
  test("over it, a Feature deploy is skipped with a reason, and a Dev deploy still goes ahead", async () => {
    const server = await harness.server({ diskUsed: 81 });
    const app = server.apps.a.name;

    const feature = await deploy(server, "a", "feature-first");
    const dev = await deploy(server, "a", "dev");

    expect(feature.outcome, feature.output).toBe("skipped");
    expect(feature.reason).toMatch(/^deadlift: the disk is 81% full, over .*80%.*feature-first wasn't deployed/);
    expect(await server.containers(app)).not.toMatch(/feature-first/);
    expect(dev.outcome, dev.output).toBe("done");
    expect(await get(server.devPort("a"), "/version")).toBe("1");
  });

  test("is the Server-wide setting, and a disk exactly at it isn't over it", async () => {
    const server = await harness.server({
      diskUsed: 70,
      settings: "# deadlift's Server-wide settings\nfeature_deploy_disk_limit=70\nwatchdog_disk_limit=90\n",
    });

    const at = await deploy(server, "a", "feature-first");
    await server.setDiskUsed(71);
    const over = await deploy(server, "b", "feature-second");

    expect(at.outcome, at.output).toBe("done");
    expect(over.outcome, over.output).toBe("skipped");
    expect(over.reason).toMatch(/71% full, over .*70%/);
  });
});

describe("the watchdog", () => {
  test("over the upper threshold, stops every Feature Environment on the Server and leaves every Dev Environment running", async () => {
    const server = await harness.server();
    for (const app of ["a", "b"] as const) {
      for (const environment of ["dev", "feature-first"]) {
        const result = await deploy(server, app, environment);
        expect(result.outcome, result.output).toBe("done");
      }
    }
    await server.setDiskUsed(91);

    const result = await server.watchdog();

    expect(result.code, result.output).toBe(0);
    const reasons = result.output.split("\n").filter((line) => line.startsWith("deadlift: "));
    expect(reasons.at(-1)).toMatch(/^deadlift: the disk is 91% full, over .*90%.*stopped/);
    for (const app of ["a", "b"] as const) {
      const running = await server.runningContainers(server.apps[app].name);
      expect(running).toMatch(/-dev-web-/);
      expect(running).not.toMatch(/feature-first/);
    }
    expect(await get(server.devPort("a"), "/version")).toBe("1");
    expect(await get(server.devPort("b"), "/version")).toBe("1");
  });

  test("a stopped Feature Environment starts again on its next deploy, at the same address", async () => {
    const server = await harness.server();
    const first = await deploy(server, "a", "feature-first");
    await server.setDiskUsed(95);
    await server.watchdog();
    await server.setDiskUsed(50);

    const again = await deploy(server, "a", "feature-first", 2);

    expect(again.outcome, again.output).toBe("done");
    expect(again.url).toBe(first.url);
    expect(await get(server.slotPort("a", 1), "/version")).toBe("2");
  });

  test("at or under the upper threshold, stops nothing", async () => {
    const server = await harness.server({
      settings: "feature_deploy_disk_limit=95\nwatchdog_disk_limit=96\n",
    });
    await deploy(server, "a", "feature-first");
    await server.setDiskUsed(96);

    const result = await server.watchdog();

    expect(result.code, result.output).toBe(0);
    expect(result.output).toMatch(/^deadlift: the disk is 96% full.*nothing was stopped/m);
    expect(await server.runningContainers(server.apps.a.name)).toMatch(/feature-first/);
  });

  test("waits for the operation in progress on the Server", async () => {
    const server = await harness.server();
    const release = await server.holdLock();

    let finished = false;
    const watchdog = server.watchdog().then((result) => {
      finished = true;
      return result;
    });
    await new Promise((resolve) => setTimeout(resolve, 1000));
    expect(finished).toBe(false);
    await release();
    const result = await watchdog;

    expect(result.code, result.output).toBe(0);
  });
});
