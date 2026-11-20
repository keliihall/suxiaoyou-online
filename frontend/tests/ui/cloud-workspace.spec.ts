import { expect, test } from "@playwright/test";
import { cloudFixture, login } from "./fixtures/cloud-account";

test.skip(process.env.NEXT_PUBLIC_CLOUD_MODE !== "true", "Cloud UI is an explicit, separate build mode.");

test("cloud account preserves management, usage and logout without a second chat shell", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await login(page);
  await expect(page.getByRole("link", { name: "完整工作台" })).toHaveAttribute("href", "/c/new");
  await expect(page.getByRole("button", { name: "创建第一个工作区" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "消息内容" })).toHaveCount(0);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByRole("main", { name: "登录", exact: true })).toBeVisible();
  await expect(page.getByText("云端用户", { exact: true })).toHaveCount(0);
  expect(fixture.calls.every(call => call.path.startsWith("/api/cloud/"))).toBe(true);
  expect(fixture.calls.find(call => call.path.endsWith("/auth/logout"))?.csrf).toBe("test-csrf-token");
  expect(await page.evaluate(() => Object.values(localStorage).some(value => value.includes("test-csrf-token")))).toBe(false);
});

test("administrator can create users and configure routes using environment variable names", async ({ page }) => {
  await cloudFixture(page, true);
  await login(page);
  await page.getByRole("button", { name: "管理后台", exact: true }).click();
  await page.getByRole("button", { name: "创建用户", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("姓名", { exact: true }).fill("新同事");
  await dialog.getByLabel("邮箱", { exact: true }).fill("new@example.com");
  await dialog.getByLabel("初始密码", { exact: false }).fill("new-password-123");
  await dialog.getByRole("button", { name: "创建用户", exact: true }).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("tab", { name: "模型路由" }).click();
  await expect(page.getByRole("row").filter({ hasText: "DeepSeek 待配置" }).getByText("未配置", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "DeepSeek 已配置" }).getByText("已配置", { exact: true })).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "纯测试路由" })).toContainText("测试模型 · 无真实密钥");
  await expect(page.getByRole("row").filter({ hasText: "DeepSeek 待配置" }).getByRole("button", { name: "连接测试" })).toBeDisabled();
  await expect(page.getByRole("row").filter({ hasText: "纯测试路由" }).getByRole("button", { name: "连接测试" })).toBeDisabled();
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  await expect(page.getByRole("dialog").getByLabel("服务商标识", { exact: true })).toHaveValue("deepseek");
  await expect(page.getByRole("dialog").getByLabel("实际模型 ID", { exact: true })).toHaveValue("deepseek-flash");
  await expect(page.getByRole("dialog").getByLabel("兼容接口 Base URL（可选）", { exact: true })).toHaveValue("https://api.deepseek.com/v1");
  await expect(page.getByRole("dialog").getByRole("checkbox", { name: "向用户开放此模型" })).not.toBeChecked();
  await page.getByRole("dialog").getByText("高级配置（可选）", { exact: true }).click();
  await expect(page.getByRole("dialog").getByLabel("密钥环境变量名", { exact: false })).toHaveAttribute("pattern", "SUXIAOYOU_MODEL_[A-Z0-9_]+");
  await page.getByRole("dialog").getByLabel("路由 ID", { exact: false }).fill("office-route");
  await page.getByRole("dialog").getByLabel("展示名称", { exact: true }).fill("团队办公模型");
  await page.getByRole("dialog").getByLabel("实际模型 ID", { exact: true }).fill("example-model");
  await page.getByRole("dialog").getByLabel("密钥环境变量名", { exact: false }).fill("SUXIAOYOU_MODEL_TEAM_KEY");
  const saved = page.waitForRequest(request => request.url().endsWith("/admin/models") && request.method() === "POST");
  await page.getByRole("dialog").getByRole("button", { name: "保存模型路由" }).click();
  const payload = (await saved).postDataJSON();
  expect(payload.api_key_env).toBe("SUXIAOYOU_MODEL_TEAM_KEY");
  expect(payload).not.toHaveProperty("api_key");
  await expect(page.getByText("团队办公模型", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "用量与核查" }).click();
  await expect(page.getByRole("heading", { name: "请求账本" })).toBeVisible();
});

test("administrator replaces and clears a write-only API key without browser persistence", async ({ page }) => {
  await cloudFixture(page, true);
  await login(page);
  await page.getByRole("button", { name: "管理后台", exact: true }).click();
  await page.getByRole("tab", { name: "模型路由" }).click();
  const row = page.getByRole("row").filter({ hasText: "DeepSeek 已配置" });
  await row.getByRole("button", { name: "编辑", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const keyInput = dialog.getByLabel("API Key", { exact: true });
  await expect(keyInput).toHaveAttribute("type", "password");
  await expect(keyInput).toHaveAttribute("autocomplete", "new-password");
  await expect(keyInput).toHaveValue("");
  const testKey = "sk-ui-placeholder-never-a-live-key";
  await keyInput.fill(testKey);
  await page.route("**/api/cloud/admin/models/deepseek-ready", route => route.fulfill({ status: 503, json: { detail: "保存暂时不可用" } }));
  const failedSave = page.waitForRequest(request => request.url().endsWith("/admin/models/deepseek-ready") && request.method() === "PUT");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  expect((await failedSave).postDataJSON().api_key).toBe(testKey);
  await expect(dialog.getByRole("alert")).toContainText("保存暂时不可用");
  await expect(keyInput).toHaveValue("");
  expect(await page.evaluate(key => [...Object.values(localStorage), ...Object.values(sessionStorage)].some(value => value.includes(key)), testKey)).toBe(false);
  await page.unroute("**/api/cloud/admin/models/deepseek-ready");
  await keyInput.fill(testKey);
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  await expect(dialog).toBeHidden();
  await expect(row).toContainText("管理界面");
  await row.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(keyInput).toHaveValue("");
  await dialog.getByRole("checkbox", { name: "删除已保存的 API Key" }).check();
  await expect(keyInput).toBeDisabled();
  await expect(dialog.getByRole("checkbox", { name: "向用户开放此模型" })).not.toBeChecked();
  const cleared = page.waitForRequest(request => request.url().endsWith("/admin/models/deepseek-ready") && request.method() === "PUT");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  const clearBody = (await cleared).postDataJSON();
  expect(clearBody.clear_api_key).toBe(true);
  expect(clearBody.enabled).toBe(false);
  expect(clearBody).not.toHaveProperty("api_key");
  await expect(dialog).toBeHidden();
  await expect(row.getByText("未配置", { exact: true })).toHaveCount(2);
  await expect(row.getByRole("button", { name: "连接测试" })).toBeDisabled();
});

test("shared custom routes preserve, replace and clear write-only headers with explicit anonymous authentication", async ({ page }) => {
  await cloudFixture(page, true);
  await login(page);
  await page.getByRole("tab", { name: "模型路由" }).click();
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("路由 ID", { exact: true }).fill("header-route");
  await dialog.getByLabel("展示名称").fill("请求头认证模型");
  await dialog.getByLabel("服务商标识").fill("custom_team");
  await dialog.getByText("自定义请求头", { exact: true }).click();
  await dialog.getByLabel("请求头操作").selectOption("replace");
  await dialog.getByLabel("请求头名称 1", { exact: true }).fill("Authorization");
  const value = dialog.getByLabel("请求头值 1", { exact: true });
  await expect(value).toHaveAttribute("type", "password");
  const firstSecret = "Bearer placeholder-header-not-a-live-secret";
  await value.fill(firstSecret);
  await dialog.getByRole("checkbox", { name: "向用户开放此模型" }).check();
  const created = page.waitForRequest(request => request.url().endsWith("/admin/models") && request.method() === "POST");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  expect((await created).postDataJSON()).toMatchObject({ custom_headers: { Authorization: firstSecret }, allow_unauthenticated: false, enabled: true });
  expect((await created).postDataJSON()).not.toHaveProperty("api_key");
  await expect(dialog).toBeHidden();
  const row = page.getByRole("row").filter({ hasText: "请求头认证模型" });
  await expect(row).toContainText("自定义请求头");
  await expect(row.getByRole("button", { name: "连接测试" })).toBeEnabled();
  await row.getByRole("button", { name: "编辑", exact: true }).click();
  await dialog.getByText("自定义请求头", { exact: true }).click();
  await expect(dialog.getByLabel("请求头操作")).toHaveValue("preserve");
  const preserved = page.waitForRequest(request => request.url().endsWith("/admin/models/header-route") && request.method() === "PUT");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  expect((await preserved).postDataJSON()).not.toHaveProperty("custom_headers");
  await expect(dialog).toBeHidden();
  await row.getByRole("button", { name: "编辑", exact: true }).click();
  await dialog.getByText("自定义请求头", { exact: true }).click();
  await dialog.getByLabel("请求头操作").selectOption("replace");
  await expect(value).toHaveValue("");
  await dialog.getByLabel("请求头名称 1", { exact: true }).fill("X-Team-Key");
  const replacement = "placeholder-replacement-header-never-live";
  await value.fill(replacement);
  await page.route("**/api/cloud/admin/models/header-route", route => route.fulfill({ status: 503, json: { detail: "保存暂时不可用" } }));
  const failed = page.waitForRequest(request => request.url().endsWith("/admin/models/header-route") && request.method() === "PUT");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  expect((await failed).postDataJSON().custom_headers).toEqual({ "X-Team-Key": replacement });
  await expect(dialog.getByRole("alert")).toContainText("保存暂时不可用");
  await expect(value).toHaveValue("");
  expect(await page.evaluate(secrets => [...Object.values(localStorage), ...Object.values(sessionStorage)].some(stored => secrets.some(secret => stored.includes(secret))), [firstSecret, replacement])).toBe(false);
  await page.unroute("**/api/cloud/admin/models/header-route");
  await dialog.getByLabel("请求头操作").selectOption("clear");
  await dialog.getByRole("checkbox", { name: "无需身份验证", exact: true }).check();
  const cleared = page.waitForRequest(request => request.url().endsWith("/admin/models/header-route") && request.method() === "PUT");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  expect((await cleared).postDataJSON()).toMatchObject({ custom_headers: {}, allow_unauthenticated: true, enabled: true });
  await expect(dialog).toBeHidden();
  await expect(row).toContainText("无需身份验证");
  await expect(row.getByRole("button", { name: "连接测试" })).toBeEnabled();
});

test("private header-only routes expose an explicit connection test without persisting header values", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await login(page);
  await page.getByRole("button", { name: "个人模型", exact: true }).click();
  await page.getByRole("button", { name: "添加个人模型" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("路由 ID", { exact: true }).fill("private-header");
  await dialog.getByLabel("展示名称").fill("个人请求头模型");
  await dialog.getByLabel("服务商标识").fill("openai");
  await dialog.getByText("自定义请求头", { exact: true }).click();
  await dialog.getByLabel("请求头操作").selectOption("replace");
  await dialog.getByLabel("请求头名称 1", { exact: true }).fill("X-Api-Key");
  await dialog.getByLabel("请求头值 1", { exact: true }).fill("placeholder-private-header-not-live");
  await dialog.getByRole("checkbox", { name: "向用户开放此模型" }).check();
  const saved = page.waitForRequest(request => request.url().endsWith("/user/models") && request.method() === "POST");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  const body = (await saved).postDataJSON();
  expect(body).toMatchObject({ custom_headers: { "X-Api-Key": "placeholder-private-header-not-live" }, api_key_env: "", enabled: true });
  expect(body).not.toHaveProperty("api_key");
  expect(body).not.toHaveProperty("owner_user_id");
  await expect(dialog).toBeHidden();
  const row = page.getByRole("row").filter({ hasText: "个人请求头模型" });
  await row.getByRole("button", { name: "连接测试" }).click();
  expect(fixture.calls.some(call => call.path === "/api/cloud/jobs" && call.method === "POST")).toBe(false);
  const submitted = page.waitForRequest(request => request.url().endsWith("/jobs") && request.method() === "POST");
  await page.getByRole("dialog").getByRole("button", { name: "开始连接测试", exact: true }).click();
  const sent = await submitted;
  expect(sent.postDataJSON()).toMatchObject({ model_id: "user.user-1.private-header", max_output_tokens: 128 });
  expect(await sent.headerValue("x-csrf-token")).toBe("test-csrf-token");
  expect(await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].some(stored => stored.includes("placeholder-private-header-not-live")))).toBe(false);
});

test("connection testing uses an owned workspace, worker job and settled usage ledger", async ({ page }) => {
  const fixture = await cloudFixture(page, true);
  const now = "2026-11-15T08:00:00Z";
  const submitted: Record<string, unknown>[] = [];
  let polls = 0;
  const job = { id: "connection-job", user_id: "user-1", conversation_id: "conversation-1", state: "running", prompt: "请只回答“连接正常”", model_id: "deepseek-ready", answer: "", error: "", cancel_requested: false, created_at: now, updated_at: now };
  await page.route("**/api/cloud/workspaces", route => route.request().method() === "GET" ? route.fulfill({ json: [{ id: "connection-workspace", name: "模型连接测试", created_at: now }] }) : route.fallback());
  await page.route("**/api/cloud/jobs", route => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted.push(route.request().postDataJSON());
    return route.fulfill({ status: 201, json: job });
  });
  await page.route("**/api/cloud/jobs/connection-job", route => route.fulfill({ json: ++polls === 1 ? job : { ...job, state: "completed", answer: "连接正常" } }));
  await page.route("**/api/cloud/usage/requests", route => route.fulfill({ json: [
    { id: "connection-receipt", job_id: job.id, state: "settled", total_tokens: 18 },
    { id: "other-job-receipt", job_id: "another-job", state: "settled", total_tokens: 999 },
  ] }));
  await login(page);
  await page.getByRole("button", { name: "管理后台", exact: true }).click();
  await page.getByRole("tab", { name: "模型路由" }).click();
  await page.getByRole("row").filter({ hasText: "DeepSeek 已配置" }).getByRole("button", { name: "连接测试", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const conversation = page.waitForRequest(request => request.url().endsWith("/conversations") && request.method() === "POST");
  await dialog.getByRole("button", { name: "开始连接测试", exact: true }).click();
  expect((await conversation).postDataJSON().workspace_id).toBe("connection-workspace");
  await expect(dialog.getByText("连接正常", { exact: true })).toBeVisible();
  await expect(dialog.getByText("18", { exact: true })).toBeVisible();
  await expect(dialog.locator(".cloud-metric").filter({ hasText: "已结算请求" }).getByText("1", { exact: true })).toBeVisible();
  expect(submitted).toHaveLength(1);
  expect(submitted[0]).toMatchObject({ conversation_id: "conversation-1", model_id: "deepseek-ready", prompt: "请只回答“连接正常”", max_output_tokens: 128, file_ids: [] });
  expect(submitted[0].idempotency_key).toEqual(expect.any(String));
  expect(fixture.calls.filter(call => call.path === "/api/cloud/workspaces" && call.method === "POST")).toHaveLength(0);
  expect(fixture.calls.every(call => call.path.startsWith("/api/cloud/"))).toBe(true);
});

test("connection query recovery preserves the original failed job and its unresolved usage", async ({ page }) => {
  await cloudFixture(page, true);
  let submits = 0;
  let polls = 0;
  const job = { id: "failed-connection-job", user_id: "user-1", conversation_id: "conversation-1", state: "running", prompt: "请只回答“连接正常”", model_id: "deepseek-ready", answer: "", error: "", cancel_requested: false, created_at: "2026-11-15T08:00:00Z", updated_at: "2026-11-15T08:00:00Z" };
  await page.route("**/api/cloud/jobs", route => {
    if (route.request().method() !== "POST") return route.fallback();
    submits += 1;
    return route.fulfill({ status: 201, json: job });
  });
  await page.route("**/api/cloud/jobs/failed-connection-job", route => ++polls === 1 ? route.fulfill({ status: 503, json: { detail: "查询暂时不可用" } }) : route.fulfill({ json: { ...job, state: "needs_review", error: "供应商响应中断，实际用量待核查" } }));
  await page.route("**/api/cloud/usage/requests", route => route.fulfill({ json: [{ id: "unresolved-connection", job_id: job.id, state: "unknown", total_tokens: 0 }] }));
  await login(page);
  await page.getByRole("button", { name: "管理后台", exact: true }).click();
  await page.getByRole("tab", { name: "模型路由" }).click();
  await page.getByRole("row").filter({ hasText: "DeepSeek 已配置" }).getByRole("button", { name: "连接测试", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "开始连接测试", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("查询中断：查询暂时不可用");
  await dialog.getByRole("button", { name: "恢复查询", exact: true }).click();
  await expect(dialog).toContainText("供应商响应中断，实际用量待核查");
  await expect(dialog).toContainText("对应预留额度暂时保留");
  expect(submits).toBe(1);
});

test("mobile account navigation and the full workbench link remain usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await cloudFixture(page);
  await login(page);
  await page.getByRole("button", { name: "展开导航" }).click();
  await expect(page.getByRole("link", { name: "完整工作台" })).toBeVisible();
  await page.getByRole("button", { name: "用量与额度", exact: true }).click();
  await expect(page.getByRole("heading", { name: "用量与额度", level: 1, exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("expired authentication clears the protected workspace", async ({ page }) => {
  await cloudFixture(page);
  await login(page);
  await page.route("**/api/cloud/auth/me", route => route.fulfill({ status: 401, json: { detail: "Session expired" } }));
  await page.route("**/api/cloud/usage", route => route.fulfill({ status: 401, json: { detail: { code: "session_expired", message: "Session expired" } } }));
  await page.getByRole("button", { name: "刷新数据", exact: true }).click();
  await expect(page.getByRole("main", { name: "登录", exact: true })).toBeVisible();
  await expect(page.getByText("登录已失效，请重新登录。", { exact: true })).toBeVisible();
  await expect(page.getByText("云端用户", { exact: true })).toHaveCount(0);
});

test("usage distinguishes unknown prices from calculated cost", async ({ page }) => {
  await cloudFixture(page);
  await page.route("**/api/cloud/usage", route => route.fulfill({ json: { period: "2026-11", limit_tokens: 100000, used_tokens: 60, reserved_tokens: 0, remaining_tokens: 99940, unknown_requests: 0, unpriced_requests: 1, total_cost_microusd: 500, by_model: [{ model_id: "office-route", total_tokens: 60, cost_microusd: 500, unpriced_requests: 1 }], daily: [] } }));
  const record = { user_id: "user-1", model_id: "office-route", state: "settled", reserved_tokens: 50, total_tokens: 30, error: "", created_at: "2026-11-15T08:00:00Z" };
  await page.route("**/api/cloud/usage/requests", route => route.fulfill({ json: [
    { ...record, id: "unknown-price", cost_microusd: 0, price_snapshot: { version: 1, status: "unknown", route_id: "office-route", currency: "USD", calculation: "input_output_total_ceil_microusd_v1" } },
    { ...record, id: "configured-price", cost_microusd: 500, price_snapshot: { version: 1, status: "route_configured", route_id: "office-route", currency: "USD", calculation: "input_output_total_ceil_microusd_v1", provider: "openai", model: "office-model", input_price_microusd_per_million: 1000000, output_price_microusd_per_million: 2000000 } },
  ] }));
  await login(page);
  await page.getByRole("button", { name: "用量与额度", exact: true }).click();
  await expect(page.locator(".cloud-metric").filter({ hasText: "价格未知请求" }).getByText("1", { exact: true })).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "unknown-pric" })).toContainText("价格未知");
  const priced = page.getByRole("row").filter({ hasText: "configured-p" });
  await expect(priced).toContainText("$0.0005");
  await priced.getByText("价格依据").click();
  await expect(priced).toContainText("输入 $1.0000 / 百万 Token");
});

test("private BYOK image routes keep keys write-only and require an explicit paid generation action", async ({ page }) => {
  const fixture = await cloudFixture(page);
  await login(page);
  await page.getByRole("button", { name: "个人模型", exact: true }).click();
  await page.getByRole("button", { name: "添加个人模型" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("模型用途").selectOption("image");
  await expect(dialog.getByLabel("服务商标识")).toHaveValue("siliconflow");
  await expect(dialog.getByLabel("实际模型 ID")).toHaveValue("Kwai-Kolors/Kolors");
  await expect(dialog.getByLabel("密钥环境变量名", { exact: false })).toHaveCount(0);
  await dialog.getByLabel("路由 ID", { exact: true }).fill("private-image");
  await dialog.getByLabel("展示名称").fill("个人图像模型");
  await dialog.getByLabel("API Key", { exact: true }).fill("sk-test-placeholder-not-a-live-key");
  await dialog.getByLabel("单张图片价格（微美元）").fill("10000");
  await dialog.getByRole("checkbox", { name: "向用户开放此模型" }).check();
  const save = page.waitForRequest(request => request.url().endsWith("/user/models") && request.method() === "POST");
  await dialog.getByRole("button", { name: "保存模型路由" }).click();
  const body = (await save).postDataJSON();
  expect(body).toMatchObject({ kind: "image", provider: "siliconflow", image_price_microusd: 10000, api_key_env: "" });
  expect(body).not.toHaveProperty("owner_user_id");
  await expect(dialog).toBeHidden();
  const row = page.getByRole("row").filter({ hasText: "个人图像模型" });
  await row.getByRole("button", { name: "生成测试图" }).click();
  expect(fixture.calls.some(call => call.path.endsWith("/images/generations"))).toBe(false);
  await page.getByRole("dialog").getByRole("button", { name: "确认生成 1 张（消耗图片额度）" }).click();
  await expect(page.getByRole("img", { name: "模型生成的测试图片" })).toBeVisible();
  expect(fixture.calls.filter(call => call.path.endsWith("/images/generations"))).toEqual([expect.objectContaining({ method: "POST", csrf: "test-csrf-token" })]);
  expect(await page.evaluate(() => Object.values(localStorage).some(value => value.includes("sk-test-placeholder")))).toBe(false);
});

test("administrator configures a dedicated Mac node with a write-only token and preserves credentials on update", async ({ page }) => {
  await cloudFixture(page, true);
  let configured = false;
  const payloads: Record<string, unknown>[] = [];
  await page.route("**/api/cloud/admin/users/user-1/mlx-node", route => {
    if (route.request().method() === "PUT") {
      payloads.push(route.request().postDataJSON()); configured = true;
    }
    return route.fulfill({ json: { configured, enabled: configured, url: configured ? "https://mac.example.test:9443" : "", certificate_fingerprint: configured ? "ab".repeat(32) : "", token_configured: configured } });
  });
  await login(page);
  await page.getByRole("button", { name: "专属 Mac 节点", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("HTTPS 节点地址").fill("https://mac.example.test:9443");
  await dialog.getByLabel("节点令牌").fill("placeholder-node-token-not-a-live-secret");
  await dialog.getByLabel("节点证书 PEM").fill("-----BEGIN CERTIFICATE-----\ntest-fixture-only\n-----END CERTIFICATE-----");
  await dialog.getByRole("checkbox", { name: "启用专属 Mac 节点" }).check();
  await dialog.getByRole("button", { name: "保存节点" }).click();
  await expect(dialog).toBeHidden();
  expect(payloads[0]).toMatchObject({ enabled: true, token: "placeholder-node-token-not-a-live-secret", url: "https://mac.example.test:9443" });
  expect(await page.evaluate(() => [...Object.values(localStorage), ...Object.values(sessionStorage)].some(value => value.includes("placeholder-node-token")))).toBe(false);
  await page.getByRole("button", { name: "专属 Mac 节点", exact: true }).click();
  await expect(dialog.getByLabel("节点令牌")).toHaveValue("");
  await expect(dialog.getByLabel("节点证书 PEM")).toHaveValue("");
  await expect(dialog.getByText("ab".repeat(32))).toBeVisible();
  await dialog.getByRole("checkbox", { name: "启用专属 Mac 节点" }).uncheck();
  await dialog.getByRole("button", { name: "保存节点" }).click();
  await expect(dialog).toBeHidden();
  expect(payloads[1]).toEqual({ enabled: false, url: "https://mac.example.test:9443" });
});

test("image ledger displays picture counts and historical per-image prices", async ({ page }) => {
  await cloudFixture(page, true);
  const record = { id: "priced-image", user_id: "user-1", model_id: "image-route", source: "image", state: "settled", reserved_tokens: 0, total_tokens: 0, cost_microusd: 10000, error: "", created_at: "2026-11-15T08:00:00Z", price_snapshot: { version: 1, unit: "image", currency: "USD", model: "Kwai-Kolors/Kolors", image_price_microusd: 10000 }, normalized_usage: { unit: "image", count: 1, cost_status: "calculated" } };
  await page.route("**/api/cloud/admin/usage/requests", route => route.fulfill({ json: [record, { ...record, id: "unknown-image", state: "unknown", normalized_usage: {} }] }));
  await login(page);
  await page.getByRole("tab", { name: "用量与核查" }).click();
  const row = page.getByRole("row").filter({ hasText: "priced-image" });
  await expect(row).toContainText("1 张");
  await expect(row).toContainText("$0.0100");
  await row.getByText("价格依据").click();
  await expect(row).toContainText("$0.0100 / 张");
  await page.getByRole("row").filter({ hasText: "unknown-imag" }).getByRole("button", { name: "核查处理" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("已确认图片数量")).toHaveValue("1");
  await expect(dialog.getByLabel("输入总 Token")).toHaveCount(0);
  await dialog.getByLabel("核查依据与原因").fill("已确认供应商生成一张图片");
  await page.route("**/api/cloud/admin/requests/unknown-image/reconcile", route => route.fulfill({ json: { ok: true } }));
  const save = page.waitForRequest(request => request.url().endsWith("/unknown-image/reconcile"));
  await dialog.getByRole("button", { name: "确认并记录核查结果" }).click();
  expect((await save).postDataJSON()).toMatchObject({ action: "settle", usage: { count: 1 } });
});
