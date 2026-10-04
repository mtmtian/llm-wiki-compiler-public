/**
 * Deterministic quote repair before claim validation, and evidence anchoring for corrections.
 *
 * A claim's quote must be an exact substring of its evidence. Drafts often copy a passage with
 * markdown or spacing removed, or cite the right passage under the wrong evidence ID; one such
 * claim used to send the whole batch into correction, where evidence for every claim is chosen
 * again and the drafter tends to switch claims to a user's short question. Repair only restores
 * what is provable:
 * - a quote that matches its own evidence after removing markdown symbols, unifying quote marks
 *   and collapsing whitespace becomes the exact original span (once, unambiguously);
 * - a quote found verbatim in exactly one other evidence item is rebound to that item.
 * Paraphrases and fragments stitched with ellipses stay unchanged for the validator. Evidence
 * roles are applied afterwards, so a rebound claim gets the authority of its real source.
 *
 * A correction must not move a claim to unrelated evidence. preserveEvidence keeps a corrected
 * claim's original references when the review accepted the claim or explicitly retained its evidence
 * while requesting a prose repair. Other disputed claims may choose a different source. All corrected
 * claims still pass normal validation and a fresh independent whole-page review.
 */
import type { FlowClaim, FlowEvidence } from "./types.js";
import type { TopicDraft } from "./consolidation-draft.js";
import { resolveQuote } from "./consolidation-quotes.js";
import type { CorrectionEvidence } from "./consolidation-quotes.js";

const REMOVED = /[*_`#>]/;
const QUOTE_MARKS: Record<string, string> = { "“": "\"", "”": "\"", "「": "\"", "」": "\"", "‘": "'", "’": "'" };
// Shorter normalized quotes are too likely to match by accident.
const MIN_REPAIRED_QUOTE = 8;
// A corrected claim is "the same claim" when this share of the shorter text's trigrams recurs;
// on stored corrections 0.8 matched near-identical rewordings without pairing different claims.
const SAME_CLAIM = 0.8;
// A restored option must share at least this many consecutive characters with the previous quote.
const MIN_OPTION_OVERLAP = 20;
// Bounds the correction prompt; long evidence is split into many quote options.
const MAX_ANCHORED_QUOTES = 20;

type Reference = { evidenceId: string; quote: string };

/** Repair every claim's primary and supporting quotes of a draft against the batch evidence. */
export function withRepairedQuotes(draft: TopicDraft, evidence: readonly FlowEvidence[]): TopicDraft {
  return { ...draft, claims: repairQuotes(draft.claims, evidence) };
}

/** Repair primary and supporting quotes; claims that cannot be proven are returned unchanged. */
export function repairQuotes(claims: FlowClaim[], evidence: readonly FlowEvidence[]): FlowClaim[] {
  return claims.map(claim => {
    const primary = repairReference(claim, evidence);
    const supporting = claim.supportingQuotes?.map(item => repairReference(item, evidence));
    return { ...claim, ...primary, ...(supporting ? { supportingQuotes: supporting } : {}) };
  });
}

function repairReference(reference: Reference, evidence: readonly FlowEvidence[]): Reference {
  const own = evidence.find(item => item.id === reference.evidenceId);
  if (own?.text.includes(reference.quote)) return { evidenceId: reference.evidenceId, quote: reference.quote };
  const span = own ? exactSpan(own.text, reference.quote) : null;
  if (span) return { evidenceId: reference.evidenceId, quote: span };
  const holders = evidence.filter(item => item.text.includes(reference.quote));
  return holders.length === 1 ? { evidenceId: holders[0].id, quote: reference.quote }
    : { evidenceId: reference.evidenceId, quote: reference.quote };
}

/** The unique original span whose normalized form equals the normalized quote. */
function exactSpan(text: string, quote: string): string | null {
  const source = normalized(text);
  const target = normalized(quote).value;
  const at = source.value.indexOf(target);
  if (target.length < MIN_REPAIRED_QUOTE || at < 0 || source.value.indexOf(target, at + 1) >= 0) return null;
  let start = source.origin[at];
  let end = source.origin[at + target.length - 1];
  while (start > 0 && REMOVED.test(text[start - 1])) start -= 1;
  while (end + 1 < text.length && REMOVED.test(text[end + 1])) end += 1;
  return text.slice(start, end + 1);
}

/** Normalized text plus, for each kept character, its index in the original. */
function normalized(text: string): { value: string; origin: number[] } {
  let value = "";
  const origin: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (REMOVED.test(character)) continue;
    if (/\s/.test(character)) {
      if (value && !value.endsWith(" ")) { value += " "; origin.push(index); }
      continue;
    }
    value += QUOTE_MARKS[character] ?? character;
    origin.push(index);
  }
  if (value.endsWith(" ")) { value = value.slice(0, -1); origin.pop(); }
  return { value, origin };
}

/** For each previous claim, the quote options of the evidence it cited, so a correction keeps its source. */
export function claimAnchors(previous: TopicDraft, catalog: readonly CorrectionEvidence[]):
  Array<{ claimIndex: number; evidenceId: string; quoteIds: string[] }> {
  return previous.claims.flatMap((claim, claimIndex) => {
    const item = catalog.find(entry => entry.id === claim.evidenceId);
    return item ? [{ claimIndex, evidenceId: item.id,
      quoteIds: item.quoteOptions.slice(0, MAX_ANCHORED_QUOTES).map(option => option.quoteId) }] : [];
  });
}

/** What a correction is checked against: the batch evidence, its quote options, and the previous review. */
export interface PreservationContext {
  evidence: readonly FlowEvidence[];
  catalog: readonly CorrectionEvidence[];
  /** Previous claim indexes the review did not accept; their evidence may legitimately change. */
  disputed: ReadonlySet<number>;
  /** The reviewer explicitly found these references suitable for the requested prose repair. */
  retained?: ReadonlySet<number>;
}

/** Restore references only for an unambiguous same-page claim whose evidence the review retained. */
export function preserveEvidence(corrected: TopicDraft, previous: TopicDraft, context: PreservationContext): TopicDraft {
  const protectedClaims = previous.claims.filter((_, index) => !context.disputed.has(index) || context.retained?.has(index));
  return { ...corrected, claims: corrected.claims.map(claim => {
    const index = sameClaim(claim, previous.claims);
    const before = index === undefined ? undefined : previous.claims[index];
    if (!before || !protectedClaims.includes(before)) return claim;
    const source = context.evidence.find(item => item.id === before.evidenceId);
    if (!source) return claim;
    const quote = source.text.includes(before.quote) ? before.quote : bestOption(context.catalog, before);
    return quote ? { ...claim, evidenceId: before.evidenceId, quote, supportingQuotes: before.supportingQuotes } : claim;
  }) };
}

/** Index of the only previous claim whose text is nearly the same; none when there are zero or several. */
function sameClaim(claim: FlowClaim, previous: readonly FlowClaim[]): number | undefined {
  const matches = previous.flatMap((item, index) => item.targetPageId === claim.targetPageId
    && similarity(item.text, claim.text) >= SAME_CLAIM ? [index] : []);
  return matches.length === 1 ? matches[0] : undefined;
}

function similarity(left: string, right: string): number {
  const a = trigrams(left);
  const b = trigrams(right);
  if (!a.size || !b.size) return normalized(left).value === normalized(right).value ? 1 : 0;
  let shared = 0;
  for (const gram of a) if (b.has(gram)) shared += 1;
  return shared / Math.min(a.size, b.size);
}

function trigrams(text: string): Set<string> {
  const value = normalized(text).value;
  const grams = new Set<string>();
  for (let index = 0; index + 3 <= value.length; index += 1) grams.add(value.slice(index, index + 3));
  return grams;
}

/** The option of the previous evidence that shares the longest run of characters with its quote. */
function bestOption(catalog: readonly CorrectionEvidence[], before: FlowClaim): string | null {
  const options = catalog.find(item => item.id === before.evidenceId)?.quoteOptions ?? [];
  let best: { quoteId: string; score: number } | null = null;
  for (const option of options) {
    const score = longestCommonRun(option.quote, before.quote);
    if (score >= MIN_OPTION_OVERLAP && (!best || score > best.score)) best = { quoteId: option.quoteId, score };
  }
  return best ? resolveQuote(catalog, best.quoteId).quote : null;
}

function longestCommonRun(left: string, right: string): number {
  let best = 0;
  const row = new Array<number>(right.length + 1).fill(0);
  for (let i = 1; i <= left.length; i += 1) {
    let diagonal = 0;
    for (let j = 1; j <= right.length; j += 1) {
      const above = row[j];
      row[j] = left[i - 1] === right[j - 1] ? diagonal + 1 : 0;
      if (row[j] > best) best = row[j];
      diagonal = above;
    }
  }
  return best;
}
