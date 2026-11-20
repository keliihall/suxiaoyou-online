import { expect, type Page } from "@playwright/test";
import { captchaFixture, completeCaptcha } from "./cloud-captcha";

export async function cloudFixture(page: Page, admin = false) {
  let signedIn = false;
  let finished = false;
  let prompt = "";
  const captcha = captchaFixture();
  const calls: { path: string; method: string; csrf: string | undefined }[] = [];
  const user = { id: "user-1", email: "cloud@example.com", display_name: "云端用户", role: admin ? "admin" : "user", is_active: true, token_limit: 100000, image_limit: 100, max_concurrent_jobs: 2 };
  const auth = { user, csrf_token: "test-csrf-token" };
  const workspaces: Record<string, unknown>[] = [];
  const conversations: Record<string, unknown>[] = [];
  const files: Record<string, unknown>[] = [];
  const messages: Record<string, unknown>[] = [];
  const models: Record<string, unknown>[] = [
    { id: "deepseek-pending", name: "DeepSeek 待配置", provider: "deepseek", model: "deepseek-flash", enabled: false, strict_quota: false, api_key_env: "SUXIAOYOU_MODEL_PENDING_KEY", key_configured: false, credential_source: "none" },
    { id: "deepseek-ready", name: "DeepSeek 已配置", provider: "deepseek", model: "deepseek-flash", enabled: true, strict_quota: false, api_key_env: "", key_configured: true, credential_source: "managed" },
    { id: "mock-only", name: "纯测试路由", provider: "mock", model: "mock", enabled: false, strict_quota: true, key_configured: false, credential_source: "none" },
  ];
  const privateModels: Record<string, unknown>[] = [];
  function savedModel(body: Record<string, unknown>, previous?: Record<string, unknown>) {
    const model = { ...previous, ...body };
    if (body.api_key) { model.key_configured = true; model.credential_source = "managed"; }
    else if (body.clear_api_key || !previous) { model.key_configured = Boolean(body.api_key_env); model.credential_source = body.api_key_env ? "environment" : "none"; }
    if (body.custom_headers && typeof body.custom_headers === "object") model.custom_headers_configured = Object.keys(body.custom_headers).length > 0;
    model.authentication_mode = model.allow_unauthenticated ? "none" : model.key_configured ? (model.custom_headers_configured ? "api_key_and_headers" : "api_key") : model.custom_headers_configured ? "headers" : "missing";
    model.authentication_configured = model.authentication_mode !== "missing";
    delete model.api_key; delete model.clear_api_key; delete model.custom_headers;
    return model;
  }
  const now = "2026-11-15T08:00:00Z";
  const usage = { period: "2026-11", limit_tokens: 100000, used_tokens: 0, reserved_tokens: 0, remaining_tokens: 100000, unknown_requests: 0, unpriced_requests: 0, total_cost_microusd: 0, by_model: [], daily: [] };
  const job = () => ({ id: "job-1", user_id: user.id, conversation_id: "conversation-1", state: finished ? "completed" : "running", prompt, model_id: "mock-office", answer: finished ? "已根据参考材料完成整理。" : "", error: "", cancel_requested: false, created_at: now, updated_at: now });
  await page.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/cloud", "");
    const method = request.method();
    calls.push({ path: url.pathname, method, csrf: request.headers()["x-csrf-token"] });
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (await captcha.handle(route)) return;
    if (path === "/auth/me") return signedIn ? json(auth) : json({ detail: { code: "unauthenticated", message: "Sign in" } }, 401);
    if (path === "/auth/login") {
      if (!captcha.consume(request.postDataJSON().captcha_proof)) return json({ detail: { code: "captcha_required", message: "请先完成拼图验证" } }, 403);
      signedIn = true; return json(auth);
    }
    if (path === "/auth/logout") { signedIn = false; return json({ ok: true }); }
    if (!signedIn) return json({ detail: "Authentication required" }, 401);
    if (path === "/workspaces" && method === "POST") { const body = request.postDataJSON(); const workspace = { id: "workspace-1", name: body.name, created_at: now }; workspaces.push(workspace); return json(workspace, 201); }
    if (path === "/workspaces") return json(workspaces);
    if (path === "/conversations" && method === "POST") { const conversation = { id: "conversation-1", workspace_id: "workspace-1", title: request.postDataJSON().title, created_at: now, updated_at: now }; conversations.push(conversation); return json(conversation, 201); }
    if (path === "/conversations") return json(conversations);
    if (path === "/conversations/conversation-1") return json({ ...conversations[0], messages });
    if (path === "/files" && method === "POST") { const file = { id: "file-1", workspace_id: "workspace-1", filename: "notes.txt", content_type: "text/plain", size: 12, created_at: now }; files.push(file); return json(file, 201); }
    if (path === "/files") return json(files);
    if (path === "/models") return json([{ id: "mock-office", name: "测试办公模型", provider: "mock", model: "mock", enabled: true, strict_quota: true }]);
    if (path === "/usage") return json(usage);
    if (path === "/usage/requests") return json([]);
    if (path === "/jobs" && method === "POST") { prompt = request.postDataJSON().prompt; messages.push({ id: "message-1", role: "user", content: prompt, created_at: now }); return json(job(), 201); }
    if (path === "/jobs") return json(prompt ? [job()] : []);
    if (path === "/jobs/job-1/events") {
      finished = true;
      messages.push({ id: "message-2", role: "assistant", content: "已根据参考材料完成整理。", created_at: now });
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: 'id: 1\nevent: started\ndata: {}\n\nid: 2\nevent: delta\ndata: {"text":"已根据参考材料完成整理。"}\n\nid: 3\nevent: completed\ndata: {}\n\n' });
    }
    if (path === "/jobs/job-1") return json(job());
    if (path === "/user/models" && method === "GET") return json(privateModels);
    if (path === "/user/models" && method === "POST") {
      const body = request.postDataJSON();
      const model = savedModel({ ...body, id: `user.${user.id}.${body.id}`, owner_user_id: user.id });
      privateModels.push(model);
      return json(model, 201);
    }
    if (path.startsWith("/user/models/") && method === "DELETE") { privateModels.splice(0); return json({ ok: true }); }
    if (path === "/images/generations") return json({ data: [{ b64_json: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jSfUAAAAASUVORK5CYII=" }] });
    if (path === "/admin/users" && method === "POST") return json({ ...user, id: "user-2", ...request.postDataJSON() }, 201);
    if (path === "/admin/users") return json([user]);
    if ((path === "/admin/models" && method === "POST") || (path.startsWith("/admin/models/") && method === "PUT")) {
      const body = request.postDataJSON();
      const previous = models.find(model => model.id === body.id);
      const model = savedModel(body, previous);
      if (previous) models.splice(models.indexOf(previous), 1, model); else models.push(model);
      return json(model, method === "POST" ? 201 : 200);
    }
    if (path === "/admin/models") return json(models);
    if (path === "/admin/jobs" || path === "/admin/audit" || path === "/admin/usage/requests") return json([]);
    if (path === "/admin/usage") return json({ users: [{ ...usage, user_id: user.id, email: user.email }], total_tokens: 0, total_cost_microusd: 0 });
    return json({ detail: `Unexpected API: ${method} ${path}` }, 404);
  });
  return { calls, captcha };
}

export async function login(page: Page) {
  await page.goto("/cloud?manage=1");
  await expect(page.getByRole("main", { name: "登录", exact: true })).toBeVisible();
  await page.getByLabel("邮箱", { exact: true }).fill("cloud@example.com");
  await page.getByLabel("密码", { exact: true }).fill("cloud-password-123");
  await page.getByRole("button", { name: "登录工作台" }).click();
  await completeCaptcha(page);
  await expect(page.getByRole("heading", { name: /管理后台|用量与额度/ }).first()).toBeVisible();
}

