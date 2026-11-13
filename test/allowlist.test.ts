// What an App's Compose template may hold (ADR 0001): anything that reaches past the App's own
// Compose project is refused before anything stops.
import { afterAll, beforeAll, describe, expect, test } from "vitest";
import { exampleTemplate, get, startHarness } from "./harness.ts";

let harness: Awaited<ReturnType<typeof startHarness>>;
beforeAll(async () => {
  harness = await startHarness();
});
afterAll(() => harness.remove());

/** The example template with `lines` added to its routed service, web. */
async function inWeb(lines: string) {
  const template = await exampleTemplate();
  const web = "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n";
  if (!template.includes(web)) throw new Error("the example template's web service has moved");
  return template.replace(web, `${web}${lines}`);
}

/** The example template with one more service, `probe`, running the web image with `lines`. */
async function withProbe(lines: string) {
  return (await exampleTemplate()).replace(
    "services:\n",
    "services:\n  probe:\n    image: ${DEADLIFT_IMAGE_WEB}\n" + lines,
  );
}

/** The example template with `text` added at its top level. */
async function atTopLevel(text: string) {
  return `${await exampleTemplate()}\n${text}`;
}

/** The example template, with `web` also using `kind` (secrets or configs) `name`, defined at the
 * top level as `definition`. */
async function using(kind: "secrets" | "configs", definition: string) {
  return atTopLevel(`${kind}:\n  shared:\n${definition}`).then((template) =>
    template.replace(
      "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
      `\n  web:\n    image: \${DEADLIFT_IMAGE_WEB}\n    ${kind}: [shared]\n`,
    ),
  );
}

const refusals: Array<[string, () => Promise<string>, RegExp]> = [
  // Host files, which Compose would read as root.
  ["a bind mount", () => withProbe("    volumes:\n      - /etc:/host-etc:ro\n"), /volumes.*\/etc/],
  ["env_file", () => inWeb("    env_file: /etc/environment\n"), /env_file/],
  [
    "env_file through a YAML merge key",
    async () =>
      `x-files: &files\n  env_file: /etc/environment\n${await exampleTemplate()}`.replace(
        "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
        "\n  web:\n    <<: *files\n    image: ${DEADLIFT_IMAGE_WEB}\n",
      ),
    /env_file/,
  ],
  ["include", () => atTopLevel("include:\n  - /etc/deadlift/other.yaml\n"), /include/],
  [
    "extends",
    () => inWeb("    extends:\n      file: /etc/deadlift/other.yaml\n      service: web\n"),
    /extends/,
  ],
  ["label_file", () => inWeb("    label_file: /etc/hostname\n"), /label_file/],
  ["a secret read from a file", () => using("secrets", "    file: /etc/shadow\n"), /secrets\.shared\.file/],
  ["a config read from a file", () => using("configs", "    file: /etc/passwd\n"), /configs\.shared\.file/],
  [
    "a named volume with driver_opts",
    async () =>
      (await exampleTemplate()).replace(
        /^volumes:\n {2}data:\n/m,
        "volumes:\n  data:\n    driver_opts: {type: none, o: bind, device: /etc}\n",
      ),
    /driver_opts/,
  ],
  // Privileges and the host's namespaces.
  ["a privileged container", () => inWeb("    privileged: true\n"), /privileged/],
  ["added capabilities", () => inWeb("    cap_add: [SYS_ADMIN]\n"), /cap_add/],
  ["devices", () => inWeb("    devices: [/dev/fuse:/dev/fuse]\n"), /devices/],
  ["a loosened security_opt", () => inWeb("    security_opt: [seccomp=unconfined]\n"), /security_opt/],
  ["the host's network", () => inWeb("    network_mode: host\n"), /network_mode/],
  ["the host's PID namespace", () => inWeb("    pid: host\n"), /pid/],
  ["the host's IPC namespace", () => inWeb("    ipc: host\n"), /ipc/],
  ["the host's UTS namespace", () => inWeb("    uts: host\n"), /uts/],
  ["the host's user namespace", () => inWeb("    userns_mode: host\n"), /userns_mode/],
  // Networks, containers and volumes from outside the project.
  [
    "an external network",
    async () =>
      (await atTopLevel("networks:\n  shared:\n    external: true\n    name: bridge\n")).replace(
        "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n",
        "\n  web:\n    image: ${DEADLIFT_IMAGE_WEB}\n    networks: [shared]\n",
      ),
    /networks\.shared\.external/,
  ],
  ["another container's network", () => inWeb('    network_mode: "container:other"\n'), /container:/],
  ["volumes_from another container", () => inWeb('    volumes_from: ["container:other"]\n'), /volumes_from/],
  [
    "a volume named for another project",
    async () =>
      (await exampleTemplate()).replace(
        /^volumes:\n {2}data:\n/m,
        "volumes:\n  data:\n    name: deadlift-other-dev_data\n",
      ),
    /volumes\.data\.name/,
  ],
  [
    "an external volume",
    async () =>
      (await exampleTemplate()).replace(
        /^volumes:\n {2}data:\n/m,
        "volumes:\n  data:\n    external: true\n",
      ),
    /volumes\.data\.external/,
  ],
  // The Docker socket, in any form.
  [
    "the Docker socket",
    () => withProbe("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n"),
    /Docker socket/,
  ],
  ["use_api_socket", () => inWeb("    use_api_socket: true\n"), /use_api_socket/],
  // Images only, and only deadlift publishes.
  ["build", () => inWeb("    build: .\n"), /build/],
  ["published ports", () => inWeb('    ports: ["8080:3000"]\n'), /ports/],
  ["a container_name", () => inWeb("    container_name: web\n"), /container_name/],
];

const postgresImage =
  "postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94";

describe("what a template may hold", () => {
  afterAll(() => harness.removeServers());

  test("inline configs, tmpfs and shm_size are allowed, and the official Postgres image starts", async () => {
    const server = await harness.server();
    const app = server.apps.a.name;
    // Postgres runs the inline config as its first-start script, and is healthy only once the
    // table that script makes is there. web waits for it.
    const template = (await exampleTemplate())
      .replace(
        "services:\n",
        `services:
  postgres:
    image: ${postgresImage}
    environment:
      POSTGRES_PASSWORD: example
    configs:
      - source: schema
        target: /docker-entrypoint-initdb.d/schema.sql
    tmpfs:
      - /tmp
    shm_size: 256m
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "psql -h 127.0.0.1 -U postgres -tAc 'select 1 from marker'"]
      interval: 1s
      retries: 120
`,
      )
      .replace(
        "    depends_on:\n",
        "    depends_on:\n      postgres:\n        condition: service_healthy\n",
      )
      .replace(
        /^volumes:\n/m,
        "configs:\n  schema:\n    content: |\n      create table marker (id int);\n\nvolumes:\n  pgdata:\n",
      );

    const result = await server.deliver(
      app,
      ["deploy", "feature-1", "--image", `web=${await harness.image()}`],
      { template },
    );

    expect(result.outcome, result.output).toBe("done");
    expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
    expect(await server.runningContainers(app)).toMatch(/-postgres-1/);
  });
});

describe("another App's secrets file", () => {
  afterAll(() => harness.removeServers());

  const ways: Array<[string, (file: string) => Promise<string>]> = [
    ["env_file", (file) => inWeb(`    env_file: ${file}\n`)],
    ["a bind mount", (file) => withProbe(`    volumes:\n      - ${file}:/stolen.env:ro\n`)],
    ["a config read from a file", (file) => using("configs", `    file: ${file}\n`)],
    ["a secret read from a file", (file) => using("secrets", `    file: ${file}\n`)],
  ];

  test.each(ways)("can't be read by a template through %s", async (_, template) => {
    const server = await harness.server();
    const [a, b] = [server.apps.a.name, server.apps.b.name];
    const file = await server.secrets(a, "feature", "A_ONLY=a's own secret\n");

    const result = await server.deliver(
      b,
      ["deploy", "feature-1", "--image", `web=${await harness.image()}`],
      { template: await template(file) },
    );

    expect(result.code, result.output).toBe(2);
    expect(result.reason).toMatch(/^deadlift: .*ADR 0001/);
    expect(result.output).not.toMatch(/a's own secret/);
    expect(await server.containers(b)).toBe("");
  });
});

describe("a template ADR 0001 refuses", () => {
  let server: Awaited<ReturnType<typeof harness.server>>;
  let app: string;
  let running: string;

  // One Feature Environment is up throughout, and every refused delivery targets it.
  beforeAll(async () => {
    server = await harness.server();
    app = server.apps.a.name;
    const result = await server.deliver(app, [
      "deploy",
      "feature-1",
      "--image",
      `web=${await harness.image("healthy", 1)}`,
    ]);
    expect(result.outcome, result.output).toBe("done");
    running = await containerIds(app);
  });
  afterAll(() => harness.removeServers());

  async function containerIds(name: string) {
    return (await server.runningContainers(name)).split("\n").sort().join("\n");
  }

  test.each(refusals)(
    "is refused for %s, exit 2, naming the key, and the running Environment is unchanged",
    async (_, template, key) => {
      const mappings = await server.serveMappings();

      const result = await server.deliver(
        app,
        ["deploy", "feature-1", "--image", `web=${await harness.image("healthy", 2)}`],
        { template: await template() },
      );

      expect(result.code, result.output).toBe(2);
      expect(result.outcome).toBe("failed");
      expect(result.reason).toMatch(/^deadlift: /);
      expect(result.reason).toMatch(key);
      expect(result.reason).toMatch(/ADR 0001/);
      expect(await containerIds(app)).toBe(running);
      expect(await get(server.slotPort("a", 1), "/version")).toBe("1");
      expect(await server.serveMappings()).toEqual(mappings);
    },
  );
});
