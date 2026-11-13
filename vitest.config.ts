import { configDefaults, defineConfig } from "vitest/config";

// The Seam A tests drive real containers, so test files run one at a time and get minutes, not
// seconds.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    // Agent worktrees live under .claude/worktrees and carry their own copies of the tests.
    exclude: [...configDefaults.exclude, ".claude/**"],
    fileParallelism: false,
    testTimeout: 300_000,
    hookTimeout: 300_000,
  },
});
