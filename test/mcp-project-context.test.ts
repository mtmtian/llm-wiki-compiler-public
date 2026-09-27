/** Project-aware evidence is discoverable and identical at the hook and MCP read boundary. */
import { describe, expect, it } from "vitest";
import { buildServer, callTool, useMcpRoot } from "./fixtures/mcp-test-env.js";
import { GAME_PAGE, GAME_PROJECT, LANGUAGE_DECISION, LANGUAGE_QUESTION, seedTaskWiki } from "./fixtures/task-context-wiki.js";
import type { TaskContext } from "../src/context/task-types.js";

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
});
