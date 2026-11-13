// A fake GitHub runner for testing deadlift's actions through their action.yml: a job's checkout,
// and the runner's tools faked on PATH ahead of any real ones (test/fakes/runner). Its `ssh`
// stands in for the Server, keeping what each delivery sent and answering as the test says.
// Nothing reaches the network, the tailnet or this machine's Docker, and nothing runs as root.
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { type ActionRun, runAction, type UsesStub } from "./composite-action.ts";

const fakes = resolve(import.meta.dirname, "../fakes/runner");

/** What one delivery sent the Server. */
export interface SentDelivery {
  /** Who it logged in as, and where: `deadlift-<app>@<server>`. */
  destination: string;
  /** The command the Server ran, word by word. */
  command: string[];
  /** The delivery tar's members and their contents. */
  files: Record<string, string>;
}

export function fakeRunner() {
  const dir = mkdtempSync(join(tmpdir(), "deadlift-test-runner-"));
  const state = join(dir, "state");
  const workspace = join(dir, "workspace");
  mkdirSync(state);
  mkdirSync(workspace);
  mkdirSync(join(dir, "home"));
  const env = {
    PATH: `${fakes}:/usr/bin:/bin`,
    HOME: join(dir, "home"),
    FAKE_RUNNER_STATE: state,
  };

  return {
    /** The job's checkout, where the action runs. */
    workspace,
    /** Makes the Server answer the next deliveries with this output and exit code. */
    serverAnswers(output: string, status = 0) {
      writeFileSync(join(state, "server-reply"), output);
      writeFileSync(join(state, "server-status"), String(status));
    },
    /** Every delivery the Server got, in order. */
    deliveries(): SentDelivery[] {
      const sshDir = join(state, "ssh");
      let calls: string[];
      try {
        calls = readdirSync(sshDir).sort((a, b) => Number(a) - Number(b));
      } catch {
        return [];
      }
      return calls.map((n) => {
        const call = join(sshDir, n);
        const files = Object.fromEntries(
          readdirSync(join(call, "delivery")).map((f) => [f, readFileSync(join(call, "delivery", f), "utf8")]),
        );
        return {
          destination: readFileSync(join(call, "destination"), "utf8").trim(),
          command: JSON.parse(readFileSync(join(call, "args"), "utf8")),
          files,
        };
      });
    },
    /** Every call to the runner's faked tools, such as `sudo tailscale logout`, in order. */
    calls(): string[] {
      try {
        return readFileSync(join(state, "calls.log"), "utf8").split("\n").filter(Boolean);
      } catch {
        return [];
      }
    },
    /** Runs a composite action from the repo on this runner. */
    runAction(
      action: string,
      inputs: Record<string, string>,
      { github = {}, uses = {} }: { github?: Record<string, string>; uses?: Record<string, UsesStub> } = {},
    ): ActionRun {
      return runAction(action, {
        inputs,
        github: { token: "fake-job-token", ...github },
        env,
        uses,
        cwd: workspace,
      });
    },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}
