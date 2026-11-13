// Runs one of deadlift's composite actions as a GitHub runner would, far enough to test it: its
// inputs (with defaults), its `bash` steps with GITHUB_ACTION_PATH, GITHUB_OUTPUT and
// GITHUB_STEP_SUMMARY, and its outputs. Expressions may name only `inputs.*`, `github.*` and
// `steps.<id>.outputs.*`. A step's `if:` may be only `success()` (the default), `always()` or
// `failure()`. A step that `uses:` another action runs the test's stand-in for it, given the
// step's `with:`, since the real one can't run here.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { parse } from "yaml";

const repoRoot = resolve(import.meta.dirname, "../..");

interface ActionYaml {
  inputs?: Record<string, { default?: string; required?: boolean }>;
  outputs?: Record<string, { value: string }>;
  runs: { using: string; steps: Step[] };
}

interface Step {
  id?: string;
  if?: string;
  uses?: string;
  with?: Record<string, string>;
  shell?: string;
  env?: Record<string, string>;
  run?: string;
}

/** A test's stand-in for an action a step `uses:`. It gets the step's `with:` and returns its
 * exit code, 0 when it returns nothing. */
export type UsesStub = (inputs: Record<string, string>) => number | void;

export interface ActionRun {
  /** The failing step's exit code, or 0 when every step passed. */
  status: number | null;
  stdout: string;
  stderr: string;
  outputs: Record<string, string>;
  summary: string;
}

export function runAction(
  action: string,
  {
    inputs = {},
    github = {},
    env = {},
    uses = {},
    cwd,
  }: {
    inputs?: Record<string, string>;
    github?: Record<string, string>;
    env?: Record<string, string>;
    /** Stand-ins for the actions the steps use, by name without the `@ref`. */
    uses?: Record<string, UsesStub>;
    /** The job's working directory, its checkout. */
    cwd?: string;
  },
): ActionRun {
  const actionPath = join(repoRoot, action);
  const yaml: ActionYaml = parse(readFileSync(join(actionPath, "action.yml"), "utf8"));
  if (yaml.runs.using !== "composite") throw new Error(`${action} isn't a composite action`);

  const steps: Record<string, Record<string, string>> = {};
  const context = (expression: string): string => {
    const [scope, ...path] = expression.trim().split(".");
    if (scope === "github") return github[path.join(".")] ?? "";
    if (scope === "inputs") return resolvedInputs[path.join(".")] ?? "";
    if (scope === "steps" && path[1] === "outputs") return steps[path[0]]?.[path[2]] ?? "";
    throw new Error(`the test runner can't evaluate \${{ ${expression} }}`);
  };
  const evaluate = (value: string) => value.replace(/\$\{\{(.*?)\}\}/g, (_, e: string) => context(e));

  const resolvedInputs: Record<string, string> = {};
  for (const [name, spec] of Object.entries(yaml.inputs ?? {})) {
    if (name in inputs) resolvedInputs[name] = inputs[name];
    else if (spec.required) throw new Error(`input ${name} is required`);
    else resolvedInputs[name] = evaluate(String(spec.default ?? ""));
  }
  for (const name of Object.keys(inputs)) {
    if (!(name in resolvedInputs)) throw new Error(`${action} has no input ${name}`);
  }

  const dir = mkdtempSync(join(tmpdir(), "deadlift-test-action-"));
  const summaryFile = join(dir, "summary");
  writeFileSync(summaryFile, "");
  let stdout = "";
  let stderr = "";
  let status: number | null = 0;
  try {
    for (const [index, step] of yaml.runs.steps.entries()) {
      const condition = (step.if ?? "success()").replace(/^\$\{\{(.*)\}\}$/, "$1").trim();
      const runs = { "success()": status === 0, "always()": true, "failure()": status !== 0 }[condition];
      if (runs === undefined) throw new Error(`the test runner can't evaluate if: ${step.if}`);
      if (!runs) continue;
      let stepStatus: number | null;
      if (step.uses !== undefined) {
        const name = step.uses.replace(/@.*$/, "");
        const stub = uses[name];
        if (!stub) throw new Error(`the test gave no stand-in for ${name} (step ${index})`);
        const withInputs = Object.fromEntries(
          Object.entries(step.with ?? {}).map(([k, v]) => [k, evaluate(String(v))]),
        );
        stepStatus = stub(withInputs) ?? 0;
      } else {
        if (step.run === undefined || step.shell !== "bash") {
          throw new Error(`the test runner only runs \`shell: bash\` steps (step ${index})`);
        }
        const outputFile = join(dir, `output-${index}`);
        writeFileSync(outputFile, "");
        const stepEnv = Object.fromEntries(
          Object.entries(step.env ?? {}).map(([k, v]) => [k, evaluate(String(v))]),
        );
        // As the runner does for `shell: bash`.
        const result = spawnSync("bash", ["--noprofile", "--norc", "-eo", "pipefail", "-c", evaluate(step.run)], {
          encoding: "utf8",
          cwd,
          env: {
            ...env,
            ...stepEnv,
            GITHUB_ACTION_PATH: actionPath,
            GITHUB_OUTPUT: outputFile,
            GITHUB_STEP_SUMMARY: summaryFile,
          },
        });
        stdout += result.stdout;
        stderr += result.stderr;
        if (step.id) steps[step.id] = parseOutputFile(readFileSync(outputFile, "utf8"));
        stepStatus = result.status;
      }
      // The action fails with its first failing step, whatever later `always()` steps do.
      if (stepStatus !== 0 && status === 0) status = stepStatus;
    }
    const outputs = Object.fromEntries(
      Object.entries(yaml.outputs ?? {}).map(([name, spec]) => [name, evaluate(spec.value)]),
    );
    return { status, stdout, stderr, outputs, summary: readFileSync(summaryFile, "utf8") };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/** Parses $GITHUB_OUTPUT: `key=value` lines, and `key<<DELIMITER` blocks for multi-line values. */
export function parseOutputFile(file: string): Record<string, string> {
  const outputs: Record<string, string> = {};
  const lines = file.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const heredoc = lines[i].match(/^([^=]+)<<(.+)$/);
    if (heredoc) {
      const end = lines.indexOf(heredoc[2], i + 1);
      outputs[heredoc[1]] = lines.slice(i + 1, end).join("\n");
      i = end;
    } else if (lines[i].includes("=")) {
      outputs[lines[i].slice(0, lines[i].indexOf("="))] = lines[i].slice(lines[i].indexOf("=") + 1);
    }
  }
  return outputs;
}
