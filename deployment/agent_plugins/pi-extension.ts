/**
 * Pi adapter: inject shared Wiki context before a turn and submit only its
 * settled, active-branch user/assistant text. The Python launcher owns config
 * loading and delegates all routing/intake to the current knowledge worker.
 */

import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { BeforeAgentStartEvent, ExtensionAPI, ExtensionContext, SessionEntry } from "@earendil-works/pi-coding-agent";

type MessageEntry = Extract<SessionEntry, { type: "message" }>;
type Outcome = "completed" | "aborted" | "error";

interface PendingTurn {
  cwd: string;
  prompt: string;
  profile: string;
  sessionId: string;
  turnId: string;
  baselineLeafId?: string;
  promptEntryId?: string;
  baselineIds: Set<string>;
  outcome?: Outcome;
}

interface EvidenceItem {
  id: string;
  kind: "user" | "assistant";
  text: string;
  locator: string;
  sha256: string;
  observedAt: string;
  complete: true;
  current: true;
}

const MAX_EVIDENCE_CHARS = 120_000;
const pendingTurns = new Map<string, PendingTurn>();

function textOf(entry: MessageEntry): string {
  if (entry.message.role !== "user" && entry.message.role !== "assistant") return "";
  const content = entry.message.content;
  if (typeof content === "string") return content.trim();
  return content.filter((part) => part.type === "text").map((part) => part.text).join("\n").trim();
}

function messageEntries(branch: SessionEntry[]): MessageEntry[] {
  return branch.filter((entry): entry is MessageEntry => entry.type === "message");
}

/** A relocated session cannot provide evidence for its former workspace. */
function sessionHeader(ctx: ExtensionContext) {
  const header = ctx.sessionManager.getHeader();
  if (!header) return undefined;
  return header.cwd === ctx.cwd ? header : undefined;
}

function beginTurn(eventPrompt: string, hasImages: boolean, ctx: ExtensionContext): PendingTurn | undefined {
  if (hasImages || !eventPrompt.trim()) return undefined;
  const header = sessionHeader(ctx);
  if (!header) return undefined;
  const branch = ctx.sessionManager.getBranch();
  const entries = messageEntries(branch);
  const sessionId = header.id;
  return { cwd: ctx.cwd, prompt: eventPrompt, profile: profilePath(), sessionId, turnId: randomUUID(),
    baselineLeafId: leafId(branch), baselineIds: new Set(entries.map((entry) => entry.id)) };
}

/** The pre-prompt leaf is absent in a brand-new session. */
function leafId(branch: SessionEntry[]): string | undefined {
  return branch.at(-1)?.id;
}

/** The original baseline must remain on the active branch after settlement. */
function branchSuffix(turn: PendingTurn, branch: SessionEntry[]): SessionEntry[] | undefined {
  const start = turn.baselineLeafId ? branch.findIndex((entry) => entry.id === turn.baselineLeafId) + 1 : 0;
  if (turn.baselineLeafId && start === 0) return undefined;
  return branch.slice(start);
}

/** Session identity and workspace must still match the prompt callback. */
function isSameSession(turn: PendingTurn, ctx: ExtensionContext): boolean {
  const header = sessionHeader(ctx);
  if (!header) return false;
  return header.id === turn.sessionId && ctx.cwd === turn.cwd;
}

/** Image evidence is outside this text-only bridge's completeness contract. */
function containsImage(entry: MessageEntry): boolean {
  if (entry.message.role !== "user") return false;
  const content = entry.message.content;
  return Array.isArray(content) && content.some((part) => part.type === "image");
}

/** Select the new native prompt, excluding repeated text from earlier turns. */
function promptFor(turn: PendingTurn, entries: MessageEntry[]): MessageEntry | undefined {
  const prompt = entries.find((entry) => entry.message.role === "user");
  if (!prompt || containsImage(prompt)) return undefined;
  return textOf(prompt) === turn.prompt.trim() ? prompt : undefined;
}

/** A second user message makes the single-turn boundary ambiguous. */
function selectPromptMessages(turn: PendingTurn, entries: MessageEntry[]): MessageEntry[] | undefined {
  const prompt = promptFor(turn, entries);
  if (!prompt) return undefined;
  turn.promptEntryId = prompt.id;
  const selected = entries.slice(entries.indexOf(prompt));
  if (selected.slice(1).some((entry) => entry.message.role === "user")) return undefined;
  return selected;
}

/** Preserve the active branch and original pre-turn boundary independently. */
function scopedMessages(turn: PendingTurn, ctx: ExtensionContext): MessageEntry[] | undefined {
  if (!isSameSession(turn, ctx)) return undefined;
  const suffix = branchSuffix(turn, ctx.sessionManager.getBranch());
  if (!suffix) return undefined;
  if (suffix.some((entry) => turn.baselineIds.has(entry.id))) return undefined;
  return selectPromptMessages(turn, messageEntries(suffix));
}

/** Only a completed text answer proves a successful turn boundary. */
function isFinalAnswer(entry: MessageEntry | undefined): boolean {
  if (!entry || entry.message.role !== "assistant") return false;
  return entry.message.stopReason === "stop";
}

/** Never retain partial or interrupted assistant responses as complete evidence. */
function isIncompleteAnswer(entry: MessageEntry): boolean {
  if (entry.message.role !== "assistant") return false;
  return ["aborted", "error", "length", "deferred"].includes(entry.message.stopReason);
}

function completeMessages(selected: MessageEntry[]): MessageEntry[] | undefined {
  const assistants = selected.filter((entry) => entry.message.role === "assistant" && textOf(entry));
  if (!isFinalAnswer(assistants.at(-1))) return undefined;
  if (assistants.some(isIncompleteAnswer)) return undefined;
  return [selected[0], ...assistants];
}

/** Reject oversized turns before constructing any allegedly complete evidence. */
function boundedEvidence(turn: PendingTurn, visible: MessageEntry[]): EvidenceItem[] | undefined {
  if (visible[0].message.role !== "user") return undefined;
  const total = visible.reduce((sum, entry) => sum + textOf(entry).length, 0);
  if (total > MAX_EVIDENCE_CHARS) return undefined;
  return visible.map((entry) => {
    const text = textOf(entry);
    const kind = entry.message.role as "user" | "assistant";
    return { id: entry.id, kind, text,
      locator: `pi://${encodeURIComponent(turn.sessionId)}/entry/${encodeURIComponent(entry.id)}`,
      sha256: createHash("sha256").update(text).digest("hex"),
      observedAt: new Date(entry.message.timestamp).toISOString(), complete: true, current: true };
  });
}

function evidenceFor(turn: PendingTurn, ctx: ExtensionContext): EvidenceItem[] | undefined {
  const selected = scopedMessages(turn, ctx);
  if (!selected) return undefined;
  const visible = completeMessages(selected);
  return visible ? boundedEvidence(turn, visible) : undefined;
}

function configPath(): string {
  return process.env.LLMWIKI_CONFIG ?? join(homedir(), ".config/llmwiki/knowledge-flow.json");
}

function launcherPath(): string {
  return process.env.LLMWIKI_AGENT_LAUNCHER ?? join(homedir(), ".local/bin/llmwiki-agent");
}

function profilePath(): string {
  return resolve(process.env.PI_CODING_AGENT_DIR ?? join(homedir(), ".pi/agent"));
}

/** Parse the bridge's sole JSON object without accepting arrays or primitives. */
function responseObject(stdout: string): Record<string, unknown> | undefined {
  const parsed: unknown = JSON.parse(stdout);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
  return parsed as Record<string, unknown>;
}

/** Keep transport failure separate from host lifecycle and evidence selection. */
async function executeBridge(pi: ExtensionAPI, ctx: ExtensionContext, file: string) {
  const result = await pi.exec(launcherPath(), ["--host", "pi", "--config", configPath(),
    "--profile", profilePath(), "--input-file", file], { cwd: ctx.cwd, timeout: 15_000 });
  if (result.code !== 0 || result.killed) return undefined;
  return responseObject(result.stdout);
}

/** Remove private transport files even when the child or JSON decoding fails. */
async function exchangeEvent(pi: ExtensionAPI, ctx: ExtensionContext, payload: Record<string, unknown>, directory: string) {
  const file = join(directory, "event.json");
  try {
    await writeFile(file, JSON.stringify(payload), { mode: 0o600, flag: "wx" });
    return await executeBridge(pi, ctx, file);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}

/** Wiki failures, including local temporary storage, must not block a Pi turn. */
async function invokeBridge(pi: ExtensionAPI, ctx: ExtensionContext,
  payload: Record<string, unknown>): Promise<Record<string, unknown> | undefined> {
  try {
    const directory = await mkdtemp(join(tmpdir(), "llmwiki-pi-"));
    return await exchangeEvent(pi, ctx, payload, directory);
  } catch {
    return undefined;
  }
}

function eventPayload(turn: PendingTurn, action: "prompt" | "stop", evidence?: EvidenceItem[]): Record<string, unknown> {
  return { action, sessionId: turn.sessionId, turnId: turn.turnId, profile: turn.profile, cwd: turn.cwd,
    prompt: turn.prompt, ...(evidence ? { evidence } : {}), ...(action === "stop" ? { outcome: turn.outcome } : {}) };
}

/** Extract only the standard shared hook context payload. */
function contextText(result: Record<string, unknown> | undefined): string | undefined {
  const response = result?.hookSpecificOutput as { additionalContext?: unknown } | undefined;
  const text = response?.additionalContext;
  return typeof text === "string" ? text.trim() : undefined;
}

/** Inject one hidden navigation message before the native user entry exists. */
async function startTurn(pi: ExtensionAPI, event: BeforeAgentStartEvent, ctx: ExtensionContext) {
  const turn = beginTurn(event.prompt, Boolean(event.images?.length), ctx);
  if (!turn) return;
  pendingTurns.set(turn.sessionId, turn);
  const result = await invokeBridge(pi, ctx, eventPayload(turn, "prompt"));
  const text = contextText(result);
  if (!text) return;
  return { message: { customType: "llmwiki-context", content: `<llmwiki-context>\n${text}\n</llmwiki-context>`,
    display: false } };
}

/** Find this session's pending turn without changing settlement state. */
function pendingFor(ctx: ExtensionContext): PendingTurn | undefined {
  const header = ctx.sessionManager.getHeader();
  return header ? pendingTurns.get(header.id) : undefined;
}

/** Submit only after the native run, retries, and continuations have settled. */
async function settleTurn(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  const turn = pendingFor(ctx);
  if (!turn) return;
  // Consume before awaiting transport: duplicate settlement callbacks have no turn.
  pendingTurns.delete(turn.sessionId);
  if (turn.outcome !== "completed") return;
  const evidence = evidenceFor(turn, ctx);
  if (!evidence) return;
  await invokeBridge(pi, ctx, { ...eventPayload(turn, "stop", evidence), promptEntryId: turn.promptEntryId });
}

/** Register the three native lifecycle boundaries without maintaining Wiki policy. */
export default function llmwikiPi(pi: ExtensionAPI): void {
  pi.on("before_agent_start", (event, ctx) => startTurn(pi, event, ctx));
  pi.on("agent_before_settle", (event, ctx) => {
    const turn = pendingFor(ctx);
    if (turn) turn.outcome = event.outcome;
  });
  pi.on("agent_settled", (_event, ctx) => settleTurn(pi, ctx));
}
