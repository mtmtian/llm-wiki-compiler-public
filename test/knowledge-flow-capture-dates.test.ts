/** Behavior tests for separating evidence capture metadata from dated report and effective claims. */
import { expect, it } from "vitest";
import type { TopicDraft } from "../extensions/knowledge-flow/consolidation-draft.js";
import type { PlannedPage } from "../extensions/knowledge-flow/consolidation-plan.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";
import { sha256Text } from "../src/connectors/hash.js";
import { assertEvidenceDates } from "../extensions/knowledge-flow/consolidation-dates.js";

const PAGE_ID = "concepts/workstream";
const OBSERVED_AT = "2025-01-15T12:00:00Z";

function evidence(id: string, observedAt = OBSERVED_AT, text = "保留这个结论。"): FlowEvidence {
  return { id, kind: "user", text, observedAt, locator: `turn:${id}`, sha256: sha256Text(text) };
}

function claim(evidenceId: string, text: string, quote = "保留这个结论。", supportingQuotes: FlowDraftClaim["supportingQuotes"] = []) {
  return { text, evidenceId, quote, title: "结论", topic: "工作流", decisionObject: "日期", slug: "date",
    targetPageId: PAGE_ID, kind: "fact" as const, status: "historical" as const, useWhen: "复核时", rationale: "有来源",
    supportingQuotes };
}

type FlowDraftClaim = TopicDraft["claims"][number];

function draft(body: string, claims: FlowDraftClaim[]): TopicDraft {
  return { claims, summary: "测试草稿", pages: [{ pageId: PAGE_ID, body, claimIndexes: claims.map((_item, index) => index) }] };
}

function page(original: string | null = null): PlannedPage {
  return { pageId: PAGE_ID, topicId: "topic", title: "工作流", topic: "工作流", decisionObject: "日期",
    basisHash: null, original };
}

it("Given an unchanged old paragraph, When checking a draft, Then it skips that paragraph", () => {
  const old = "报告于 2025-01-15，旧记录保持原文。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(old, [claim("e1", "旧记录")]),
    [page(`---\nupdatedAt: 2024-12-01T00:00:00Z\n---\n\n${old}`)], [evidence("e1")])).not.toThrow();
});

it("Given a new claim under a dated report heading, When the heading inherits capture metadata, Then it rejects", () => {
  const body = "## 报告于 2025-01-15\n\n本轮结论仍然有效。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "本轮结论")]), [page()], [evidence("e1")]))
    .toThrow(/claim 0.*2025-01-15/i);
});

it("Given a heading and prose share a paragraph, When capture time becomes its report date, Then it rejects", () => {
  const body = "## 报告\n助手报告于 2025-01-15 已完成。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "助手当时报告已完成")]), [page()], [evidence("e1")]))
    .toThrow(/claim 0.*2025-01-15/i);
});

it("Given two claims with separate evidence, When one claim borrows the other's capture date, Then it stays isolated", () => {
  const claims = [claim("e1", "报告于 2025-01-16 的内容"), claim("e2", "另一结论")];
  const body = "报告于 2025-01-16 的内容。{{claim:0}}\n\n另一结论。{{claim:1}}";
  expect(() => assertEvidenceDates(draft(body, claims), [page()], [evidence("e1"), evidence("e2", "2025-01-16T00:00:00Z")]))
    .not.toThrow();
});

it("Given a report date that differs from metadata, When checking the draft, Then it does not infer an error", () => {
  const body = "该信息报告于 2024-12-31。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "该信息报告于 2024-12-31")]), [page()], [evidence("e1")]))
    .not.toThrow();
});

it.each(["捕获于", "采集于", "captured on"])("Given capture wording '%s', When checking the draft, Then it accepts observedAt", wording => {
  const body = `该信息${wording} 2025-01-15。{{claim:0}}`;
  expect(() => assertEvidenceDates(draft(body, [claim("e1", body)]), [page()], [evidence("e1")]))
    .not.toThrow();
});

it("Given publication wording for updatedAt, When checking the draft, Then it accepts the page date", () => {
  const body = "此页面发布于 2025/1/15。{{claim:0}}";
  const existing = "---\ntitle: Work\nupdatedAt: 2025-01-15T12:00:00Z\n---\n\n旧结论。";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "页面已发布")]), [page(existing)], [evidence("e1", "2024-12-01T00:00:00Z")]))
    .not.toThrow();
});

it("Given a metadata date used as a report date, When checking the draft, Then it rejects", () => {
  const body = "助手报告于 2025/1/15 该项已完成。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "助手报告于 2025/1/15 该项已完成")]), [page()], [evidence("e1")]))
    .toThrow(/claim 0.*2025-01-15/i);
});

it("Given the same date is both captured and reported, When checking the draft, Then the report use still rejects", () => {
  const body = "captured on 2025-01-15; reported on 2025-01-15. {{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", body)]), [page()], [evidence("e1")]))
    .toThrow(/claim 0.*2025-01-15/i);
});

it.each([
  "助手在 2025-01-15 的历史报告称设置已改变。{{claim:0}}",
  "## 助手历史复核报告（2025-01-15）\n\n助手称设置已改变。{{claim:0}}",
  "2025-01-15 的历史复核报告称设置已改变。{{claim:0}}",
  "在 2025-01-15 的助手检查报告中，设置已改变。{{claim:0}}",
  "在 2025-01-15 12:00 的助手检查报告中，设置已改变。{{claim:0}}",
])("Given a dated report noun phrase, When its only date is capture metadata, Then it rejects: %s", body => {
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "助手当时报告设置改变")]), [page()], [evidence("e1")]))
    .toThrow(/claim 0.*2025-01-15/i);
});

it("Given a report explicitly described as captured on the date, Then it accepts that capture attribution", () => {
  const body = "助手在 2025-01-15 的捕获记录中报告设置改变，本批未独立核验。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", body)]), [page()], [evidence("e1")])).not.toThrow();
});

it("Given only a supporting quote states the report date, When checking the draft, Then it accepts that source date", () => {
  const claimWithSupport = claim("e1", "报告于 2025年1月15日", "方案已确认。", [{ evidenceId: "e2", quote: "报告日期为 2025年1月15日。" }]);
  const body = "报告于 2025年1月15日。{{claim:0}}";
  expect(() => assertEvidenceDates(draft(body, [claimWithSupport]), [page()],
    [evidence("e1"), evidence("e2", "2025-01-15T00:00:00Z", "报告日期为 2025年1月15日。")])).not.toThrow();
});

it("Given a page updatedAt used as an effective date, When checking the draft, Then it rejects", () => {
  const body = "规则自 2025-01-15 生效。{{claim:0}}";
  const existing = "---\ntitle: Work\nupdatedAt: 2025-01-15T12:00:00Z\n---\n\n旧结论。";
  expect(() => assertEvidenceDates(draft(body, [claim("e1", "规则生效")]), [page(existing)], [evidence("e1", "2024-12-01T00:00:00Z")]))
    .toThrow(/claim 0.*2025-01-15/i);
});
