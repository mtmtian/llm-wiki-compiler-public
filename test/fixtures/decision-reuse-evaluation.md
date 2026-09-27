# Chinese decision-reuse retrieval baseline

This evaluation checks whether Chinese project questions retrieve the right cited decision from a small synthetic wiki. It calls the real `buildTaskContext` and `buildHookContext` functions over temporary Markdown pages, source files, and source-state hashes. No business material, credentials, paid model, or live embedding provider is used.

The corpus now has 37 cases. Three semantic-scope cases were added after the frozen baseline below: two short generic prompts (“预算定了”, “代理配置了”) must not inject another project's growth page, and one explicit growth question from the repository project must still reach it. The frozen numbers remain the original 34-case report. The 34 original cases cover current and historical decisions on one page, why the existing configuration path is reused, broad project-structure wording, unrelated growth-marketing material, cross-project shared principles, explicit no-evidence questions, repeated turns, and a later source appended beside a historical decision. Time is represented by “历史决定（2024）” and “当前决定（2026）” sections with separate cited sources; the fixture does not invent validity-date metadata.

## Scope and metrics

The default cases use **project scope** with explicit project ownership. Four additional cases use the hook's `topicScope: "semantic"` mode to check whether shared principles remain reachable across projects and whether unrelated pages stay out. The temporary wiki has no embedding store, so “semantic scope” here means the topic-scope contract; ranking uses the production lexical fallback. This baseline does not measure vector recall.

The broad question “这个项目的结构怎么安排？” is marked `project-boundary`: it only checks that growth-project evidence does not enter a code-project context. It does not assume that a broad question must retrieve one particular architecture section. Other nonempty expected sections are evaluated in three separate ways:

- **Exact section set:** the returned section set exactly matches the expected sections. Denominator: exact-evidence cases only; the broad boundary-only case is excluded.
- **Expected-section recall:** every expected section appears, even if related extra sections also appear. Denominator: cases with at least one expected section; no-hit and boundary-only cases are excluded.
- **Forbidden contamination:** no explicitly forbidden page appears. Denominator: cases that define at least one forbidden page, across project and semantic scopes.
- **No-evidence behavior:** no decision section is returned for a question marked `expectNoEvidence`. Denominator: those explicitly marked cases only.

The test treats forbidden contamination, required no-hit behavior, cross-project shared-principle recall, and historical fact recall after the later source as regression contracts. The report still records exact-set differences as diagnostics; extra related sections are not called cross-project leakage.

## Frozen baseline

The checked-in report [decision-reuse-baseline.json](decision-reuse-baseline.json) was produced from production commit `32e20b1815160002e93194d008893c3bed5f4e5f`, archived before the current production edits, and run with the harness in this change. Results:

| Metric | Task context | Hook context | Denominator |
|---|---:|---:|---:|
| Exact section set | 15 | 15 | 33 |
| Expected-section recall | 26 | 26 | 28 |
| Forbidden contamination | 0 | 0 | 9 |
| Required no-evidence failures | 4 | 4 | 5 |

The 4 no-evidence failures are `growth-terms-no-hit-in-code`, `no-evidence-credential`, `no-evidence-unrelated-code`, and `no-evidence-after-follow-up`. The combined current/history question also missed the current test decision while retrieving the historical section. After later material was appended, the old history remained retrievable, while some timeline questions returned both old and current sections. The growth-project budget question returned its expected section and the neighboring agent/material section; this is an exact-set difference, not forbidden contamination.

The regression test is expected to fail on this frozen baseline because it asserts the four no-evidence contracts. The static JSON preserves all 34 case outcomes and is not an adoption-rate or real-user accuracy claim.

## Run

From the repository root, run the evaluation against the checked-out production code:

```sh
node scripts/eval-decision-reuse.mjs /tmp/decision-reuse-report.json
```

The script records the production Git revision and marks it `+production-dirty` if the retrieval source files have uncommitted changes. It delegates execution to the same Vitest report test, so test and report share one corpus and one runner. `npm run eval:reuse -- /tmp/decision-reuse-report.json` is the package alias for this command.

Run this command sequentially with other test and build commands. The shared Vitest global setup rebuilds `dist/`; another CLI test running at the same time can lose `dist/cli.js` during that build's clean step.

To reproduce the frozen baseline, export the production tree with `git archive` at the full commit above, copy only this evaluation harness into that isolated tree, install the locked dependencies, and run the command with `DECISION_REUSE_SOURCE_REVISION` set to that commit. The reference corpus SHA-256 is `752949f4125469ef59a3f7b7ad497d2e63b5471dbdff56e5296d52366ef47dd2`. Do not label a report from modified production code with the unchanged HEAD alone.
