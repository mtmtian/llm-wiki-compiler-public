/**
 * Build the optional host adapter separately from upstream CLI/SDK bundles.
 * Production dependencies resolve from the same pinned installation as the
 * compiler. The caller chooses a staging directory and promotes it after tests.
 */
import { build } from "tsup";
import { cp, mkdir, readdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const source = path.dirname(fileURLToPath(import.meta.url));
const destination = process.argv[2];
if (!destination || !path.isAbsolute(destination)) throw new Error("Supply an absolute build destination");
await mkdir(destination, { recursive: true });
await build({ entry: { worker: path.join(source, "entry.ts") }, outDir: destination,
  platform: "node", format: ["esm"], target: "node24", bundle: true, dts: false,
  config: false, removeNodeProtocol: false, outExtension: () => ({ js: ".mjs" }) });
for (const name of (await readdir(source)).filter(name => name.endsWith(".py") && !name.startsWith("test_")).sort()) {
  await cp(path.join(source, name), path.join(destination, name));
}
