/** Durable local trial accounting. SQLite reservations survive crashes and serialize concurrent hooks. */
import { DatabaseSync } from "node:sqlite";
import { mkdirSync, chmodSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

export interface JevSettings { enabled: boolean; budgetUsd: number; expiresAt: string }
interface Ledger {
  spentNano: number; requests: number; successes: number; fallbacks: number;
  inputTokens: number; outputTokens: number; cooldownUntil: number;
  stoppedReason: string; lastReason: string; lastAt: string; pending: Record<string, number>;
}
const empty = (): Ledger => ({ spentNano: 0, requests: 0, successes: 0, fallbacks: 0,
  inputTokens: 0, outputTokens: 0, cooldownUntil: 0, stoppedReason: "", lastReason: "", lastAt: "", pending: {} });
export const NANO_PER_INPUT_TOKEN = 42;

/** One short transaction; no lock is held across network or credential operations. */
function transaction<T>(directory: string, update: (value: Ledger) => T): T {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const file = path.join(directory, "jev-trial.sqlite");
  const db = new DatabaseSync(file);
  try {
    chmodSync(file, 0o600);
    db.exec("PRAGMA busy_timeout=100; CREATE TABLE IF NOT EXISTS trial (id INTEGER PRIMARY KEY, value TEXT NOT NULL)");
    db.exec("BEGIN IMMEDIATE");
    const row = db.prepare("SELECT value FROM trial WHERE id=1").get();
    const value: Ledger = row ? JSON.parse(String(row.value)) : empty();
    if (!validLedger(value)) throw new Error("Invalid trial ledger");
    const result = update(value);
    db.prepare("INSERT OR REPLACE INTO trial(id,value) VALUES(1,?)").run(JSON.stringify(value));
    db.exec("COMMIT");
    return result;
  } finally { db.close(); }
}

/** Never reset corrupted spending records to a fresh allowance. */
function validLedger(value: Ledger): boolean {
  return value && typeof value.pending === "object" && value.pending !== null &&
    [value.spentNano, value.requests, value.successes, value.fallbacks, value.inputTokens,
      value.outputTokens, value.cooldownUntil, ...Object.values(value.pending)].every(n => Number.isSafeInteger(n) && n >= 0) &&
    typeof value.stoppedReason === "string";
}

/** Status is shared with the local switch CLI; it contains no prompts or credentials. */
export function trialStatus(directory: string): Ledger { return transaction(directory, value => ({ ...value })); }

/** Validate finite grant settings before any paid request. */
export function stopReason(settings: JevSettings, state: Ledger): string {
  if (!Number.isFinite(settings.budgetUsd) || settings.budgetUsd <= 0 || settings.budgetUsd > 5 ||
    !Number.isFinite(Date.parse(settings.expiresAt))) return "invalid-config";
  if (Date.now() >= Date.parse(settings.expiresAt)) return "expired";
  if (state.stoppedReason) return state.stoppedReason;
  if (state.cooldownUntil > Date.now()) return "cooldown";
  return "";
}

/** Reserve conservative input cost atomically; unknown outcomes keep their reservation. */
export function reserveTrial(directory: string, settings: JevSettings, estimateNano: number) {
  return transaction(directory, state => {
    let reason = stopReason(settings, state);
    if (!reason && state.spentNano + estimateNano > Math.floor(settings.budgetUsd * 1e9)) {
      reason = "budget-exhausted"; state.stoppedReason = reason;
    }
    if (reason) return { reason, id: "" };
    const id = randomUUID();
    state.spentNano += estimateNano; state.pending[id] = estimateNano; state.requests++;
    return { id, reason: "" };
  });
}

/** Settle once. Billing/auth stops persist; transient failures briefly suspend the trial. */
export function settleTrial(directory: string, id: string, reason: string, usage?: { input_tokens: number; output_tokens: number }) {
  transaction(directory, state => {
    const reserved = state.pending[id];
    if (reserved === undefined) return;
    delete state.pending[id];
    if (usage) {
      state.spentNano += usage.input_tokens * NANO_PER_INPUT_TOKEN - reserved;
      state.inputTokens += usage.input_tokens; state.outputTokens += usage.output_tokens;
    }
    state.lastReason = reason || "success"; state.lastAt = new Date().toISOString();
    if (reason) state.fallbacks++; else state.successes++;
    if (["credit-exhausted", "authentication-failed"].includes(reason)) state.stoppedReason = reason;
    else if (reason) state.cooldownUntil = Date.now() + 60_000;
  });
}
