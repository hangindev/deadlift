// deadlift's override (ADR 0001): what every Environment's containers run with, whatever the
// App's template says, as Docker reports it.
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { docker, exampleTemplate, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterAll(() => harness.remove());

type Container = {
  HostConfig: {
    Memory: number;
    NanoCpus: number;
    PidsLimit: number;
    SecurityOpt: string[] | null;
    LogConfig: { Type: string; Config: Record<string, string> };
    PortBindings: Record<string, Array<{ HostIp: string; HostPort: string }>> | null;
  };
  Config: { Labels: Record<string, string> };
};

describe("deadlift's override", () => {
  let server: Awaited<ReturnType<typeof harness.server>>;
  let app: string;
  let web: Container;
  let migrate: Container;

  // The App may use 256 MiB, half a CPU and 200 processes in each container. Its template asks
  // for more memory for web, and less for migrate.
  beforeAll(async () => {
    server = await harness.server({ limits: { memory: "256m", cpus: "0.5", pids: 200 } });
    app = server.apps.a.name;
    const template = (await exampleTemplate())
      .replace(
        "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
        "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n    mem_limit: 4g\n    pids_limit: -1\n",
      )
      .replace(
        "    command: [\"node\", \"migrate.ts\"]\n",
        '    command: ["node", "migrate.ts"]\n    mem_limit: 128m\n',
      );
    const result = await server.deliver(
      app,
      ["deploy", "feature-1", "--image", `web=${await harness.image()}`],
      { template },
    );
    expect(result.outcome, result.output).toBe("done");
    const inspect = async (service: string) =>
      (JSON.parse(await docker("inspect", `deadlift-${app}-feature-1-${service}-1`)) as Container[])[0]!;
    web = await inspect("web");
    migrate = await inspect("migrate");
  });
  afterAll(() => harness.removeServers());

  test("publishes only the routed service, and only on the host's loopback", () => {
    expect(web.HostConfig.PortBindings).toEqual({
      "3000/tcp": [{ HostIp: "127.0.0.1", HostPort: String(server.slotPort("a", 1)) }],
    });
    expect(migrate.HostConfig.PortBindings ?? {}).toEqual({});
  });

  test("limits every container to the App's memory, CPUs and processes", () => {
    expect(web.HostConfig.Memory).toBe(256 * 1024 * 1024);
    expect(web.HostConfig.NanoCpus).toBe(500_000_000);
    expect(web.HostConfig.PidsLimit).toBe(200);
    expect(migrate.HostConfig.NanoCpus).toBe(500_000_000);
    expect(migrate.HostConfig.PidsLimit).toBe(200);
  });

  test("keeps a template's own limit where it's lower than the App's", () => {
    expect(migrate.HostConfig.Memory).toBe(128 * 1024 * 1024);
  });

  test("sets no-new-privileges on every container", () => {
    expect(web.HostConfig.SecurityOpt).toContain("no-new-privileges:true");
    expect(migrate.HostConfig.SecurityOpt).toContain("no-new-privileges:true");
  });

  test("caps every container's logs", () => {
    for (const container of [web, migrate]) {
      expect(container.HostConfig.LogConfig).toEqual({
        Type: "json-file",
        Config: { "max-size": "10m", "max-file": "3" },
      });
    }
  });

  test("labels every container and volume with its App and Environment", async () => {
    for (const container of [web, migrate]) {
      expect(container.Config.Labels).toMatchObject({
        "deadlift.app": app,
        "deadlift.environment": "feature-1",
      });
    }
    const volume = JSON.parse(
      await docker("volume", "inspect", `deadlift-${app}-feature-1_data`),
    ) as Array<{ Labels: Record<string, string> }>;
    expect(volume[0]!.Labels).toMatchObject({
      "deadlift.app": app,
      "deadlift.environment": "feature-1",
    });
  });
});

test("an App with no limits set gets deadlift's defaults: 1 GiB, 1 CPU and 512 processes", async () => {
  const server = await harness.server();
  const app = server.apps.a.name;

  const result = await server.deliver(app, [
    "deploy",
    "feature-1",
    "--image",
    `web=${await harness.image()}`,
  ]);

  expect(result.outcome, result.output).toBe("done");
  const [web] = JSON.parse(
    await docker("inspect", `deadlift-${app}-feature-1-web-1`),
  ) as Container[];
  expect(web!.HostConfig.Memory).toBe(1024 * 1024 * 1024);
  expect(web!.HostConfig.NanoCpus).toBe(1_000_000_000);
  expect(web!.HostConfig.PidsLimit).toBe(512);
  await harness.removeServers();
});
