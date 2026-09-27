/** Synthetic Chinese decisions and query cases for a repeatable retrieval baseline. */

export const REPO_PROJECT = "llm-wiki-compiler";
export const GROWTH_PROJECT = "growth-demo";

export interface DecisionReuseCase {
  id: string;
  prompt: string;
  projectId: string;
  scope?: "project" | "semantic";
  expected: Array<{ pageId: string; section: string }>;
  acceptance?: "exact-evidence" | "project-boundary";
  forbiddenPageIds?: string[];
  expectNoEvidence?: boolean;
  repeat?: boolean;
  phase?: "initial" | "after-follow-up";
}

export interface SyntheticPage {
  slug: string;
  fields: Record<string, unknown>;
  sections: Array<{ heading: string; source: string; text: string }>;
}

export const DECISION_REUSE_PAGES: SyntheticPage[] = [
  {
    slug: "repo-architecture",
    fields: { title: "知识编译链架构", projectId: REPO_PROJECT },
    sections: [
      { heading: "当前知识链", source: "architecture-chain.md",
        text: "现有链路由原始来源、编译后的 Markdown 主题页、引用证据和 hook 检索组成；主题页投影回指来源，Agent读取有引用的决策章节。" },
      { heading: "不另建第二管道", source: "no-second-pipeline.md",
        text: "决策：复用现有配置链，不新建独立记忆数据库或第二条写入管道。双事实源会增加同步冲突、项目入口分叉和维护负担；先在既有入口补足可验证的检索评测。" },
      { heading: "扩展入口", source: "extension-entry.md",
        text: "新增决策检索接入 buildTaskContext 和 buildHookContext，由一个章节选择结果供 hook 与 MCP 共用；不另做平行的检索服务。" },
      { heading: "降低沟通成本", source: "communication-cost.md",
        text: "持续推进时把决策对象、原因、适用范围、来源和替代关系留在主题页；新任务先检索并补读原页，避免重复解释项目结构和以临时补丁掩盖旧约束。" },
    ],
  },
  {
    slug: "repo-test-decisions",
    fields: { title: "决策评测与测试入口", projectId: REPO_PROJECT },
    sections: [
      { heading: "当前测试入口", source: "test-entry.md",
        text: "当前决策是沿用 Vitest；真实检索验收放在 test/fixtures 的 Markdown 与来源夹具，测试入口调用生产上下文构建函数。" },
      { heading: "当前报告入口", source: "report-entry.md",
        text: "报告由独立 scripts/eval-decision-reuse.mjs 启动同一份 Vitest runner，输出每个问题的命中、漏召回与误召回 JSON。" },
      { heading: "历史测试尝试", source: "old-test-attempt.md",
        text: "历史上曾尝试把每个问题都写成独立测试，但重复夹具难维护；后续改为共享合成语料和单 runner。" },
    ],
  },
  {
    slug: "shared-project-principle",
    fields: { title: "跨项目复用决策原则", topicScope: "semantic",
      sourceProjectIds: [REPO_PROJECT, GROWTH_PROJECT] },
    sections: [
      { heading: "跨项目复用原则", source: "shared-principle.md",
        text: "跨项目可复用原则：先查现有配置与调用入口，确认约束和责任边界后再扩展；记录为什么复用或拆分，避免重复管道和局部补丁逐渐破坏项目结构。" },
    ],
  },
  {
    slug: "growth-marketing",
    fields: { title: "增长投放项目决策结构", projectId: GROWTH_PROJECT },
    sections: [
      { heading: "广告预算决策", source: "growth-budget.md",
        text: "增长项目的投放结构按地区、代理和素材检查；预算决策关注注册成本、试用转化、成熟回收窗口与广告回收倍数。" },
      { heading: "代理与素材结构", source: "growth-agency.md",
        text: "增长项目把代理、广告系列与素材结构作为并列诊断维度；不能仅凭点击率决定迁移预算。" },
    ],
  },
  {
    slug: "decision-timeline",
    fields: { title: "知识页与决策历史", projectId: REPO_PROJECT, updatedAt: "2026-09-01" },
    sections: [
      { heading: "历史决定（2024）", source: "timeline-2024.md",
        text: "2024年评估后，选择 Markdown 主题页作为可审阅的知识投影；Git diff、人工审核和 Obsidian 链接可以直接保留证据与上下文。" },
    ],
  },
];

export const DECISION_REUSE_CASES: DecisionReuseCase[] = [
  { id: "architecture-why-reuse", projectId: REPO_PROJECT, prompt: "为什么沿用现有知识链，不另建一条记忆管道？",
    expected: [{ pageId: "concepts/repo-architecture", section: "不另建第二管道" }] },
  { id: "architecture-pipeline", projectId: REPO_PROJECT, prompt: "原始来源、主题页和 hook 之间如何串起来？",
    expected: [{ pageId: "concepts/repo-architecture", section: "当前知识链" }] },
  { id: "architecture-entry", projectId: REPO_PROJECT, prompt: "新增决策检索要接入哪个既有入口？",
    expected: [{ pageId: "concepts/repo-architecture", section: "扩展入口" }] },
  { id: "architecture-communication", projectId: REPO_PROJECT, prompt: "怎么减少持续推进项目时反复解释背景的沟通成本？",
    expected: [{ pageId: "concepts/repo-architecture", section: "降低沟通成本" }] },
  { id: "architecture-patch-debt", projectId: REPO_PROJECT, prompt: "怎样避免功能小补丁累积后破坏代码结构？",
    expected: [{ pageId: "concepts/repo-architecture", section: "降低沟通成本" }] },
  { id: "current-test-entry", projectId: REPO_PROJECT, prompt: "当前的决策验收测试放在哪个目录、使用什么入口？",
    expected: [{ pageId: "concepts/repo-test-decisions", section: "当前测试入口" }] },
  { id: "current-report-entry", projectId: REPO_PROJECT, prompt: "现在从哪里运行决策检索报告，报告包含什么？",
    expected: [{ pageId: "concepts/repo-test-decisions", section: "当前报告入口" }] },
  { id: "history-old-test-approach", projectId: REPO_PROJECT, prompt: "历史上为什么不再为每个决策问题单独写测试？",
    expected: [{ pageId: "concepts/repo-test-decisions", section: "历史测试尝试" }] },
  { id: "history-pipeline-reason", projectId: REPO_PROJECT, prompt: "以前为什么没有保留独立记忆数据库方案？",
    expected: [{ pageId: "concepts/repo-architecture", section: "不另建第二管道" }] },
  { id: "same-page-current-test", projectId: REPO_PROJECT, prompt: "现在测试用例和报告由什么实现？",
    expected: [{ pageId: "concepts/repo-test-decisions", section: "当前测试入口" },
      { pageId: "concepts/repo-test-decisions", section: "当前报告入口" }] },
  { id: "same-page-history-test", projectId: REPO_PROJECT, prompt: "旧测试组织方法的问题是什么？",
    expected: [{ pageId: "concepts/repo-test-decisions", section: "历史测试尝试" }] },
  { id: "same-page-current-and-history", projectId: REPO_PROJECT,
    prompt: "测试现在怎么组织，过去尝试过什么？", repeat: true,
    expected: [{ pageId: "concepts/repo-test-decisions", section: "当前测试入口" },
      { pageId: "concepts/repo-test-decisions", section: "历史测试尝试" }] },
  { id: "generic-project-structure", projectId: REPO_PROJECT,
    prompt: "这个项目的结构怎么安排？", expected: [], acceptance: "project-boundary",
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "generic-project-extension", projectId: REPO_PROJECT,
    prompt: "这个项目的结构要怎么扩展新的功能？",
    expected: [{ pageId: "concepts/repo-architecture", section: "扩展入口" }],
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "generic-project-principle", projectId: REPO_PROJECT,
    prompt: "项目结构扩展时怎样复用已有决策？",
    expected: [{ pageId: "concepts/shared-project-principle", section: "跨项目复用原则" }],
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "growth-is-not-code", projectId: REPO_PROJECT,
    prompt: "代码项目的结构和决策如何避免被广告投放材料干扰？",
    expected: [{ pageId: "concepts/repo-architecture", section: "不另建第二管道" }],
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "growth-terms-no-hit-in-code", projectId: REPO_PROJECT,
    prompt: "本代码项目的广告回收倍数、代理预算与素材结构怎么决策？", expected: [],
    expectNoEvidence: true, forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "growth-in-own-project", projectId: GROWTH_PROJECT,
    prompt: "增长投放项目的广告回收倍数和代理预算如何一起检查？",
    expected: [{ pageId: "concepts/growth-marketing", section: "广告预算决策" }] },
  { id: "growth-creative-structure", projectId: GROWTH_PROJECT,
    prompt: "增长代理和素材在结构诊断里如何区分？",
    expected: [{ pageId: "concepts/growth-marketing", section: "代理与素材结构" }] },
  { id: "shared-principle-repo", projectId: REPO_PROJECT,
    prompt: "跨项目时应该先检查哪些配置与边界？",
    expected: [{ pageId: "concepts/shared-project-principle", section: "跨项目复用原则" }] },
  { id: "shared-principle-growth", projectId: GROWTH_PROJECT,
    prompt: "增长项目如何复用通用的系统配置原则？",
    expected: [{ pageId: "concepts/shared-project-principle", section: "跨项目复用原则" }] },
  { id: "shared-principle-project-split", projectId: REPO_PROJECT,
    prompt: "什么时候拆分能力，什么时候复用现有入口？",
    expected: [{ pageId: "concepts/shared-project-principle", section: "跨项目复用原则" }] },
  { id: "semantic-repo-shared-principle", projectId: REPO_PROJECT, scope: "semantic",
    prompt: "跨项目开发时怎样复用现有入口并记录责任边界？",
    expected: [{ pageId: "concepts/shared-project-principle", section: "跨项目复用原则" }],
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "semantic-repo-unrelated-growth", projectId: REPO_PROJECT, scope: "semantic",
    prompt: "知识编译链的引用证据如何回到原始来源？",
    expected: [{ pageId: "concepts/repo-architecture", section: "当前知识链" }],
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "semantic-growth-shared-principle", projectId: GROWTH_PROJECT, scope: "semantic",
    prompt: "增长项目如何复用跨项目的系统配置和责任边界原则？",
    expected: [{ pageId: "concepts/shared-project-principle", section: "跨项目复用原则" }],
    forbiddenPageIds: ["concepts/repo-architecture"] },
  { id: "semantic-growth-unrelated-code", projectId: GROWTH_PROJECT, scope: "semantic",
    prompt: "代理素材投放的预算和广告回收倍数、成熟窗口如何检查？",
    expected: [{ pageId: "concepts/growth-marketing", section: "广告预算决策" }],
    forbiddenPageIds: ["concepts/repo-architecture"] },
  { id: "semantic-repo-short-ack", projectId: REPO_PROJECT, scope: "semantic",
    prompt: "预算定了", expected: [], expectNoEvidence: true,
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "semantic-repo-generic-pair", projectId: REPO_PROJECT, scope: "semantic",
    prompt: "代理配置了", expected: [], expectNoEvidence: true,
    forbiddenPageIds: ["concepts/growth-marketing"] },
  { id: "semantic-repo-explicit-cross-project", projectId: REPO_PROJECT, scope: "semantic",
    prompt: "增长项目的广告预算决策要看哪些注册成本和成熟回收窗口指标？",
    expected: [{ pageId: "concepts/growth-marketing", section: "广告预算决策" }] },
  { id: "no-evidence-weather", projectId: REPO_PROJECT,
    prompt: "下周东京的降雨概率和气温是多少？", expected: [], expectNoEvidence: true },
  { id: "no-evidence-credential", projectId: REPO_PROJECT,
    prompt: "当前数据库管理员密码是多少？", expected: [], expectNoEvidence: true },
  { id: "no-evidence-unrelated-code", projectId: REPO_PROJECT,
    prompt: "如何用 Rust 编写蓝牙温度传感器驱动？", expected: [], expectNoEvidence: true },
  { id: "timeline-history-before-follow-up", projectId: REPO_PROJECT,
    prompt: "2024年为什么选择 Markdown 主题页作为知识投影？",
    expected: [{ pageId: "concepts/decision-timeline", section: "历史决定（2024）" }] },
  { id: "timeline-current-after-follow-up", projectId: REPO_PROJECT,
    prompt: "现在知识页采用哪种可审阅投影，后来追加了什么？", phase: "after-follow-up",
    expected: [{ pageId: "concepts/decision-timeline", section: "当前决定（2026）" }] },
  { id: "timeline-history-survives-follow-up", projectId: REPO_PROJECT,
    prompt: "后续材料追加后，2024年选择 Markdown 的原因还是什么？", phase: "after-follow-up",
    expected: [{ pageId: "concepts/decision-timeline", section: "历史决定（2024）" }] },
  { id: "timeline-current-history-distinction", projectId: REPO_PROJECT,
    prompt: "请分别说明 2024 年历史决定和 2026 年当前决定。", phase: "after-follow-up",
    expected: [{ pageId: "concepts/decision-timeline", section: "历史决定（2024）" },
      { pageId: "concepts/decision-timeline", section: "当前决定（2026）" }] },
  { id: "no-evidence-after-follow-up", projectId: REPO_PROJECT,
    prompt: "本机现在的磁盘剩余空间是多少？", phase: "after-follow-up", expected: [], expectNoEvidence: true },
];
