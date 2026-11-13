// Runs a workflow as GitHub would for one event, far enough to test the starter workflows: whether
// the event triggers it, its jobs in `needs` order with their `if:`, `env`, `outputs` and the
// `needs` context, and each job's steps: `run:` steps in bash, with GITHUB_OUTPUT and
// GITHUB_STEP_SUMMARY, and `uses:` steps through the test's stand-ins, which get the step's
// `with:` and answer with an exit code and outputs. Expressions are GitHub's (expressions.ts).
// Concurrency, permissions and runners aren't modelled; actionlint checks those are well formed.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parse } from "yaml";
import { parseOutputFile } from "./composite-action.ts";
import { condition, interpolate, type Scope, toText, type Value } from "./expressions.ts";

interface Step {
  id?: string;
  name?: string;
  if?: string;
  uses?: string;
  with?: Record<string, unknown>;
  env?: Record<string, unknown>;
  shell?: string;
  run?: string;
}

interface Job {
  needs?: string | string[];
  if?: string;
  env?: Record<string, unknown>;
  outputs?: Record<string, string>;
  steps: Step[];
}

interface Workflow {
  on: Record<string, Record<string, unknown> | null>;
  env?: Record<string, unknown>;
  defaults?: { run?: { shell?: string } };
  jobs: Record<string, Job>;
}

/** A test's stand-in for an action a step `uses:`. It gets the step's `with:`. */
export type UsesStub = (
  inputs: Record<string, string>,
) => { status?: number; outputs?: Record<string, string> } | void;

export interface JobRun {
  result: "success" | "failure" | "skipped";
  outputs: Record<string, string>;
}

export interface WorkflowRun {
  /** Whether the event triggers the workflow at all. When it doesn't, no job ran. */
  triggered: boolean;
  jobs: Record<string, JobRun>;
  /** Each `uses:` step that ran, in order: the action without its `@ref`, and its `with:`. */
  used: { action: string; ref: string; with: Record<string, string> }[];
  stdout: string;
  stderr: string;
  /** Every job's summary, run by run. */
  summary: string;
}

export interface WorkflowEvent {
  /** The event's name, such as `pull_request`, `push`, `schedule` or `workflow_dispatch`. */
  name: string;
  /** The event's payload, `github.event`. */
  payload?: Record<string, Value>;
  /** For `workflow_dispatch`: the inputs given, before defaults. */
  inputs?: Record<string, Value>;
}

export function runWorkflow(
  file: string,
  event: WorkflowEvent,
  {
    github = {},
    vars = {},
    uses = {},
    env = {},
    cwd,
  }: {
    /** The rest of the `github` context: repository, sha, ref, token, … */
    github?: Record<string, Value>;
    vars?: Record<string, string>;
    /** Stand-ins for the actions the steps use, by name without the `@ref`. */
    uses?: Record<string, UsesStub>;
    /** The runner's own environment: PATH, HOME, … */
    env?: Record<string, string>;
    /** The job's working directory. */
    cwd?: string;
  } = {},
): WorkflowRun {
  const workflow: Workflow = parse(readFileSync(file, "utf8"));
  const run: WorkflowRun = { triggered: false, jobs: {}, used: [], stdout: "", stderr: "", summary: "" };
  if (!triggers(workflow, event, github)) return run;
  run.triggered = true;

  const ref = String(github.ref ?? "");
  const githubContext: Record<string, Value> = {
    event_name: event.name,
    event: { ...(event.payload ?? {}), ...(event.inputs ? { inputs: event.inputs } : {}) },
    ref_name: ref.replace(/^refs\/(heads|tags)\//, ""),
    server_url: "https://github.com",
    run_id: "1234",
    ...github,
  };
  const inputs = dispatchInputs(workflow, event);
  const base = { github: githubContext, vars, inputs, secrets: {} };
  const status = { success: true, failure: false, cancelled: false };
  const workflowEnv = evaluateAll(workflow.env, { contexts: base, status });
  const shell = workflow.defaults?.run?.shell;

  const dir = mkdtempSync(join(tmpdir(), "deadlift-test-workflow-"));
  try {
    for (const name of jobOrder(workflow.jobs)) {
      const job = workflow.jobs[name];
      const needs = [job.needs ?? []].flat();
      const needsContext = Object.fromEntries(needs.map((n) => [n, run.jobs[n] as unknown as Value]));
      const jobScope: Scope = {
        contexts: { ...base, env: workflowEnv, needs: needsContext },
        status: {
          success: needs.every((n) => run.jobs[n].result === "success"),
          failure: needs.some((n) => run.jobs[n].result === "failure"),
          cancelled: false,
        },
      };
      if (!condition(job.if ?? "success()", jobScope)) {
        run.jobs[name] = { result: "skipped", outputs: {} };
        continue;
      }
      const jobEnv = { ...workflowEnv, ...evaluateAll(job.env, jobScope) };
      const steps: Record<string, Value> = {};
      let failed = false;
      const summaryFile = join(dir, `summary-${name}`);
      writeFileSync(summaryFile, "");
      for (const [index, step] of job.steps.entries()) {
        const scope: Scope = {
          contexts: { ...base, env: jobEnv, needs: needsContext, steps },
          status: { success: !failed, failure: failed, cancelled: false },
        };
        if (!condition(step.if ?? "success()", scope)) {
          if (step.id) steps[step.id] = { outcome: "skipped", conclusion: "skipped", outputs: {} };
          continue;
        }
        let stepStatus: number;
        let outputs: Record<string, string> = {};
        if (step.uses !== undefined) {
          const [action, actionRef = ""] = step.uses.split("@");
          const stub = uses[action];
          if (!stub) throw new Error(`the test gave no stand-in for ${action} (${name} step ${index})`);
          const withInputs = evaluateAll(step.with, scope);
          run.used.push({ action, ref: actionRef, with: withInputs });
          const answer = stub(withInputs) ?? {};
          stepStatus = answer.status ?? 0;
          outputs = answer.outputs ?? {};
        } else {
          if (step.run === undefined || (step.shell ?? shell) !== "bash") {
            throw new Error(`the test runner only runs bash steps (${name} step ${index})`);
          }
          const outputFile = join(dir, `output-${name}-${index}`);
          writeFileSync(outputFile, "");
          const stepEnv = evaluateAll(step.env, scope);
          // As the runner does for `shell: bash`.
          const result = spawnSync(
            "bash",
            ["--noprofile", "--norc", "-eo", "pipefail", "-c", interpolate(step.run, scope)],
            {
              encoding: "utf8",
              cwd,
              env: {
                ...env,
                ...jobEnv,
                ...stepEnv,
                CI: "true",
                GITHUB_ACTIONS: "true",
                GITHUB_OUTPUT: outputFile,
                GITHUB_STEP_SUMMARY: summaryFile,
              },
            },
          );
          run.stdout += result.stdout;
          run.stderr += result.stderr;
          stepStatus = result.status ?? 1;
          outputs = parseOutputFile(readFileSync(outputFile, "utf8"));
        }
        const outcome = stepStatus === 0 ? "success" : "failure";
        if (step.id) steps[step.id] = { outcome, conclusion: outcome, outputs };
        if (stepStatus !== 0) failed = true;
      }
      run.summary += readFileSync(summaryFile, "utf8");
      const outputScope: Scope = {
        contexts: { ...base, env: jobEnv, needs: needsContext, steps },
        status: { success: !failed, failure: failed, cancelled: false },
      };
      run.jobs[name] = {
        result: failed ? "failure" : "success",
        outputs: evaluateAll(job.outputs, outputScope),
      };
    }
    return run;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function evaluateAll(values: Record<string, unknown> | undefined, scope: Scope): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values ?? {}).map(([key, v]) => [key, interpolate(toText(v as Value), scope)]),
  );
}

function triggers(workflow: Workflow, event: WorkflowEvent, github: Record<string, Value>): boolean {
  if (!(event.name in workflow.on)) return false;
  const filter = workflow.on[event.name] ?? {};
  if (event.name === "pull_request" || event.name === "pull_request_target") {
    const types = (filter.types as string[] | undefined) ?? ["opened", "synchronize", "reopened"];
    if (!types.includes(String(event.payload?.action))) return false;
  }
  if (event.name === "push" && filter.branches) {
    const branch = String(github.ref ?? "").replace(/^refs\/heads\//, "");
    if (!(filter.branches as string[]).some((pattern) => glob(pattern).test(branch))) return false;
  }
  return true;
}

function glob(pattern: string): RegExp {
  const source = pattern
    .split(/(\*\*|\*)/)
    .map((part) => (part === "**" ? ".*" : part === "*" ? "[^/]*" : part.replace(/[.+?^${}()|[\]\\]/g, "\\$&")))
    .join("");
  return new RegExp(`^${source}$`);
}

function dispatchInputs(workflow: Workflow, event: WorkflowEvent): Record<string, Value> {
  if (event.name !== "workflow_dispatch") return {};
  const specs = (workflow.on.workflow_dispatch?.inputs ?? {}) as Record<string, { type?: string; default?: Value }>;
  const given = event.inputs ?? {};
  for (const name of Object.keys(given)) {
    if (!(name in specs)) throw new Error(`the workflow has no input ${name}`);
  }
  return Object.fromEntries(
    Object.entries(specs).map(([name, spec]) => {
      const v = name in given ? given[name] : (spec.default ?? (spec.type === "boolean" ? false : ""));
      return [name, spec.type === "boolean" ? v === true || v === "true" : v];
    }),
  );
}

function jobOrder(jobs: Record<string, Job>): string[] {
  const order: string[] = [];
  const visit = (name: string, path: string[]) => {
    if (order.includes(name)) return;
    if (path.includes(name)) throw new Error(`jobs need each other: ${[...path, name].join(" -> ")}`);
    if (!jobs[name]) throw new Error(`no job named ${name}`);
    for (const need of [jobs[name].needs ?? []].flat()) visit(need, [...path, name]);
    order.push(name);
  };
  for (const name of Object.keys(jobs)) visit(name, []);
  return order;
}
