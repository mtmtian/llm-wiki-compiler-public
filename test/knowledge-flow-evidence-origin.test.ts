/**
 * The drafting and review models see where each piece of evidence came from: the turns being
 * consolidated now ("current") or earlier turns of the session ("earlier"). Most held batches cite
 * a long earlier or unrelated passage as a claim's primary quote; marking origin lets the drafter
 * prefer evidence that states the claim. The marker is prompt-only and never reaches a publication.
 */
import { describe, expect, it } from "vitest";
import { consolidateSession } from "../extensions/knowledge-flow/consolidate.js";
import { sha256Text } from "../src/connectors/hash.js";
import { accepted, config, draft, job, original, pageId, plan } from "./knowledge-flow-consolidation-fixtures.js";

const earlierText = "上周已经决定样例素材测试只在工作日投放。";

function sessionJob() {
  const input = job();
  input.sessionContext!.evidence = [{ id: "user-1", kind: "user", text: earlierText, sha256: sha256Text(earlierText),
    observedAt: "2025-01-08T00:00:00Z", locator: "codex://s/t1" }];
  return input;
}

type Seen = Array<[string, string | undefined]>;
const origins = (request: any): Seen =>
  (request.evidence as any[]).map(item => [item.id, item.origin]);

describe("evidence origin in drafting and review prompts", () => {
  it("Given earlier session evidence, When drafted and reviewed, Then every item is marked current or earlier", async () => {
    const seen: Record<string, Seen> = {};
    const runtime = config({ knowledge_topic_plan: plan(),
      knowledge_topic_edit: (request: any) => { seen.edit = origins(request); return draft(); },
      knowledge_topic_review: (request: any) => { seen.review = origins(request); return accepted(); } });
    const result = await consolidateSession(sessionJob(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(seen.edit).toEqual([["user-1", "earlier"], ["user-2", "current"]]);
    expect(seen.review).toEqual(seen.edit);
    expect(result.contribution?.evidence.every(item => !("origin" in item))).toBe(true);
  });

  it("Given a correction, Then its frozen quote catalog keeps the same origins", async () => {
    let reviews = 0;
    let catalog: Seen = [];
    const runtime = config({ knowledge_topic_plan: plan(),
      knowledge_topic_edit: (request: any) => { if (request.correction) catalog = origins(request); return draft(); },
      knowledge_topic_review: () => reviews++ === 0 ? { ...accepted(), decision: "reject", reason: "请改写" } : accepted() });
    const result = await consolidateSession(sessionJob(), runtime, new Map([[pageId, original]]));
    expect(result.status).toBe("submitted");
    expect(catalog).toEqual([["user-1", "earlier"], ["user-2", "current"]]);
  });
});
