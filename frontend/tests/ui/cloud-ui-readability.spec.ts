import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { cloudFixture, login } from "./fixtures/cloud-account";
import { fullWorkbenchFixture } from "./fixtures/cloud-workbench";
import { auditVisibleContrast } from "./helpers/visible-contrast.mjs";

test.skip(process.env.NEXT_PUBLIC_CLOUD_MODE !== "true", "User pages use the authenticated cloud build.");
test.setTimeout(240_000);

async function theme(page: Page, mode: "light" | "dark") {
  await page.addStyleTag({ content: "*,*::before,*::after{transition:none!important;animation:none!important}" });
  await page.evaluate(mode => {
    document.documentElement.classList.remove("light", "dark");
    document.documentElement.classList.add(mode);
    document.documentElement.style.colorScheme = mode;
  }, mode);
}

async function readable(page: Page, info: TestInfo, label: string) {
  if (new URL(page.url()).pathname === "/c/new") {
    await expect(page.getByTestId("composer-more")).toBeVisible();
  }
  const audit = await page.evaluate(auditVisibleContrast, {});
  await info.attach(label, { body: JSON.stringify(audit, null, 2), contentType: "application/json" });
  expect(audit.checked, label).toBeGreaterThan(0);
  expect.soft(audit.failures, `${label}: rendered text on its actual background`).toEqual([]);
  expect.soft(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${label}: viewport overflow`).toBe(true);
}

test("login fields and primary action remain readable in both themes", async ({ page }, info) => {
  await cloudFixture(page);
  await page.goto("/cloud?manage=1");
  await expect(page.getByRole("main", { name: "登录", exact: true })).toBeVisible();
  for (const mode of ["light", "dark"] as const) {
    await theme(page, mode);
    await readable(page, info, `login-${mode}`);
    await page.getByRole("button", { name: "登录工作台" }).hover();
    await readable(page, info, `login-hover-${mode}`);
  }
});

test("all account tabs and configuration dialogs have readable normal and disabled states", async ({ page }, info) => {
  await cloudFixture(page, true);
  await login(page);
  for (const mode of ["light", "dark"] as const) {
    await theme(page, mode);
    for (const tab of ["用户与额度", "用量与核查", "全部任务", "模型路由", "操作审计"]) {
      await page.getByRole("tab", { name: tab, exact: true }).click();
      await readable(page, info, `admin-${tab}-${mode}`);
    }
    await page.getByRole("tab", { name: "用户与额度", exact: true }).click();
    await page.getByRole("button", { name: "创建用户", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await readable(page, info, `create-user-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("tab", { name: "模型路由", exact: true }).click();
    await page.getByRole("button", { name: "添加模型", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await readable(page, info, `create-model-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("row").filter({ hasText: "DeepSeek 已配置" }).getByRole("button", { name: "编辑", exact: true }).click();
    await readable(page, info, `edit-disabled-model-id-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "用量与额度", exact: true }).click();
    await readable(page, info, `account-usage-${mode}`);
    await page.getByRole("button", { name: "个人模型", exact: true }).click();
    await page.getByRole("button", { name: "添加个人模型", exact: true }).click();
    await readable(page, info, `personal-model-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "管理后台", exact: true }).click();
  }
});

test("all workbench and settings pages retain contrast in both themes", async ({ page }, info) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  const paths = ["/c/new", "/plugins", "/automations", "/remote", ...["general", "providers", "permissions", "security", "automations", "plugins", "usage", "memory"].map(tab => `/settings?tab=${tab}`)];
  for (const path of paths) {
    await page.goto(path);
    await expect(page.locator("#main-content")).toBeVisible();
    await expect(page.locator("#main-content h1")).toBeVisible();
    for (const mode of ["light", "dark"] as const) {
      await theme(page, mode);
      await readable(page, info, `workbench-${path.replaceAll(/[^a-z]/g, "-")}-${mode}`);
    }
  }
});

test("320px user pages keep readable text and contained layouts in both themes", async ({ page }, info) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.setViewportSize({ width: 320, height: 568 });
  for (const path of ["/c/new", "/m", "/m/new", "/m/settings", "/plugins", "/automations", "/settings?tab=providers"]) {
    await page.goto(path);
    await expect(page.locator("body")).not.toContainText("正在验证云端账户");
    await expect(page.locator("h1, textarea").first()).toBeVisible();
    for (const mode of ["light", "dark"] as const) {
      await theme(page, mode);
      await readable(page, info, `mobile-${path.replaceAll(/[^a-z]/g, "-")}-${mode}`);
    }
  }
});

test("historical files, activity and plan panels stay readable on desktop and mobile", async ({ page }, info) => {
  await fullWorkbenchFixture(page, { runtimeReady: true });
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: width === 320 ? 568 : 960 });
    await page.goto(width === 320 ? "/m/task/_?sessionId=session-artifacts" : "/c/_?sessionId=session-artifacts");
    const previewTrigger = page.getByRole("button", { name: "Open preview Release Brief", exact: true });
    await expect(previewTrigger).toBeVisible();
    for (const mode of ["light", "dark"] as const) {
      await theme(page, mode);
      await readable(page, info, `history-${width}-${mode}`);
      await previewTrigger.click();
      const closePreview = page.getByRole("button", { name: "Close preview", exact: true });
      await expect(closePreview).toBeInViewport();
      await readable(page, info, `preview-${width}-${mode}`);
      await closePreview.click();
      await page.getByRole("button", { name: "View activity", exact: true }).click();
      const closePanel = page.getByRole("button", { name: "Close panel", exact: true });
      await expect(closePanel).toBeInViewport();
      await readable(page, info, `activity-${width}-${mode}`);
      await closePanel.click();
      await page.getByRole("button", { name: /GUI Preflight Plan/ }).click();
      await expect(closePanel).toBeInViewport();
      await readable(page, info, `plan-${width}-${mode}`);
      await closePanel.click();
    }
  }
});

test("destructive confirmations, uncertain usage and image-test forms stay readable without executing them", async ({ page }, info) => {
  const fixture = await cloudFixture(page, true);
  const at = "2026-11-15T08:00:00Z";
  const image = { id: "user.user-1.image", name: "个人图像路由", provider: "siliconflow", model: "image-model", kind: "image", enabled: true, strict_quota: false, key_configured: true, authentication_configured: true, credential_source: "managed" };
  await page.route("**/api/cloud/user/models", route => route.fulfill({ json: [image] }));
  await page.route("**/api/cloud/admin/jobs", route => route.fulfill({ json: [{ id: "review-job", user_id: "user-1", conversation_id: "conversation-1", state: "running", model_id: "office-model", prompt: "状态显示检查", answer: "", error: "", cancel_requested: false, created_at: at, updated_at: at }] }));
  await page.route("**/api/cloud/admin/usage/requests", route => route.fulfill({ json: [{ id: "unknown-request", user_id: "user-1", model_id: "office-model", state: "unknown", reserved_tokens: 100, total_tokens: 0, cost_microusd: 0, error: "等待核查供应商用量", created_at: at }] }));
  await login(page);
  for (const mode of ["light", "dark"] as const) {
    await theme(page, mode);
    await page.getByRole("tab", { name: "全部任务", exact: true }).click();
    await page.getByRole("button", { name: "取消任务", exact: true }).click();
    await readable(page, info, `cancel-confirm-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("tab", { name: "用量与核查", exact: true }).click();
    await readable(page, info, `uncertain-usage-${mode}`);
    await page.getByRole("button", { name: "核查处理", exact: true }).click();
    await readable(page, info, `reconcile-form-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "个人模型", exact: true }).click();
    await page.getByRole("button", { name: "删除个人模型 个人图像路由", exact: true }).click();
    await readable(page, info, `delete-confirm-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "生成测试图", exact: true }).click();
    await readable(page, info, `image-test-form-${mode}`);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "管理后台", exact: true }).click();
  }
  expect(fixture.calls.some(call => /\/cancel$|\/reconcile$|\/images\/generations$/.test(call.path))).toBe(false);
  expect(fixture.calls.some(call => call.method === "DELETE")).toBe(false);
});
