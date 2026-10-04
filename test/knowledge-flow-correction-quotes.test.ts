/** Correction quote IDs preserve source bytes and restore their original source identity. */
import { describe, expect, it } from "vitest";
import { buildCorrectionEvidence, resolveQuote } from "../extensions/knowledge-flow/consolidation-quotes.js";
import { resolveQuoteBoundDraft, validatedDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import type { QuoteBoundTopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import { sha256Text } from "../src/connectors/hash.js";
import type { FlowEvidence, FlowJob } from "../extensions/knowledge-flow/types.js";
import type { PlannedPage } from "../extensions/knowledge-flow/consolidation-plan.js";

function evidence(id: string, text: string): FlowEvidence {
  return { id, kind: "user", text, locator: `turn:${id}`, observedAt: "2026-09-21T00:00:00Z", sha256: sha256Text(text) };
}

function page(pageId: string, topic = "规范主题", decisionObject = "规范对象"): PlannedPage {
  return { pageId, topicId: "topic-id", title: "规范页", topic, decisionObject, basisHash: null, original: null };
}

describe("correction quote catalog", () => {
  it("keeps code comments, Markdown and newlines byte-for-byte across bounded fragments", () => {
    const text = "```ts\nconst enabled = true; // preserve this comment\n```\n\n# 标题\n" + "x".repeat(700);
    const options = buildCorrectionEvidence([evidence("e1", text)])[0].quoteOptions;
    expect(options.every(option => option.quote.length <= 600)).toBe(true);
    expect(options.map(option => option.quote).join("")).toBe(text);
    expect(new Set(options.map(option => option.quoteId)).size).toBe(options.length);
  });

  it("keeps CRLF in the catalog while returning a publication-safe exact substring", () => {
    const text = "代码 // 注释\r\n下一行\r\n";
    const catalog = buildCorrectionEvidence([evidence("crlf", text)]);
    const options = catalog[0].quoteOptions;
    expect(options.map(option => option.quote).join("")).toBe(text);
    expect(options.every(option => !resolveQuote(catalog, option.quoteId).quote.includes("\r"))).toBe(true);
  });

  it("does not create an empty quote for a terminal standalone carriage return", () => {
    const text = "尾部内容\r";
    const catalog = buildCorrectionEvidence([evidence("terminal-cr", text)]);
    expect(catalog[0].quoteOptions.map(option => option.quote).join("")).toBe(text);
    expect(resolveQuote(catalog, catalog[0].quoteOptions[0].quoteId).quote).toBe("尾部内容");
  });

  it("keeps a surrogate pair intact at the fragment boundary", () => {
    const text = "x".repeat(599) + "😀" + "tail";
    const options = buildCorrectionEvidence([evidence("emoji", text)])[0].quoteOptions;
    expect(options.every(option => option.quote.length <= 600)).toBe(true);
    expect(options.map(option => option.quote).join("")).toBe(text);
    expect(options.some(option => option.quote.includes("\ud83d") && !option.quote.includes("😀"))).toBe(false);
  });

  it("rejects unknown quote IDs and restores each selected source binding", () => {
    const catalog = buildCorrectionEvidence([evidence("e1", "原文一"), evidence("e2", "原文二")]);
    const first = catalog[0].quoteOptions[0];
    expect(() => resolveQuote(catalog, "unknown-quote")).toThrow(/unknown/);
    expect(resolveQuote(catalog, first.quoteId)).toMatchObject({ evidenceId: "e1", quote: "原文一" });
    expect(resolveQuote(catalog, catalog[1].quoteOptions[0].quoteId)).toMatchObject({ evidenceId: "e2", quote: "原文二" });
  });

  it("restores selected quote text and supporting evidence before normal validation", () => {
    const primary = evidence("e1", "保留原始代码 // 注释\n下一行");
    const support = evidence("e2", "支持上下文");
    const catalog = buildCorrectionEvidence([primary, support]);
    const first = catalog[0].quoteOptions[0]; const second = catalog[1].quoteOptions[0];
    const resolved = resolveQuoteBoundDraft({ claims: [{
      text: primary.text, quoteId: first.quoteId, title: "保留代码", slug: "code", targetPageId: "concepts/code",
      kind: "decision", status: "decided", useWhen: "需要保留时", rationale: "用户明确要求", replacementIntent: false,
      supportingQuotes: [{ quoteId: second.quoteId }],
    }], pages: [], summary: "保留代码" }, catalog, [page("concepts/code", "规范主题", "规范对象")]);
    const { draft: restored } = resolved;
    expect(restored.claims[0].evidenceId).toBe("e1");
    expect(restored.claims[0].quote).toBe(first.quote);
    expect(restored.claims[0].supportingQuotes).toEqual([{ evidenceId: "e2", quote: support.text }]);
  });

  it("restores canonical destination metadata instead of trusting model labels", () => {
    const source = evidence("e1", "用户确认保留实现"); const catalog = buildCorrectionEvidence([source]);
    const option = catalog[0].quoteOptions[0];
    const modelDraft = { claims: [{
      text: source.text, quoteId: option.quoteId, title: "规范页", slug: "code", targetPageId: "concepts/code",
      kind: "decision", status: "decided", useWhen: "需要保留时", rationale: "用户确认", replacementIntent: false,
      supportingQuotes: [],
    }], pages: [], summary: "保留实现" } satisfies QuoteBoundTopicDraft;
    const { draft: resolved } = resolveQuoteBoundDraft(modelDraft, catalog, [page("concepts/code")]);
    expect(resolved.claims[0]).toMatchObject({ topic: "规范主题", decisionObject: "规范对象", targetPageId: "concepts/code" });
  });

  it("rejects a target page that is outside the frozen planned pages", () => {
    const source = evidence("e1", "用户确认保留实现"); const catalog = buildCorrectionEvidence([source]);
    const option = catalog[0].quoteOptions[0];
    expect(() => resolveQuoteBoundDraft({ claims: [{
      text: source.text, quoteId: option.quoteId, title: "规范页", slug: "code", targetPageId: "concepts/unknown",
      kind: "decision", status: "decided", useWhen: "需要保留时", rationale: "用户确认", replacementIntent: false,
      supportingQuotes: [],
    }], pages: [], summary: "保留实现" }, catalog, [page("concepts/code")])).toThrow(/unknown correction target page/);
  });

  it("keeps multi-page claim assignment validation after restoring destinations", () => {
    const first = evidence("e1", "用户确认主题一"); const second = evidence("e2", "用户确认主题二");
    const catalog = buildCorrectionEvidence([first, second]); const pages = [
      page("concepts/one", "主题一", "对象一"), page("concepts/two", "主题二", "对象二"),
    ];
    const claims = [first, second].map((item, index) => ({
      text: item.text, quoteId: catalog[index].quoteOptions[0].quoteId, title: "规范页", slug: `page-${index + 1}`,
      targetPageId: pages[index].pageId, kind: "decision" as const, status: "decided" as const,
      useWhen: "需要保留时", rationale: "用户确认", replacementIntent: false, supportingQuotes: [],
    }));
    const { draft: restored } = resolveQuoteBoundDraft({ claims, pages: [], summary: "保留两项" }, catalog, pages);
    const crossed = { ...restored, pages: [
      { pageId: pages[1].pageId, body: "{{claim:0}}", claimIndexes: [0] },
      { pageId: pages[0].pageId, body: "{{claim:1}}", claimIndexes: [1] },
    ] };
    const job = { evidence: [first, second], allowedPageIds: pages.map(item => item.pageId) } as FlowJob;
    expect(() => validatedDraft(crossed, job, pages, 5)).toThrow(/ownership mismatch/);
  });
});
