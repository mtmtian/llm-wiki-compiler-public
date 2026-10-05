/** Cross-language acceptance: Python's actual validated projection and seal feed the TypeScript hook unchanged. */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { buildTaskContext } from "../src/context/task.js";
import { buildHookContext } from "../extensions/knowledge-flow/context.js";
import { useTempRoot } from "./fixtures/temp-root.js";

const root = useTempRoot();
const script = fileURLToPath(new URL("./fixtures/build-reviewed-generation.py", import.meta.url));

describe("sealed Python to TypeScript claim flow", () => {
  it("Given a real Python projection and no pages, When the hook runs, Then its reference resolves to the original evidence", async () => {
    const { stdout } = await promisify(execFile)("python3", ["-B", script, root.dir], { timeout: 20000 });
    const claimRef = stdout.trim();
    const prompt = "当前小游戏更新时如何保留玩家存档？";
    const result = await buildTaskContext({ root: root.dir, projectId: "sample-game", prompt });
    expect(result.status).toBe("ok");
    expect(result.evidence[0]).toMatchObject({ origin: "ledger", claimRef,
      quotes: [{ kind: "user", quote: "样例小游戏更新必须保留玩家存档。" }] });
    const hook = await buildHookContext({ config: { wikiRoot: root.dir }, projectId: "sample-game", prompt, allowedPageIds: [], seen: {} });
    expect(hook.context).toContain("仅用于样例小游戏更新");
    expect(hook.references).toEqual([expect.objectContaining({ claimRef })]);
    expect(hook.complete).toBe(true);
  });
});
