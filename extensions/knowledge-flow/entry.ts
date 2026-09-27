/**
 * JSON-only process boundary for the private knowledge workflow adapter.
 * Runtime paths and project inventories are supplied by the local operator;
 * this public entry contains no business data or machine-specific configuration.
 */
import { buildHookContext } from "./context.js";
import { withQuiet } from "../../src/utils/output.js";
import { processJob, resolvePendingReview } from "./index.js";
import type { FlowConfig, FlowJob } from "./types.js";
import { materializeRecords } from "./materialize.js";
import type { PublicationRecord } from "./publication-types.js";
import { eventByteLimit, readBoundedJson } from "./stdin.js";

interface Request {
  config: FlowConfig & { maxContextChars?: number };
  job: FlowJob;
  projectId: string;
  prompt: string;
  allowedPageIds: string[];
  seen: Record<string, string>;
  jobId?: string;
  action?: "reject" | "dismiss";
  records?: PublicationRecord[];
}

/** Read only a bounded event, not a full session transcript. */
async function request(): Promise<Request> {
  return readBoundedJson<Request>(process.stdin, eventByteLimit(process.argv[2]));
}

/** Run one operation with compiler diagnostics kept off the JSON protocol. */
async function main(): Promise<void> {
  const input = await request();
  const result = await withQuiet(() => dispatch(input));
  await new Promise<void>((resolve, reject) => {
    process.stdout.write(JSON.stringify(result) + "\n", error => error ? reject(error) : resolve());
  });
  // Retrieval and trial accounting are complete. Aborted HTTP handles must not
  // consume the hook's remaining deadline after its JSON has been flushed.
  if (process.argv[2] === "context") process.exit(0);
}

const operations = new Map<string, (input: Request) => Promise<unknown>>([
  ["context", buildHookContext],
  ["resolve", resolveRequest],
  ["process", input => processJob(input.job, input.config)],
  ["materialize", materializeRequest],
]);

/** Reject unknown operations instead of treating them as an intake request. */
async function dispatch(input: Request): Promise<unknown> {
  const operation = operations.get(process.argv[2]);
  if (!operation) throw new Error("Unknown workflow operation");
  return operation(input);
}

/** An absent record set is malformed, not an instruction to erase the local view. */
async function materializeRequest(input: Request): Promise<unknown> {
  if (!Array.isArray(input.records)) throw new Error("Publication record set required");
  return materializeRecords(input.config, input.records);
}

async function resolveRequest(input: Request): Promise<{ resolved: boolean }> {
  if (!input.jobId || !input.action) throw new Error("Review identifier and action required");
  return { resolved: await resolvePendingReview(input.config.stateDir, input.jobId, input.action) };
}

main().catch((error: unknown) => {
  process.stderr.write(error instanceof Error ? error.name + "\n" : "WorkflowError\n");
  process.exitCode = 1;
});
