/** Persist each complete model response so an interrupted session batch resumes its exact reviewed inputs. */
import path from "node:path";
import { mkdir, readFile } from "node:fs/promises";
import Ajv from "ajv";
import { atomicWrite } from "../../src/utils/markdown.js";
import { sha256Text } from "../../src/connectors/hash.js";
import type { LLMProvider, LLMTool } from "../../src/utils/provider.js";

const ajv = new Ajv({ allErrors: true, strict: false });

/** Invalid model or frozen data needs a new corrected attempt, never human intent review. */
export class ConsolidationOutputError extends Error {}

/** A cache entry is bound to the schema, prompt, model and system policy, never just the stage name. */
export async function durableModel<T>(request: { stateDir: string; jobId: string; model: string; provider: LLMProvider;
  tool: LLMTool; system: string; prompt: string; tokens: number; stage?: string }): Promise<T> {
  const { provider, tool, system, prompt, tokens } = request;
  const folder = path.join(request.stateDir, "consolidation", sha256Text(request.jobId));
  const file = path.join(folder, `${tool.name}${request.stage ? `-${request.stage}` : ""}.json`);
  const identity = sha256Text(JSON.stringify({ tool, system, prompt, model: request.model, tokens }));
  const prior = await readFile(file, "utf8").catch(error => {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  });
  if (prior !== null) {
    const cached = parsed(prior) as { identity?: string; output?: unknown } | null;
    if (cached?.identity !== identity) throw new ConsolidationOutputError("frozen consolidation inputs changed; new attempt required");
    return validated<T>(tool, cached.output);
  }
  const output = validated<T>(tool, parsed(await provider.toolCall(system, [{ role: "user", content: prompt }], [tool], tokens)));
  await mkdir(folder, { recursive: true, mode: 0o700 });
  await atomicWrite(file, JSON.stringify({ identity, output }), { confineRoot: request.stateDir });
  return output;
}

function validated<T>(tool: LLMTool, output: unknown): T {
  const validate = ajv.compile<T>(tool.input_schema);
  if (!validate(output)) throw new ConsolidationOutputError(`${tool.name}: ${ajv.errorsText(validate.errors)}`);
  return output;
}

/** Keep transport errors outside this boundary; only returned data is classified as invalid output. */
function parsed(text: string): unknown {
  try { return JSON.parse(text); }
  catch (error) { throw new ConsolidationOutputError(error instanceof Error ? error.message : "invalid model JSON"); }
}
