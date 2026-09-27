/** Exercise identical retirement URL acceptance at the Node/Python transport boundary. */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { expect, it } from "vitest";
import { validRetirementUrl } from "../extensions/knowledge-flow/citation-retirement.js";
import { validatePageRetirements } from "../extensions/knowledge-flow/page-retirement.js";

it("keeps real HTTPS references and rejects ambiguous transport spellings on both runtimes", () => {
  const valid = ["https://github.com/example/wiki/pull/7", "https://例子.测试/docs?q=1", "https://[::1]:8443/x"];
  const invalid = ["http://example.com/x", "HTTPS://example.com/x", "https://user@example.com/x", "https://example.com/a b",
    "https:///missing-host", "https://bad_host.example/x", "https://example.com:", "https://example.com../x",
    "https://example.com:99999/x", "https://ex%61mple.com/x", "https://example.com\\other/path", "https://example.com/\u0000x"];
  const values = [...valid, ...invalid];
  const child = spawnSync("python3", ["-c", "import json,sys\nfrom citation_retirement_contract import _https_url\nprint(json.dumps([_https_url(v) for v in json.load(sys.stdin)]))"],
    { input: JSON.stringify(values), encoding: "utf8", env: { ...process.env, PYTHONPATH: path.resolve("extensions/knowledge-flow") } });
  expect(child.error).toBeUndefined(); expect(child.status, child.stderr).toBe(0);
  const expected = [...valid.map(() => true), ...invalid.map(() => false)];
  expect(values.map(validRetirementUrl)).toEqual(expected);
  expect(JSON.parse(child.stdout)).toEqual(expected);
});

it("rejects path-dangerous page names consistently for migration targets and retirements", () => {
  const valid = ["concepts/发布决策", "concepts/release notes"];
  const invalid = ["concepts/foo:bar", "concepts/foo/bar", "concepts/foo\\bar", "concepts/foo\u0000bar"];
  const values = [...valid, ...invalid];
  const retiredPage = (pageId: string) => ({ pageId, projectId: "example", sha256: "a".repeat(64),
    reason: "过程由原 PR 承接", externalReference: "https://example.com/pr/7" });
  const accepts = (pageId: string): boolean => {
    try { validatePageRetirements([retiredPage(pageId)], new Set()); return true; } catch { return false; }
  };
  const script = `import json,sys
from revision_contract import _page_id
from citation_retirement_contract import validate_retired_pages
def accepts(validator, item):
    try:
        validator(item)
        return True
    except ValueError:
        return False
validators = [lambda item: _page_id(item["pageId"]), lambda item: validate_retired_pages([item], set())]
print(json.dumps([[accepts(validator, item) for validator in validators] for item in json.load(sys.stdin)]))`;
  const child = spawnSync("python3", ["-c", script], { input: JSON.stringify(values.map(retiredPage)), encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.resolve("extensions/knowledge-flow") } });
  const expected = [...valid.map(() => true), ...invalid.map(() => false)];
  expect(child.error).toBeUndefined(); expect(child.status, child.stderr).toBe(0);
  expect(values.map(accepts)).toEqual(expected); expect(JSON.parse(child.stdout)).toEqual(expected.map(value => [value, value]));
});
