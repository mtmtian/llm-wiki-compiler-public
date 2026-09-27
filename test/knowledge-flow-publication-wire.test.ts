/** Cross-language acceptance: Python exports/validates a quote packet and TypeScript renders the same packet. */
import { spawnSync } from "node:child_process";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { expect, it } from "vitest";
import { materializeRecords } from "../extensions/knowledge-flow/materialize.js";
import { processJob } from "../extensions/knowledge-flow/pipeline.js";
import { stableTopicId } from "../extensions/knowledge-flow/topic-revision.js";
import { sha256Text } from "../src/connectors/hash.js";
import { parseFrontmatter } from "../src/utils/markdown.js";
import type { PublicationRecord } from "../extensions/knowledge-flow/publication-types.js";
import type { FlowEvidence, FlowJob, FlowResult } from "../extensions/knowledge-flow/types.js";
import { makeKnowledgeFlowConfig } from "./knowledge-flow-test-fixtures.js";

const PAGE = "concepts/example-budget";
const ORIGINAL = "---\ntitle: Budget\nprojectId: example\n---\n\nOld budget 100 ^[old.md:1]\n";

/** Seed the exact baseline page and original evidence used by wire round trips. */
async function wireFixture(suffix: string) {
  const config = await makeKnowledgeFlowConfig(suffix);
  await mkdir(path.join(config.wikiRoot, "sources"));
  await writeFile(path.join(config.wikiRoot, "sources/old.md"), "Old budget 100\n");
  await writeFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), ORIGINAL);
  return config;
}

/** Use the actual Python export and receiving validator, with no shared filesystem writes. */
function exportAndReceive(result: FlowResult): PublicationRecord {
  const code = `import json,sys
from replica import _publication_payload
from replica_records import validate_packet,canonical
from common import digest
config={"machineId":"test","model":"test"}
job={"id":"approval","projectId":"example","projectLabel":"Example","createdAt":"2026-09-17T00:00:00Z"}
payload=_publication_payload(config,job,json.load(sys.stdin),"b"*64)
packet={"id":digest(canonical(payload)),"payload":payload}
print(json.dumps(validate_packet(packet,"test","b"*64)))`;
  const child = spawnSync("python3", ["-c", code], { input: JSON.stringify(result), encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.resolve("extensions/knowledge-flow") } });
  expect(child.error).toBeUndefined(); expect(child.status, child.stderr).toBe(0);
  return JSON.parse(child.stdout) as PublicationRecord;
}

/** A next-day user approval must retain its preceding assistant proposal. */
function approvalResult(): FlowResult {
  const evidence: FlowEvidence[] = [
    { id: "approval", kind: "user", text: "同意你说的预算调整", sha256: "", locator: "fixture://approval", observedAt: "2026-09-17T00:00:00Z" },
    { id: "proposal", kind: "assistant", text: "建议每日预算从100调整为300", sha256: "", locator: "fixture://proposal", observedAt: "2026-09-16T00:00:00Z" },
  ];
  for (const item of evidence) item.sha256 = sha256Text(item.text);
  const claim = { text: "用户批准每日预算300", quote: evidence[0].text, evidenceId: "approval", title: "Budget",
    topic: "budget", decisionObject: "daily budget", slug: "budget", targetPageId: PAGE,
    kind: "decision" as const, status: "decided" as const, useWhen: "Pilot", rationale: "Approved proposal",
    supportingQuotes: [{ evidenceId: "proposal", quote: evidence[1].text }] };
  return { status: "submitted", reviewCount: 0, publishedPageIds: [], contribution: { claims: [claim], evidence,
    topicRevisions: [{ pageId: PAGE, topicId: stableTopicId("example", "budget", "daily budget"), title: "Budget",
      topic: "budget", decisionObject: "daily budget", basisHash: sha256Text(ORIGINAL), claimIndexes: [0],
      body: "## Current\n\nBudget 300 {{claim:0}}\n\n## History\n\nOld budget 100 ^[old.md:1]" }] } };
}

it("Given a prior proposal and next-day approval, When exported, received and replayed, Then the same page contains current and historical decisions", async () => {
  const config = await wireFixture("wiki-wire-approval-");
  const packet = exportAndReceive(approvalResult());
  expect(await materializeRecords(config, [packet])).toEqual({ pages: 1, conflicts: [] });
  const page = await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8");
  expect(page).toContain("Budget 300"); expect(page).toContain("Old budget 100 ^[old.md:1]");
  expect(parseFrontmatter(page).body.match(/\^\[/g)).toHaveLength(3);
  const source = (await readdir(path.join(config.wikiRoot, "sources"))).find(name => name !== "old.md")!;
  const quotes = await readFile(path.join(config.wikiRoot, "sources", source), "utf8");
  expect(quotes).toContain("同意你说的预算调整"); expect(quotes).toContain("建议每日预算从100调整为300");
});

it("Given a reviewed retirement, When exported and received, Then its reason and replacement survive replay unchanged", async () => {
  const config = await wireFixture("wiki-wire-retirement-");
  const result = approvalResult(); const revision = result.contribution!.topicRevisions![0];
  revision.body = "## Budget decision\n\nThe approved budget increased from 100 to 300. {{claim:0}}";
  revision.citationRetirements = [{ citation: "^[old.md:1]", reason: "The approved proposal retains both the previous budget and its replacement.", replacement: "{{claim:0}}" }];
  const packet = exportAndReceive(result);
  expect(packet.payload.topicRevisions![0].citationRetirements).toEqual(revision.citationRetirements);
  expect(await materializeRecords(config, [packet])).toEqual({ pages: 1, conflicts: [] });
  const page = await readFile(path.join(config.wikiRoot, "wiki", `${PAGE}.md`), "utf8");
  expect(page).toContain("increased from 100 to 300"); expect(page).not.toContain("^[old.md:1]");
  expect(await readFile(path.join(config.wikiRoot, "sources/old.md"), "utf8")).toBe("Old budget 100\n");
});

it.each(["user", "assistant"] as const)("Given a legacy frozen job with %s support, Then process, export and replay preserve both quotes", async (kind) => {
  const config = await makeKnowledgeFlowConfig("wiki-wire-legacy-");
  config.machineId = "test";
  config.exchange = { root: path.join(config.stateDir, "exchange"), protocolVersion: 2, participants: ["test"] };
  const input = approvalResult().contribution!;
  input.evidence[1].kind = kind;
  const claims = input.claims.map(claim => ({ ...claim, targetPageId: null }));
  const job: FlowJob = { id: "legacy", projectId: "example", projectLabel: "Example", cwd: config.wikiRoot,
    sessionId: "prior-session", turnId: "approval", createdAt: "2026-09-17T00:00:00Z", prompt: input.evidence[0].text,
    lastAssistant: "", evidence: input.evidence, allowedPageIds: [] };
  const submitted = await processJob(job, config, { extract: async () => claims,
    review: async () => [{ index: 0, decision: "accept", reason: "Both quotes checked", conflictingPageIds: [] }] });
  expect(submitted.status).toBe("submitted");
  const packet = exportAndReceive(submitted);
  expect(packet.payload.evidence).toHaveLength(2);
  expect(await materializeRecords(config, [packet])).toEqual({ pages: 1, conflicts: [] });
});
