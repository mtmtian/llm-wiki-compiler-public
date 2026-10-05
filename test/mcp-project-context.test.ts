/** Project-aware evidence is discoverable and identical at the hook and MCP read boundary. */
import { describe, expect, it } from "vitest";
import { appendFile } from "node:fs/promises";
import path from "node:path";
import { buildServer, callTool, useMcpRoot } from "./fixtures/mcp-test-env.js";
import { GAME_PAGE, GAME_PROJECT, LANGUAGE_DECISION, LANGUAGE_QUESTION, seedTaskWiki } from "./fixtures/task-context-wiki.js";
import type { TaskContext } from "../src/context/task-types.js";
import { CLAIM_REF, CLAIM_TEXT, writeReviewedClaims } from "./fixtures/reviewed-claims.js";

const root = useMcpRoot("mcp-project-context");

describe("project-scoped decision tools", () => {
  it("Given project-tagged pages, When listing projects, Then exposes IDs without guessing a page inventory", async () => {
    await seedTaskWiki(root.value);
    const response = await callTool(buildServer(root.value), "list_knowledge_projects", {});
    const result = response.structuredContent?.result as { projects: { projectId: string; pages: number }[] };
    expect(result.projects).toContainEqual(expect.objectContaining({ projectId: GAME_PROJECT, pages: 1 }));
    expect(JSON.stringify(result)).not.toContain("OTHER_PROJECT_PRIVATE");
  });

  it("Given two projects, When asking one project's decision, Then returns its decision and bound sources only", async () => {
    await seedTaskWiki(root.value);
    const response = await callTool(buildServer(root.value), "get_project_context", {
      projectId: GAME_PROJECT, prompt: LANGUAGE_QUESTION,
    });
    const result = response.structuredContent?.result as TaskContext;
    expect(result.evidence[0].text).toContain(LANGUAGE_DECISION);
    expect(result.evidence[0].sources.some(source => source.file === "language.md")).toBe(true);
    expect(JSON.stringify(result)).not.toContain("OTHER_PROJECT_PRIVATE");
    expect(result.projectId).toBe(GAME_PROJECT);
  });

  it("Given no semantic or lexical match, Then offers current pages inside the requested project for recovery", async () => {
    await seedTaskWiki(root.value);
    const response = await callTool(buildServer(root.value), "get_project_context", {
      projectId: GAME_PROJECT, prompt: "UnicornQuaternionZyx",
    });
    const result = response.structuredContent?.result as TaskContext;
    expect(result.evidence).toEqual([]);
    expect(result.followUpPageIds).toEqual([GAME_PAGE]);
    expect(JSON.stringify(result)).not.toContain("OTHER_PROJECT_PRIVATE");
  });

  it("Given an unknown project, When querying, Then reports no hit without broadening scope", async () => {
    await seedTaskWiki(root.value);
    const response = await callTool(buildServer(root.value), "get_project_context", {
      projectId: "unknown", prompt: LANGUAGE_QUESTION,
    });
    const result = response.structuredContent?.result as TaskContext;
    expect(result.evidence).toEqual([]);
    expect(result.status).toBe("no-hit");
  });

  it("Given a reviewed claim without a page, When listing projects, Then shows its count without its body", async () => {
    await writeReviewedClaims(root.value);
    const response = await callTool(buildServer(root.value), "list_knowledge_projects", {});
    const result = response.structuredContent?.result as { projects: { projectId: string; pages: number; claims: number }[] };

    expect(result.projects).toContainEqual({ projectId: "sample-game", label: "Sample Game", pages: 0, claims: 1 });
    expect(JSON.stringify(result)).not.toContain(CLAIM_TEXT);
  });

  it("Given a page-less project claim, When requesting context and its pointer, Then expands the same quote without a page id", async () => {
    await writeReviewedClaims(root.value);
    const server = buildServer(root.value);
    const response = await callTool(server, "get_project_context", {
      projectId: "sample-game", prompt: "当前小游戏存档保留规则是什么？",
    });
    const context = response.structuredContent?.result as TaskContext;
    const evidence = context.evidence[0] as TaskContext["evidence"][number] & { claimRef: string; quotes: { quote: string }[] };

    expect(context.status).toBe("ok");
    expect(evidence).toMatchObject({ origin: "ledger", claimRef: CLAIM_REF, text: CLAIM_TEXT,
      quotes: [{ quote: CLAIM_TEXT }] });
    expect(evidence).not.toHaveProperty("pageId");
    const expanded = await callTool(server, "read_knowledge_claim", { claimRef: evidence.claimRef });
    const result = expanded.structuredContent?.result as {
      status: string; claim: { claimRef: string; text: string }; revision: string; generationId: string; superseded: boolean;
    };

    expect(result).toMatchObject({ status: "ok", claim: { claimRef: CLAIM_REF, text: CLAIM_TEXT },
      revision: expect.any(String), generationId: path.basename(root.value), superseded: false });
  });

  it("Given an unknown claim reference, When expanded, Then returns no hit", async () => {
    await writeReviewedClaims(root.value);
    const response = await callTool(buildServer(root.value), "read_knowledge_claim", { claimRef: `${"f".repeat(64)}:7` });

    expect(response.structuredContent?.result).toMatchObject({ status: "no-hit" });
  });

  it("Given a tampered reviewed projection, When queried, Then reports degraded and returns no claim", async () => {
    await writeReviewedClaims(root.value);
    await appendFile(path.join(root.value, ".llmwiki/reviewed-claims.json"), " ");
    const server = buildServer(root.value);
    const inventory = await callTool(server, "list_knowledge_projects", {});
    const context = await callTool(server, "get_project_context", {
      projectId: "sample-game", prompt: "当前小游戏存档保留规则是什么？",
    });
    const response = await callTool(server, "read_knowledge_claim", { claimRef: CLAIM_REF });

    expect(inventory.structuredContent?.result).toMatchObject({ status: "degraded", warning: "reviewed-claims-invalid" });
    expect(context.structuredContent?.result).toMatchObject({ status: "degraded", evidence: [] });
    expect(response.structuredContent?.result).toMatchObject({ status: "degraded", warning: "reviewed-claims-invalid" });
    expect(JSON.stringify(response.structuredContent?.result)).not.toContain(CLAIM_TEXT);
  });
});
