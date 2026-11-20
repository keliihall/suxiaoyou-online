import { expect, test, type Page } from "@playwright/test";
import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

const workspace = "/Users/alex/suxiaoyou-demo";
const outputPath = `${workspace}/交付材料/年度报告 v2.md`;
const sessionId = "session-artifacts";

async function installPathOutput(page: Page, text: string, paths = [outputPath]) {
  await seed苏小有Storage(page, { force: true });
  const state = await mock苏小有Api(page);
  // The legacy task never presented its writes. A text reference may preview
  // an evidenced file without silently promoting every process write.
  await page.route("**/api/sessions/session-artifacts/files?**", route => route.fulfill({ json: { files: [] } }));
  const part = (messageId: string, id: string, data: Record<string, unknown>) => ({ id, message_id: messageId, session_id: sessionId, time_created: "2026-11-17T10:00:00.000Z", data });
  await page.route(/\/api\/messages\/session-artifacts(?:\?.*)?$/, route => route.fulfill({ json: {
    total: 2,
    offset: 0,
    messages: [
      {
        id: "path-output-tools", session_id: sessionId, time_created: "2026-11-17T10:00:00.000Z",
        data: { role: "assistant", agent: "build", finish: "tool-calls", model_id: "deepseek-office", provider_id: "custom_cloud", cost: 0 },
        parts: paths.map((path, index) => part("path-output-tools", `write-${index}`, {
          type: "tool", tool: "write", call_id: `write-${index}`,
          state: { status: "completed", input: { file_path: path }, output: "Written", metadata: { file_path: path, written_files: [path] }, time_start: null, time_end: null },
        })),
      },
      {
        id: "path-output-final", session_id: sessionId, time_created: "2026-11-17T10:00:01.000Z",
        data: { role: "assistant", agent: "build", finish: "stop", model_id: "deepseek-office", provider_id: "custom_cloud", cost: 0 },
        parts: [part("path-output-final", "path-output-text", { type: "text", text })],
      },
    ],
  } }));
  const reads: Array<{ path: string; workspace: string }> = [];
  await page.route("**/api/files/content", route => {
    reads.push(route.request().postDataJSON());
    return route.fulfill({ json: { content: "# 可直接预览的报告\n\n真实页面已读取所选文件内容。", name: "年度报告 v2.md" } });
  });
  return { state, reads };
}

test("legacy path-only results preview in the page for prose, relative, inline and sandbox links", async ({ page }) => {
  const { state, reads } = await installPathOutput(page, [
    `绝对路径：${outputPath}。`,
    "相对路径：交付材料/年度报告 v2.md。",
    "文件名：`年度报告 v2.md`。",
    `[点击查看](<sandbox:${outputPath}>)`,
  ].join("\n\n"));
  await page.goto(`/c/${sessionId}`);
  const links = page.getByTestId("file-preview-link");
  await expect(links).toHaveCount(4);
  await expect(page.getByTestId("file-artifact-card")).toHaveCount(0);
  await expect(page.getByText(outputPath, { exact: true })).toHaveCount(0);
  for (let index = 0; index < 4; index += 1) {
    await links.nth(index).click();
    await expect(page.getByRole("heading", { name: "可直接预览的报告" })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/c/${sessionId}$`));
    expect(page.context().pages()).toHaveLength(1);
    await page.getByRole("button", { name: "Close preview", exact: true }).click();
    await expect(page.getByRole("heading", { name: "可直接预览的报告" })).toBeHidden();
    await expect(page.getByRole("dialog", { name: "Artifact Preview", exact: true })).toBeHidden();
  }
  // Reopening the same mounted preview can reuse loaded content, while dev
  // StrictMode can replay a fetch. Assert the read's identity, not its count.
  expect(reads.length).toBeGreaterThan(0);
  expect(reads.every(read => read.path === outputPath && read.workspace === workspace)).toBe(true);
  expect(state.systemOpenRequests).toEqual([]);
  expect(page.context().pages()).toHaveLength(1);
});

test("same-name files, unknown server paths, external links and fenced code retain safe behavior", async ({ page }) => {
  const paths = [`${workspace}/a/report.md`, `${workspace}/b/report.md`];
  const { state, reads } = await installPathOutput(page, [
    "歧义文件：`report.md`。",
    `可定位文件：${paths[0]}。`,
    "[未知本机文件](/etc/passwd)",
    "[未知沙盒文件](sandbox:/mnt/data/report.md)",
    "[`report.md`](https://example.test/reference)",
    "[工作台](/cloud)",
    `\`\`\`text\n${paths[0]}\n\`\`\``,
    "`/tenant/unknown.zip`",
  ].join("\n\n"), paths);
  await page.goto(`/c/${sessionId}`);
  await expect(page.getByTestId("file-preview-link")).toHaveCount(1);
  await expect(page.getByRole("link", { name: "未知本机文件" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "未知沙盒文件" })).toHaveCount(0);
  const external = page.getByRole("link", { name: "report.md", exact: true });
  await expect(external).toHaveAttribute("href", "https://example.test/reference");
  await expect(external.locator("button")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "工作台", exact: true })).toHaveAttribute("href", "/cloud");
  await expect(page.locator("pre").getByText(paths[0], { exact: true })).toBeVisible();
  await expect(page.locator("pre").getByTestId("file-preview-link")).toHaveCount(0);
  expect(reads).toEqual([]);
  expect(state.systemOpenRequests).toEqual([]);
});

test("recorded Markdown image output opens a local preview while remote images stay images", async ({ page }) => {
  const imagePath = `${workspace}/交付材料/图像 结果.png`;
  await installPathOutput(page, `![生成图像](<sandbox:${imagePath}>)\n\n![外部图像](https://example.test/photo.png)`, [imagePath]);
  const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII=";
  await page.route("https://example.test/photo.png", route => route.fulfill({ contentType: "image/png", body: Buffer.from(png, "base64") }));
  const binaryReads: string[] = [];
  await page.route("**/api/files/content-binary", route => {
    const body = route.request().postDataJSON();
    binaryReads.push(body.path);
    return route.fulfill({ json: { content_base64: png, name: "图像 结果.png", mime_type: "image/png", size: Buffer.from(png, "base64").length } });
  });
  await page.goto(`/c/${sessionId}`);
  await expect(page.getByTestId("file-preview-link")).toHaveCount(1);
  await expect(page.getByRole("img", { name: "外部图像", exact: true })).toHaveAttribute("src", "https://example.test/photo.png");
  await page.getByTestId("file-preview-link").click();
  await expect(page.getByTestId("raster-image-preview")).toBeVisible();
  await expect(page.getByTestId("raster-image-preview")).toHaveAttribute("src", /^blob:/);
  expect(binaryReads.length).toBeGreaterThan(0);
  expect(binaryReads.every(path => path === imagePath)).toBe(true);
  await expect(page).toHaveURL(new RegExp(`/c/${sessionId}$`));
  expect(page.context().pages()).toHaveLength(1);
});
