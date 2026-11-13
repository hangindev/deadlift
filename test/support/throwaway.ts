// Whether this machine may be reconfigured by tests: bootstrapped as a Server, its Docker
// restarted with userns-remap, its firewall and network changed. Only a machine thrown away after
// the run may: a GitHub-hosted Ubuntu 24.04 runner, or a Multipass VM that test/seam-b/multipass.sh
// launched and marked. Never a developer's machine, even one with passwordless sudo.
import { existsSync } from "node:fs";

/** The file test/seam-b/multipass.sh creates in the VMs it launches for Seam B. */
export const throwawayMarker = "/etc/deadlift-throwaway-machine";

/** Why this machine isn't a throwaway one, or undefined when it is. */
export function whyNotThrowaway(
  env: NodeJS.ProcessEnv = process.env,
  markerExists: boolean = existsSync(throwawayMarker),
): string | undefined {
  const githubHosted =
    env.GITHUB_ACTIONS === "true" &&
    env.RUNNER_ENVIRONMENT === "github-hosted" &&
    env.ImageOS === "ubuntu24";
  if (githubHosted || markerExists) return undefined;
  return (
    "this isn't a throwaway machine (a GitHub-hosted ubuntu-24.04 runner, or a Multipass VM " +
    `marked with ${throwawayMarker} by test/seam-b/multipass.sh), and these tests reconfigure ` +
    "the machine's Docker, firewall and network"
  );
}
