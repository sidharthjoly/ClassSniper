import { defineConfig } from "vitest/config";
import { cloudflareTest } from "@cloudflare/vitest-pool-workers";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        // Stand-ins for the secrets. The real ones live in `wrangler secret`.
        bindings: {
          GITHUB_TOKEN: "test-token",
          DASHBOARD_PASSPHRASE: "correct-horse-battery-staple",
        },
      },
    }),
  ],
});
