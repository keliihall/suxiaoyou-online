import assert from "node:assert/strict";
import test from "node:test";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import {
  createFilePreviewReferenceResolver,
  isLocalFileReference,
} from "../../src/lib/file-preview-references.ts";

const workspace = "/tenant/workspaces/project-a";
const path = `${workspace}/交付材料/年度报告 v2.md`;

test("session files resolve absolute, workspace-relative, Unicode and encoded Markdown references", () => {
  const resolver = createFilePreviewReferenceResolver([{ path }], workspace);
  for (const value of [path, "交付材料/年度报告 v2.md", "./交付材料/年度报告 v2.md", "年度报告 v2.md", `sandbox:${path}`, encodeURI(path)]) {
    assert.deepEqual(resolver.resolve(value), { path, name: "年度报告 v2.md" }, value);
  }
});

test("only recorded paths resolve, and arbitrary sandbox/server paths never become reads", () => {
  const resolver = createFilePreviewReferenceResolver([{ path }], workspace);
  for (const value of ["/etc/passwd", "sandbox:/mnt/data/年度报告 v2.md", "/different/年度报告 v2.md", "../交付材料/年度报告 v2.md", "https://example.test/年度报告 v2.md", "file:///etc/passwd"]) {
    assert.equal(resolver.resolve(value), null, value);
  }
});

test("same-name files are never chosen by suffix, while exact paths remain available", () => {
  const first = `${workspace}/a/report.md`;
  const second = `${workspace}/b/report.md`;
  const resolver = createFilePreviewReferenceResolver([{ path: first }, { path: second }], workspace);
  assert.equal(resolver.resolve("report.md"), null);
  assert.equal(resolver.resolve("wrong/report.md"), null);
  assert.equal(resolver.resolve("a/report.md")?.path, first);
  assert.equal(resolver.resolve(second)?.path, second);
});

test("relative and absolute evidence for one persisted file do not create false ambiguity", () => {
  const resolver = createFilePreviewReferenceResolver([{ path: "交付材料/年度报告 v2.md" }, { path }], workspace);
  assert.equal(resolver.resolve("年度报告 v2.md")?.path, "交付材料/年度报告 v2.md");
  assert.equal(resolver.resolve(path)?.path, "交付材料/年度报告 v2.md");
});

test("recorded unknown file types still resolve for the preview panel's download fallback", () => {
  const zip = `${workspace}/bundle.zip`;
  assert.equal(createFilePreviewReferenceResolver([{ path: zip }], workspace).resolve("bundle.zip")?.path, zip);
});

test("web URLs and anchors retain navigation semantics; local file links are inert unless resolved", () => {
  for (const value of ["https://example.test/report.md", "mailto:team@example.test", "tel:123", "//example.test/report.md", "#section", "/cloud", "/c/session-a", "/", "docs/about"]) assert.equal(isLocalFileReference(value), false, value);
  for (const value of ["/tenant/report.md", "report.md", "./report.md", "sandbox:/mnt/data/report.md", "file:///tmp/report.md", "C:\\files\\report.md"]) assert.equal(isLocalFileReference(value), true, value);
  assert.equal(isLocalFileReference("/etc/passwd"), true);
  assert.equal(isLocalFileReference("/custom/project/README", "/custom/project"), true);
});

function project(markdown: string, paths = [path]) {
  const resolver = createFilePreviewReferenceResolver(paths.map((path) => ({ path })), workspace);
  const processor = unified().use(remarkParse).use(remarkGfm).use(resolver.remarkFilePreviews);
  return processor.runSync(processor.parse(markdown));
}

function links(tree: ReturnType<typeof project>): Array<{ url: string; text: string }> {
  const result: Array<{ url: string; text: string }> = [];
  function visit(node: { type: string; url?: string; value?: string; children?: typeof node[] }) {
    if (node.type === "link") result.push({ url: node.url!, text: node.children?.map((item) => item.value || "").join("") || "" });
    for (const child of node.children || []) visit(child);
  }
  visit(tree);
  return result;
}

test("prose paths become filename-only references including Chinese spaces and sandbox prefixes", () => {
  const tree = project(`已生成：${path}。另见 sandbox:${path}，以及 年度报告 v2.md。`);
  assert.deepEqual(links(tree), Array.from({ length: 3 }, () => ({ url: path, text: "年度报告 v2.md" })));
});

test("prose matching respects filename and URL boundaries, including sentence periods", () => {
  const report = `${workspace}/report.md`;
  assert.equal(links(project(`File: report.md. Then report.md, done.`, [report])).length, 2);
  assert.equal(links(project(`report.md.bak other/report.md xreport.md https://example.test/report.md`, [report])).length, 1);
  // The single link is the original external URL, not a file preview reference.
  assert.equal(links(project(`report.md.bak other/report.md xreport.md https://example.test/report.md`, [report]))[0].url, "https://example.test/report.md");
});

test("fenced code, inline code, external link labels, image alt text and citations are not rewritten", () => {
  const tree = project(`\`年度报告 v2.md\`\n\n\`\`\`text\n${path}\n\`\`\`\n\n[年度报告 v2.md](https://example.test/reference)\n\n![年度报告 v2.md](https://example.test/image.png)`);
  assert.deepEqual(links(tree), [{ url: "https://example.test/reference", text: "年度报告 v2.md" }]);
});

test("unrecorded prose paths and ambiguous bare filenames remain unchanged", () => {
  assert.deepEqual(links(project("/tenant/unknown.md report.md", [`${workspace}/a/report.md`, `${workspace}/b/report.md`])), []);
});

test("a malicious or nonexistent path in prose cannot create a preview URL", () => {
  assert.deepEqual(links(project("sandbox:/etc/passwd /tenant/workspaces/project-b/secret.md javascript:alert(1)")), []);
});
