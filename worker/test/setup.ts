import { env, applyD1Migrations, type D1Migration } from "cloudflare:test";
import { beforeAll } from "vitest";

// The migrations binding only exists under test (vitest.config.ts hands it in),
// so it's declared here rather than in the generated worker-configuration.d.ts.
// The pool types `env` as Cloudflare.Env now; the old ProvidedEnv augmentation
// no longer reached it, which is why tsc couldn't see this binding.
declare global {
  namespace Cloudflare {
    interface Env {
      TEST_MIGRATIONS: D1Migration[];
    }
  }
}

// Without this the state table doesn't exist, every handler 500s, and an auth
// test asserting "not 401" passes on the error — which is exactly what happened.
beforeAll(async () => {
  await applyD1Migrations(env.STATE_DB, env.TEST_MIGRATIONS);
});
