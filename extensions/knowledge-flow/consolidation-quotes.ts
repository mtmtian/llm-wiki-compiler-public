/** Frozen quote options used only by the bounded correction attempt. */
import type { FlowEvidence } from "./types.js";
import { sha256Text } from "../../src/connectors/hash.js";

const MAX_QUOTE_CHARS = 600;

export interface QuoteOption {
  quoteId: string;
  evidenceId: string;
  quote: string;
}

export interface CorrectionEvidence {
  id: string;
  kind: FlowEvidence["kind"];
  locator: string;
  sha256: string;
  observedAt: string;
  originalSha256?: string;
  origin?: FlowEvidence["origin"];
  quoteOptions: QuoteOption[];
}

/** Build deterministic, lossless quote options without duplicating source text. */
export function buildCorrectionEvidence(evidence: readonly FlowEvidence[]): CorrectionEvidence[] {
  return evidence.map(item => ({
    id: item.id, kind: item.kind, locator: item.locator, sha256: item.sha256, observedAt: item.observedAt,
    ...(item.originalSha256 ? { originalSha256: item.originalSha256 } : {}),
    ...(item.origin ? { origin: item.origin } : {}),
    quoteOptions: splitQuotes(item.id, item.text),
  }));
}

/** Restore the source binding from one globally unique quote selector. */
export function resolveQuote(
  catalog: readonly CorrectionEvidence[], quoteId: string,
): QuoteOption {
  const option = catalog.flatMap(item => item.quoteOptions).find(item => item.quoteId === quoteId);
  if (!option) throw new Error(`unknown correction quoteId: ${quoteId}`);
  return { ...option, quote: trimBoundaryCarriageReturn(option.quote) };
}

function splitQuotes(evidenceId: string, text: string): QuoteOption[] {
  const output: QuoteOption[] = [];
  let start = 0;
  let index = 0;
  while (start < text.length) {
    const end = nextBoundary(text, start);
    const quote = text.slice(start, end);
    output.push({ quoteId: `q-${sha256Text(JSON.stringify([evidenceId, index, quote])).slice(0, 32)}`, evidenceId, quote });
    start = end;
    index += 1;
  }
  return output;
}

function nextBoundary(text: string, start: number): number {
  const hardEnd = Math.min(start + MAX_QUOTE_CHARS, text.length);
  const firstCarriage = text.indexOf("\r", start + (text[start] === "\r" ? 1 : 0));
  const newline = text.lastIndexOf("\n", hardEnd - 1);
  // Keep a terminal standalone CR with the preceding fragment. Resolving a
  // fragment strips only boundary CR characters for the publication contract;
  // making CR its own option would otherwise produce an empty quote.
  const carriageBoundary = firstCarriage > start && firstCarriage < hardEnd
    && text.length - firstCarriage > 1 ? firstCarriage : -1;
  const boundary = carriageBoundary >= 0 ? carriageBoundary
    : newline >= start ? newline + 1 : hardEnd;
  return boundary < text.length && isLowSurrogate(text.charCodeAt(boundary)) ? boundary - 1 : boundary;
}

function trimBoundaryCarriageReturn(value: string): string {
  if (!value.includes("\r")) return value;
  const start = value.startsWith("\r") ? 1 : 0;
  const end = value.endsWith("\r") && value.length > start ? value.length - 1 : value.length;
  return value.slice(start, end);
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff;
}
