/**
 * Protocol-level tests for MCP page reads: the concept/query resource templates
 * and the `read_page` tool. A real SDK Client exercises URI normalization and
 * template matching over an in-memory transport.
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdir, symlink, writeFile } from "fs/promises";
import path from "path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { writePage } from "./fixtures/write-page.js";
import { buildServer, useMcpRoot } from "./fixtures/mcp-test-env.js";

/** Body planted in files a traversal must not be able to read. */
const SECRET_BODY = "SECRET-BYTES";

const rootHandle = useMcpRoot("llmwiki-mcp-confine");
let root: string;
let client: Client;

beforeEach(async () => {
  root = rootHandle.value;
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await buildServer(root).connect(serverTransport);
  client = new Client({ name: "confinement-test", version: "0.0.0" });
  await client.connect(clientTransport);
});

afterEach(async () => {
  await client.close();
});

/** Read a resource over the MCP protocol and parse its JSON page payload. */
async function readResource(uri: string): Promise<{ slug: string; body: string }> {
  const [content] = (await client.readResource({ uri })).contents;
  if (!("text" in content)) throw new Error(`expected a text resource for ${uri}`);
  return JSON.parse(content.text);
}

/** Plant secret pages above `wiki/concepts` for traversal checks. */
async function plantSecrets(): Promise<void> {
  const secret = `---\ntitle: Secret\n---\n${SECRET_BODY}\n`;
  await writeFile(path.join(root, "secret.md"), secret);
  await writeFile(path.join(root, "wiki", "secret.md"), secret);
}

describe("MCP page resources: traversal and confinement", () => {
  it("refuses encoded traversal through concept and query resources", async () => {
    await plantSecrets();
    for (const slug of ["..%2Fsecret", "..%2F..%2Fsecret", "..%5C..%5Csecret"]) {
      await expect(readResource(`llmwiki://concept/${slug}`)).rejects.toThrow(/Page not found/);
    }
    await expect(readResource("llmwiki://query/..%2F..%2Fsecret")).rejects.toThrow(/Page not found/);
  });

  it("refuses an encoded slug that names a nested page", async () => {
    const nestedDir = path.join(root, "wiki/concepts/nested");
    await mkdir(nestedDir, { recursive: true });
    await writePage(nestedDir, "inner", { title: "Inner", summary: "S" }, SECRET_BODY);
    await expect(readResource("llmwiki://concept/nested%2Finner")).rejects.toThrow(/Page not found/);
  });

  it("does not follow a page symlink outside its directory", async () => {
    await plantSecrets();
    await symlink(path.join(root, "secret.md"), path.join(root, "wiki/concepts/linked.md"));
    await expect(readResource("llmwiki://concept/linked")).rejects.toThrow(/Page not found/);
  });
});

describe("MCP page resources: encoded legitimate slugs", () => {
  it("reads Chinese, space, hash, and percent slugs from their encoded URIs", async () => {
    const slugs = ["知识图谱页面", "Foo #1", "50% off"];
    for (const slug of slugs) {
      await writePage(path.join(root, "wiki/concepts"), slug, { title: slug, summary: "S" }, `Body of ${slug}.`);
      await expect(readResource(`llmwiki://concept/${encodeURIComponent(slug)}`))
        .resolves.toMatchObject({ slug, body: `Body of ${slug}.` });
    }
  });

  it("reads a Chinese query page from its encoded URI", async () => {
    const slug = "机器学习问答";
    await writePage(path.join(root, "wiki/queries"), slug, { title: slug, summary: "S" }, "查询正文。");
    await expect(readResource(`llmwiki://query/${encodeURIComponent(slug)}`))
      .resolves.toMatchObject({ slug, body: "查询正文。" });
  });
});

describe("read_page tool confinement", () => {
  it("does not return files outside the page directories for traversal slugs", async () => {
    await plantSecrets();
    for (const slug of ["../secret", "../../secret", "..\\..\\secret"]) {
      const result = await client.callTool({ name: "read_page", arguments: { slug } });
      expect(result.isError).toBe(true);
      expect(JSON.stringify(result.content)).not.toContain(SECRET_BODY);
    }
  });

  it("does not return an external target from a page symlink", async () => {
    await plantSecrets();
    await symlink(path.join(root, "secret.md"), path.join(root, "wiki/concepts/linked.md"));
    const result = await client.callTool({ name: "read_page", arguments: { slug: "linked" } });
    expect(result.isError).toBe(true);
    expect(JSON.stringify(result.content)).not.toContain(SECRET_BODY);
  });
});
