import { expect, test, type Page } from "@playwright/test";
import { mock苏小有Api, seed苏小有Storage } from "./fixtures/suxiaoyou-api";

// Real browser regressions for the production UI and SSE lifecycle. API bodies
// are deterministic; operator acceptance separately runs configured DeepSeek.
const delivery = { name: "requested_helper.py", path: "/workspace/requested_helper.py", type: "deliverable", tool: "present_file" };
const working = { name: "draft-notes.md", path: "/workspace/draft-notes.md", type: "working", tool: "bash" };

async function openFiles(page: Page) {
  const showWorkspace = page.getByRole("button", { name: "Show workspace", exact: true });
  if (await showWorkspace.isVisible()) await showWorkspace.click();
  const files = page.locator("aside").filter({ has: page.getByRole("button", { name: "File options", exact: true }) });
  await expect(files).toBeVisible();
  await files.getByRole("button", { name: /^Files(?:\s|$)/ }).click();
  return files;
}

test("defaults to deliveries, exposes process files only in More, and resets on session change", async ({ page }) => {
  await seed苏小有Storage(page, { language: "en" });
  await mock苏小有Api(page);
  const requests: Array<{ session: string; scope: string | null }> = [];
  await page.route("**/api/sessions/*/todos", (route) => route.fulfill({ json: { todos: [] } }));
  await page.route("**/api/sessions/*/files**", (route) => {
    const url = new URL(route.request().url());
    const session = url.pathname.split("/").at(-2)!;
    const scope = url.searchParams.get("scope");
    requests.push({ session, scope });
    const files = session === "session-beta" ? (scope === "all" ? [delivery, working] : [delivery]) : [];
    return route.fulfill({ json: { files } });
  });
  await page.goto("/c/session-beta");
  const files = await openFiles(page);
  await expect(files.getByRole("button", { name: delivery.name, exact: true })).toBeVisible();
  await expect(files.getByRole("button", { name: working.name, exact: true })).toHaveCount(0);
  expect(requests[0]).toEqual({ session: "session-beta", scope: "deliverables" });
  await files.getByRole("button", { name: "File options", exact: true }).click();
  await page.getByRole("menuitem", { name: "Include working files" }).click();
  await expect(files.getByRole("button", { name: working.name, exact: true })).toBeVisible();
  expect(requests.at(-1)?.scope).toBe("all");
  await files.getByRole("button", { name: "File options", exact: true }).click();
  await page.getByRole("menuitem", { name: "Include working files" }).click();
  await expect(files.getByRole("button", { name: working.name, exact: true })).toHaveCount(0);
  expect(requests.at(-1)?.scope).toBe("deliverables");
  // Change sessions while all is enabled; a new session must never inherit it.
  await files.getByRole("button", { name: "File options", exact: true }).click();
  await page.getByRole("menuitem", { name: "Include working files" }).click();
  await expect(files.getByRole("button", { name: working.name, exact: true })).toBeVisible();
  const sidebarToggle = page.getByTestId("sidebar-collapse-toggle");
  if (await sidebarToggle.getAttribute("aria-expanded") === "false") await sidebarToggle.click();
  await page.getByText("Quarterly planning notes", { exact: true }).click();
  await expect.poll(() => requests.at(-1)).toEqual({ session: "session-alpha", scope: "deliverables" });
  await expect(page.getByRole("button", { name: working.name, exact: true })).toHaveCount(0);
});

test("keeps the last presentation when its files response arrives after DONE", async ({ page }) => {
  await seed苏小有Storage(page, { language: "en" });
  await mock苏小有Api(page);
  let active = true;
  let releaseStream!: () => void;
  const streamGate = new Promise<void>((resolve) => { releaseStream = resolve; });
  let releaseFiles!: () => void;
  const filesGate = new Promise<void>((resolve) => { releaseFiles = resolve; });
  let reads = 0;
  let delayedReads = 0;
  let streamRequested = false;
  let deliveryPublished = false;
  await page.route("**/api/sessions/session-beta/todos", (route) => route.fulfill({ json: { todos: [] } }));
  await page.route("**/api/chat/active**", (route) => route.fulfill({ json: active ? [{ session_id: "session-beta", stream_id: "stream-delivery-done" }] : [] }));
  await page.route("**/api/sessions/session-beta/files**", async (route) => {
    reads += 1;
    // Development StrictMode can replay session hydration. None of those
    // initial reads is a delivery refresh; gate by the actual SSE stage.
    if (!deliveryPublished) return route.fulfill({ json: { files: [] } });
    delayedReads += 1;
    await filesGate;
    return route.fulfill({ json: { files: [delivery] } });
  });
  await page.route("**/api/chat/stream/stream-delivery-done**", async (route) => {
    streamRequested = true;
    await streamGate;
    active = false;
    deliveryPublished = true;
    const event = (id: number, name: string, data: Record<string, unknown>) => `id: ${id}\nevent: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: [
        event(1, "tool-call", { tool: "present_file", call_id: "last-delivery", arguments: { file_path: delivery.path } }),
        event(2, "tool-result", { tool: "present_file", call_id: "last-delivery", output: "Presented", metadata: { file_path: delivery.path } }),
        event(3, "done", { session_id: "session-beta", finish_reason: "stop" }),
      ].join(""),
    });
  });
  await page.goto("/c/session-beta");
  const files = await openFiles(page);
  await expect.poll(() => streamRequested).toBe(true);
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toBeVisible();
  const hydrationReads = reads;
  expect(hydrationReads).toBeGreaterThanOrEqual(1);
  releaseStream();
  await expect.poll(() => delayedReads).toBeGreaterThanOrEqual(1);
  expect(reads).toBeGreaterThan(hydrationReads);
  // The response stays blocked until the real DONE handler releases the stream.
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Send message", exact: true })).toBeVisible();
  releaseFiles();
  await expect(files.getByRole("button", { name: delivery.name, exact: true })).toBeVisible();
});
