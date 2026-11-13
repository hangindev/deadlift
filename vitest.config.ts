import { configDefaults, defineConfig } from "vitest/config";

// The delivery tests drive real containers, so test files run one at a time and get minutes, not
// seconds.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    // Agent worktrees live under .claude/worktrees and carry their own copies of the tests.
    // The server-setup tests reconfigure the machine they run on, so they run only by
    // `pnpm test:server-setup`, on a throwaway machine (vitest.server-setup.config.ts).
    exclude: [...configDefaults.exclude, ".claude/**", "test/server-setup/**"],
    fileParallelism: false,
    testTimeout: 300_000,
    hookTimeout: 300_000,
  },
});
