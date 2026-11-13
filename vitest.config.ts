import { defineConfig } from "vitest/config";

// Every test drives real containers, so they run one at a time and get minutes, not seconds.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    fileParallelism: false,
    testTimeout: 300_000,
    hookTimeout: 300_000,
  },
});
