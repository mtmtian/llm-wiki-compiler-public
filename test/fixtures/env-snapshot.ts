/**
 * @file test/fixtures/env-snapshot.ts
 * @description Scoped environment-variable control for tests.
 *
 * Provider resolution reads `process.env` at call time, so a test must set
 * variables per case and restore them afterwards or they leak into unrelated
 * suites. The original value of each key is captured the FIRST time a test
 * touches it, so several `setEnv` calls within one test still restore to the
 * pre-test value rather than to an intermediate one.
 */

/** Env control bound to `keys`: each `setEnv` clears them all, then applies `values`. */
export function createEnvSnapshot(keys: readonly string[]): {
  setEnv: (values: Record<string, string | undefined>) => void;
  restore: () => void;
} {
  const saved = new Map<string, string | undefined>();
  return {
    setEnv(values) {
      for (const key of keys) {
        if (!saved.has(key)) saved.set(key, process.env[key]);
        delete process.env[key];
      }
      for (const [key, value] of Object.entries(values)) {
        if (!saved.has(key)) saved.set(key, process.env[key]);
        if (value !== undefined) process.env[key] = value;
      }
    },
    restore() {
      for (const [key, value] of saved) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
      saved.clear();
    },
  };
}
