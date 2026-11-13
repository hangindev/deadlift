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
