/** Captured dialogue adds useful analysis without promoting an assistant to an authority. */
import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import { diagnoseClaims, validateClaims } from "../extensions/knowledge-flow/extract.js";
import type { FlowEvidence } from "../extensions/knowledge-flow/types.js";

const text = "比较投放效果时应拆分同龄 cohort，避免观察窗口不同造成偏差。";
const evidence: FlowEvidence = { id: "message-1", kind: "assistant", text,
  locator: "codex://session/turn/message-1", observedAt: "2026-09-16T00:00:00Z",
  sha256: createHash("sha256").update(text).digest("hex") };
const lesson = { text, evidenceId: evidence.id, quote: text, title: "同龄窗口分析建议",
  topic: "投放分析", slug: "cohort-window", targetPageId: null, kind: "lesson", status: "decided",
  useWhen: "比较观察天数不同的投放批次", rationale: "保留本次分析方法及适用边界" };

describe("captured conversation evidence", () => {
  it("retains a quoted analytical lesson as dated history", () => {
    expect(validateClaims([lesson], [evidence], [], 5)).toMatchObject([{ kind: "lesson", status: "historical" }]);
  });

  it.each(["decision", "fact", "constraint"])("cannot establish an assistant-supported %s", (kind) => {
    expect(validateClaims([{ ...lesson, kind }], [evidence], [], 5)).toEqual([]);
  });

  it("preserves explicit uncertainty for independent review", () => {
    expect(validateClaims([{ ...lesson, status: "uncertain" }], [evidence], [], 5)[0].status).toBe("uncertain");
  });

  it("does not accept changed or uncited conversation text", () => {
    expect(validateClaims([lesson], [{ ...evidence, text: "已部署完成" }], [], 5)).toEqual([]);
    expect(validateClaims([{ ...lesson, evidenceId: "missing" }], [evidence], [], 5)).toEqual([]);
  });

  it("retains the authority of an original user decision", () => {
    expect(validateClaims([{ ...lesson, kind: "decision" }], [{ ...evidence, kind: "user" }], [], 5)[0].status).toBe("decided");
  });

  it("reports precise correction reasons without accepting unsafe proposals", () => {
    const result = diagnoseClaims([
      { ...lesson, quote: "改写后的摘要" },
      { ...lesson, kind: "decision" },
      { ...lesson, targetPageId: "concepts/other" },
    ], [evidence], [], 5);
    expect(result.claims).toEqual([]);
    expect(result.diagnostics.map(item => item.code)).toEqual(["quote_mismatch", "assistant_authority", "out_of_scope_page"]);
  });

  it.each(["assistant", "artifact"] as const)("rejects %s primary with assistant support before publication", (kind) => {
    const primaryText = `${kind} primary evidence`;
    const supportText = "assistant supporting context";
    const primary = { ...evidence, id: `${kind}-primary`, kind, text: primaryText, sha256: createHash("sha256").update(primaryText).digest("hex") };
    const support = { ...evidence, id: `${kind}-support`, text: supportText, sha256: createHash("sha256").update(supportText).digest("hex") };
    const claim = { ...lesson, text: primaryText, evidenceId: primary.id, quote: primaryText,
      kind: kind === "assistant" ? "lesson" : "fact", status: "historical",
      supportingQuotes: [{ evidenceId: support.id, quote: supportText }] };
    const result = diagnoseClaims([claim], [primary, support], [], 5);
    expect(result.claims).toEqual([]);
    expect(result.diagnostics[0]).toMatchObject({ code: "assistant_support_authority", evidenceId: primary.id });
  });

  it("retains assistant supporting context for a user primary", () => {
    const primaryText = "用户确认采用该方案";
    const supportText = "assistant proposed the option";
    const primary: FlowEvidence = { ...evidence, id: "user-primary", kind: "user", text: primaryText, sha256: createHash("sha256").update(primaryText).digest("hex") };
    const support = { ...evidence, id: "user-support", text: supportText, sha256: createHash("sha256").update(supportText).digest("hex") };
    const claim = { ...lesson, text: primaryText, evidenceId: primary.id, quote: primaryText, kind: "decision", status: "decided",
      supportingQuotes: [{ evidenceId: support.id, quote: supportText }] };
    expect(validateClaims([claim], [primary, support], [], 5)[0].supportingQuotes).toEqual(claim.supportingQuotes);
  });
});
