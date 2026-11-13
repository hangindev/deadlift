import { configDefaults, defineConfig } from "vitest/config";

// The Seam A tests drive real containers, so test files run one at a time and get minutes, not
// seconds.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    // Agent worktrees live under .claude/worktrees and carry their own copies of the tests.
    // Seam B reconfigures the machine it runs on, so it runs only by `pnpm test:seam-b`, on a
    // throwaway machine (vitest.seam-b.config.ts).
    exclude: [...configDefaults.exclude, ".claude/**", "test/seam-b/**"],
    fileParallelism: false,
    testTimeout: 300_000,
    hookTimeout: 300_000,
  },
});
