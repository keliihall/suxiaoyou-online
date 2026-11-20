import { expect, test } from "@playwright/test";
import { fullWorkbenchFixture } from "./fixtures/cloud-workbench";
import { completeCaptcha } from "./fixtures/cloud-captcha";

test.skip(process.env.NEXT_PUBLIC_CLOUD_MODE !== "true", "Cloud runtime authentication uses an explicit cloud build.");

test("cloud login boots the original full workbench and preserves the original authenticated Agent API", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { signedIn: false });
  await page.goto("/c/new");
  await expect(page.getByRole("main", { name: "登录", exact: true })).toBeVisible();
  await page.getByLabel("邮箱", { exact: true }).fill("a@example.test");
  await page.getByLabel("密码", { exact: true }).fill("not-a-real-password-123");
  await page.getByRole("button", { name: "登录工作台" }).click();
  await completeCaptcha(page);
  await expect(page).toHaveURL(/\/c\/new$/);
  await expect(page.getByPlaceholder(/Describe the result you want/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /DeepSeek/i })).toBeVisible();
  await page.getByTestId("sidebar-account-more").click();
  await expect(page.getByRole("menuitem", { name: "云端管理" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByPlaceholder(/Describe the result you want/i).fill("Validate the original Agent flow from the cloud workbench");
  await page.locator('button[aria-label="Send message"]:not([disabled])').click();
  await expect.poll(() => fixture.engine.promptBodies.length).toBe(1);
  expect(fixture.engine.promptBodies[0]).toMatchObject({ model: "deepseek-office", provider_id: "custom_cloud", workspace: "/tenant/workspaces/project-a" });
  expect(fixture.calls.find(call => call.path === "/api/cloud/runtime/start")?.csrf).toBe("memory-only-csrf");
  expect(fixture.calls.find(call => call.path === "/api/chat/prompt")?.csrf).toBe("memory-only-csrf");
  expect(fixture.calls.filter(call => call.path.startsWith("/api/")).every(call => !call.authorization)).toBe(true);
  expect(await page.evaluate(() => Object.values(localStorage).some(value => value.includes("memory-only-csrf")))).toBe(false);
});

test("a failed private runtime never mounts business queries or the desktop workbench", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { unavailable: true });
  await page.goto("/c/new");
  await expect(page.getByRole("alert").filter({ hasText: "运行环境尚未启动" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重新连接" })).toBeVisible();
  await expect(page.getByPlaceholder(/Describe the result you want/i)).toHaveCount(0);
  expect(fixture.calls.some(call => call.path === "/api/sessions" || call.path === "/api/chat/active")).toBe(false);
});

test("an expired cookie removes the full workbench before returning to login", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.goto("/c/new");
  await expect(page.getByPlaceholder(/Describe the result you want/i)).toBeVisible();
  fixture.expire();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(page.getByRole("main", { name: "登录", exact: true })).toBeVisible();
  await expect(page.getByPlaceholder(/Describe the result you want/i)).toHaveCount(0);
  expect(fixture.calls.filter(call => call.path === "/api/cloud/runtime/start")).toHaveLength(0);
});

test("mobile cloud tasks use the cloud cookie without desktop pairing or a stored tunnel token", async ({ page }) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/m/new");
  await expect(page).toHaveURL(/\/m\/new$/);
  await expect(page.locator("textarea")).toBeVisible();
  await expect(page.getByRole("combobox")).toBeVisible();
  await page.goto("/m/settings");
  await expect(page.getByRole("heading", { name: "账户设置" })).toBeVisible();
  await expect(page.getByRole("link", { name: "完整设置" })).toBeVisible();
  await expect(page.getByPlaceholder("https://your-tunnel-url.trycloudflare.com")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("cloud access controls only the private runtime and never enables a desktop tunnel", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/remote");
  await expect(page.getByRole("heading", { name: "云端访问" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "工作台地址", exact: true })).toHaveValue(/\/c\/new$/);
  await expect(page.getByRole("status").filter({ hasText: "运行中" })).toBeVisible();
  await page.getByRole("button", { name: "停止", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "已停止" })).toBeVisible();
  await page.getByRole("button", { name: "启动", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "运行中" })).toBeVisible();
  expect(fixture.calls.filter(call => call.path === "/api/remote/enable")).toHaveLength(0);
  expect(fixture.calls.find(call => call.path === "/api/cloud/runtime/stop")?.csrf).toBe("memory-only-csrf");
});

test("cloud workspace memory export downloads the actual authenticated file", async ({ page }) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  const path = "/tenant/workspaces/project-a/.suxiaoyou/memory.md";
  const content = "# Workspace memory\nOnly this user's project details.\n";
  await page.route("**/api/workspace-memory/export?**", route => route.fulfill({ json: { exported_to: path } }));
  await page.route("**/api/files/content-binary", route => {
    expect(route.request().postDataJSON()).toMatchObject({ path, workspace: "/tenant/workspaces/project-a" });
    expect(route.request().headers()["x-csrf-token"]).toBe("memory-only-csrf");
    return route.fulfill({ json: { name: "memory.md", mime_type: "text/markdown", content_base64: Buffer.from(content).toString("base64") } });
  });
  await page.route("**/api/workspace-memory/list", route => route.fulfill({ json: [{ workspace_path: "/tenant/workspaces/project-a", content, line_count: 2 }] }));
  await page.goto("/settings");
  await page.getByRole("button", { name: "Memory", exact: true }).click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("memory.md");
});

test("320px mobile workspace selection keeps touch targets and modal keyboard focus accessible", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.route("**/api/cloud-runtime/files/directories", route => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ path: "/tenant/workspaces/project-a" });
    return route.fulfill({ json: {
      path: "/tenant/workspaces/project-a",
      parent: "/tenant/workspaces",
      dirs: [{ name: "reports", path: "/tenant/workspaces/project-a/reports" }],
    } });
  });
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/m/new");
  const workspaceTrigger = page.getByRole("button", { name: "Select workspace: /tenant/workspaces/project-a", exact: true });
  const clearWorkspace = page.getByRole("button", { name: "Clear", exact: true });
  await expect(workspaceTrigger).toBeVisible();
  await expect(clearWorkspace).toBeVisible();
  await expect(page.locator("button button")).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  for (const control of [workspaceTrigger, clearWorkspace]) {
    const bounds = await control.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.width).toBeGreaterThanOrEqual(44);
    expect(bounds!.height).toBeGreaterThanOrEqual(44);
  }

  await workspaceTrigger.click();
  const directoryDialog = page.getByRole("dialog", { name: "Select workspace", exact: true });
  const lastDirectory = directoryDialog.getByRole("button", { name: "reports", exact: true });
  const closeDirectory = directoryDialog.getByRole("button", { name: "Back", exact: true });
  await expect(directoryDialog).toBeVisible();
  await expect(lastDirectory).toBeVisible();
  await expect.poll(() => directoryDialog.evaluate(dialog => dialog.contains(document.activeElement))).toBe(true);
  await lastDirectory.focus();
  await page.keyboard.press("Tab");
  await expect(closeDirectory).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(lastDirectory).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(directoryDialog).toBeHidden();
  await expect(workspaceTrigger).toBeFocused();
  expect(fixture.engine.promptBodies).toHaveLength(0);
  expect(fixture.calls.some(call => call.path.endsWith("/directories/create"))).toBe(false);
});

test("390px workbench keeps suggestions in More, reachable navigation and distinct dark surfaces", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/c/new");
  await expect(page.getByPlaceholder(/Describe the result you want/i)).toBeVisible();
  const recommendations = page.locator('[data-testid^="starter-"]');
  await expect(recommendations).toHaveCount(0);
  await page.getByTestId("composer-more").click();
  await expect(recommendations).toHaveCount(3);
  for (const recommendation of await recommendations.all()) await expect(recommendation).toBeVisible();
  await page.keyboard.press("Escape");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);

  const navigationTrigger = page.getByRole("button", { name: "Toggle sidebar", exact: true });
  await expect(navigationTrigger).toBeInViewport();
  await navigationTrigger.click();
  const navigation = page.getByRole("dialog", { name: "Navigation", exact: true });
  const pluginsLink = navigation.getByRole("link", { name: "Plugins", exact: true });
  await expect(pluginsLink).toBeInViewport();
  await pluginsLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/plugins$/);
  await expect(navigation).toBeHidden();
  await page.goBack();
  await expect(page).toHaveURL(/\/c\/new$/);
  await expect(page.getByTestId("composer-more")).toBeVisible();
  await expect(recommendations).toHaveCount(0);

  await page.evaluate(() => {
    document.documentElement.classList.remove("dark");
    document.documentElement.classList.add("light");
  });
  const canvas = page.locator("#main-content");
  const lightCanvas = await canvas.evaluate(element => getComputedStyle(element).backgroundColor);
  const composer = page.locator(".composer-shell");
  const lightComposer = await composer.evaluate(element => getComputedStyle(element).backgroundColor);
  await page.evaluate(() => {
    document.documentElement.classList.remove("light");
    document.documentElement.classList.add("dark");
  });
  await expect.poll(() => canvas.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe(lightCanvas);
  await expect.poll(() => composer.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe(lightComposer);
  expect(await canvas.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe("rgba(0, 0, 0, 0)");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(fixture.engine.promptBodies).toHaveLength(0);
});

test("320px mobile history opens file, activity and plan panels without submitting a task", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/m/task/_?sessionId=session-artifacts");
  const previewTrigger = page.getByRole("button", { name: "Open preview Release Brief", exact: true });
  await expect(previewTrigger).toBeVisible();
  await expect(page.locator("button button, button [role=button]")).toHaveCount(0);
  await previewTrigger.click();
  const artifactPanel = page.getByRole("dialog", { name: "Artifact Preview", exact: true });
  await expect(artifactPanel).toBeVisible();
  await expect(artifactPanel.getByText("Validate mobile handoff", { exact: true })).toBeVisible();
  await expect(artifactPanel.getByRole("button", { name: /^Close/ })).toHaveCount(1);
  expect(await artifactPanel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await artifactPanel.getByRole("button", { name: "Close preview", exact: true }).click();
  await expect(artifactPanel).toBeHidden();
  await expect(previewTrigger).toBeFocused();

  const activityTrigger = page.getByRole("button", { name: "View activity", exact: true });
  await activityTrigger.click();
  const activityPanel = page.getByRole("dialog", { name: "Activity", exact: true });
  await expect(activityPanel).toBeVisible();
  await expect(activityPanel.getByRole("button", { name: /^Close/ })).toHaveCount(1);
  expect(await activityPanel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.keyboard.press("Escape");
  await expect(activityPanel).toBeHidden();
  await expect(activityTrigger).toBeFocused();

  const planTrigger = page.getByRole("button", { name: /GUI Preflight Plan/ });
  await planTrigger.click();
  const planPanel = page.getByRole("dialog", { name: "Plan Review", exact: true });
  await expect(planPanel).toBeVisible();
  await expect(planPanel.getByText("Run mobile handoff workflows.", { exact: true })).toBeVisible();
  await expect(planPanel.getByText("frontend/tests/ui/suxiaoyou-preflight.spec.ts", { exact: true })).toBeVisible();
  expect(await planPanel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await planPanel.getByRole("button", { name: "Close panel", exact: true }).click();
  await expect(planPanel).toBeHidden();
  await expect(planTrigger).toBeFocused();
  expect(fixture.engine.promptBodies).toHaveLength(0);
  expect(fixture.calls.some(call => call.path === "/api/chat/respond")).toBe(false);
});

test("mobile history previews existing artifact content while running or after an error", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  const drafts = [
    { status: "running", title: "Running draft", content: "Available while generation continues." },
    { status: "error", title: "Failed draft", content: "Preserved after the failed update." },
  ];
  await page.route(/\/api\/messages\/session-artifacts(?:\?|$)/, route => {
    expect(route.request().method()).toBe("GET");
    return route.fulfill({ json: {
      total: 1,
      offset: 0,
      messages: [{
        id: "artifact-state-message",
        session_id: "session-artifacts",
        time_created: "2026-04-23T11:02:00.000Z",
        data: { role: "assistant", agent: "build", model_id: "deepseek-office", provider_id: "custom_cloud", finish: "stop", cost: 0 },
        parts: drafts.map(draft => ({
          id: `artifact-${draft.status}`,
          message_id: "artifact-state-message",
          session_id: "session-artifacts",
          time_created: "2026-04-23T11:02:00.000Z",
          data: {
            type: "tool",
            tool: "artifact",
            call_id: `artifact-${draft.status}`,
            state: {
              status: draft.status,
              input: { command: "create", type: "markdown", title: draft.title, ...(draft.status === "running" ? { content: draft.content } : {}) },
              metadata: draft.status === "error" ? { content: draft.content } : null,
              output: null,
              title: draft.title,
              time_start: "2026-04-23T11:02:00.000Z",
              time_end: draft.status === "error" ? "2026-04-23T11:03:00.000Z" : null,
              time_compacted: null,
            },
          },
        })),
      }],
    } });
  });
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/m/task/_?sessionId=session-artifacts");
  const preview = page.getByRole("dialog", { name: "Artifact Preview", exact: true });
  for (const draft of drafts) {
    const trigger = page.getByRole("button", { name: `Open preview ${draft.title}`, exact: true });
    await expect(trigger).toBeEnabled();
    await trigger.click();
    await expect(preview).toBeVisible();
    await expect(preview.getByText(draft.content, { exact: true })).toBeVisible();
    await preview.getByRole("button", { name: "Close preview", exact: true }).click();
    await expect(preview).toBeHidden();
    await expect(trigger).toBeFocused();
  }
  expect(fixture.engine.promptBodies).toHaveLength(0);
  expect(fixture.calls.some(call => call.path === "/api/chat/respond")).toBe(false);
});

test("mobile plan preview clears when the session changes or the task is reopened", async ({ page }) => {
  const fixture = await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/m");
  await page.getByRole("button", { name: /Artifact showcase/ }).click();
  const planTrigger = page.getByRole("button", { name: /GUI Preflight Plan/ });
  const planPanel = page.getByRole("dialog", { name: "Plan Review", exact: true });
  await planTrigger.click();
  await expect(planPanel).toBeVisible();

  // Next's native History integration keeps this route mounted while sessionId changes.
  await page.evaluate(() => window.history.pushState(null, "", "/m/task/_?sessionId=session-beta"));
  await expect(page.locator("header").getByText("Invoice cleanup", { exact: true })).toBeVisible();
  await expect(planPanel).toBeHidden();
  await page.goBack();
  await expect(page).toHaveURL(/\/m\/task\/_\?sessionId=session-artifacts$/);
  await expect(planPanel).toBeHidden();
  await planTrigger.click();
  await expect(planPanel).toBeVisible();

  // Leaving with a panel open must not restore its old data in the next task.
  await page.goBack();
  await expect(page).toHaveURL(/\/m$/);
  await page.getByRole("button", { name: /Invoice cleanup/ }).click();
  await expect(page.locator("header").getByText("Invoice cleanup", { exact: true })).toBeVisible();
  await expect(planPanel).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(fixture.engine.promptBodies).toHaveLength(0);
  expect(fixture.calls.some(call => call.path === "/api/chat/respond")).toBe(false);
});
