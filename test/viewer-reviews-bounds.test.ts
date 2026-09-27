/**
 * Bounded-read contract for the `/api/reviews` candidate loader.
 *
 * `#/reviews` re-reads disk on every visit, and `heldReasons: "all"` is a real
 * policy code meaning "hold every page" — so a 5,000-page corpus compiled under
 * it puts 5,000 candidate files behind one request. `listCandidates` reads and
 * JSON-parses every one of them, which is the cost `listCandidatePage` exists
 * to remove.
 *
 * Capping the RESPONSE alone would not remove it: the files would still be read
 * and parsed before being sliced away. So these tests assert on the read path
 * itself — `safeReadFile` is spied on, and only the served slice may reach it.
 * They also pin the ordering guarantee the bounded loader actually provides
 * (ascending candidate file id, NOT `generatedAt`) so it cannot drift back into
 * an undocumented promise.
 */

import { describe, expect, it, vi } from "vitest";
import { makeTempRoot } from "./fixtures/temp-root.js";
import { CANDIDATES_DIR } from "../src/utils/constants.js";

vi.mock("../src/utils/markdown.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/utils/markdown.js")>();
  return { ...actual, safeReadFile: vi.fn(actual.safeReadFile) };
});

import { safeReadFile } from "../src/utils/markdown.js";
import { listCandidatePage, writeFreshCandidate } from "../src/compiler/candidates.js";

/** Candidate files read from disk since the counter was last reset. */
function candidateReadCount(): number {
  return vi
    .mocked(safeReadFile)
    .mock.calls.filter(([file]) => file.includes(CANDIDATES_DIR)).length;
}

/**
 * Seed `count` pending candidates and reset the read counter, so a test counts
 * only the reads its own call made. `writeFreshCandidate` rather than
 * `writeCandidate`: the latter scans the whole queue per write to canonicalize
 * duplicates, which is O(n²) over a queue this size and would drown the very
 * reads under test. Every slug here is distinct, so there is nothing to dedup.
 */
async function seedQueue(count: number): Promise<string> {
  const root = await makeTempRoot("reviews-bounds");
  for (let i = 0; i < count; i++) {
    const slug = `candidate-${String(i).padStart(3, "0")}`;
    await writeFreshCandidate(root, {
      title: `Candidate ${i}`,
      slug,
      summary: `Summary ${i}`,
      sources: ["karpathy.md"],
      body: `---\ntitle: Candidate ${i}\n---\n\nBody ${i}.`,
    });
  }
  vi.mocked(safeReadFile).mockClear();
  return root;
}

describe("listCandidatePage — bounded disk I/O", () => {
  it("reads only the served slice, not the whole queue", async () => {
    const root = await seedQueue(12);
    const page = await listCandidatePage(root, 5);
    expect(page.candidates).toHaveLength(5);
    expect(candidateReadCount()).toBe(5);
  });

  it("reports the true total without reading past the slice", async () => {
    const root = await seedQueue(12);
    const page = await listCandidatePage(root, 5);
    expect(page.total).toBe(12);
    expect(candidateReadCount()).toBe(5);
  });

  it("reads the whole queue only when the whole queue fits under the limit", async () => {
    const root = await seedQueue(4);
    const page = await listCandidatePage(root, 5);
    expect(page.candidates).toHaveLength(4);
    expect(page.total).toBe(4);
    expect(candidateReadCount()).toBe(4);
  });

  it("reads nothing when no candidates directory exists", async () => {
    const root = await makeTempRoot("reviews-bounds-empty");
    vi.mocked(safeReadFile).mockClear();
    expect(await listCandidatePage(root, 5)).toEqual({ candidates: [], total: 0 });
    expect(candidateReadCount()).toBe(0);
  });
});

describe("listCandidatePage — ordering guarantee", () => {
  it("serves the first N by ascending candidate file id, the order it documents", async () => {
    const root = await seedQueue(12);
    const page = await listCandidatePage(root, 5);
    const slugs = page.candidates.map((candidate) => candidate.slug);
    expect(slugs).toEqual([
      "candidate-000",
      "candidate-001",
      "candidate-002",
      "candidate-003",
      "candidate-004",
    ]);
  });

  it("serves the same slice on every call, so a revisit is not a reshuffle", async () => {
    const root = await seedQueue(12);
    const first = await listCandidatePage(root, 5);
    const second = await listCandidatePage(root, 5);
    expect(second.candidates.map((c) => c.id)).toEqual(first.candidates.map((c) => c.id));
  });
});
