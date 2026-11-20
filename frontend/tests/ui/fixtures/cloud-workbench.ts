import type { Page } from "@playwright/test";
import { mock苏小有Api, seed苏小有Storage } from "./suxiaoyou-api";
import { captchaFixture } from "./cloud-captcha";

export async function fullWorkbenchFixture(page: Page, options: { signedIn?: boolean; runtimeReady?: boolean; unavailable?: boolean } = {}) {
  await seed苏小有Storage(page);
  await page.addInitScript(() => {
    const settings = JSON.parse(localStorage.getItem("suxiaoyou-settings") || "{}");
    settings.state.workspaceDirectory = "/tenant/workspaces/project-a";
    localStorage.setItem("suxiaoyou:cloud:cloud-user-a:suxiaoyou-settings", JSON.stringify(settings));
    localStorage.setItem("suxiaoyou:cloud:cloud-user-a:suxiaoyou-language", "en");
    localStorage.setItem("suxiaoyou_remote_connection", JSON.stringify({ url: "https://stale-desktop.invalid", token: "never-forward-this-old-token" }));
  });
  const engine = await mock苏小有Api(page);
  // Match the real tenant gateway's adapter ID rather than a desktop-only BYOK ID.
  await page.route("**/api/models", route => route.fulfill({ json: [{
    id: "deepseek-office", name: "DeepSeek", provider_id: "custom_cloud",
    capabilities: { function_calling: true, vision: false, reasoning: false, json_output: false, max_context: 128000, max_output: null, prompt_caching: false },
    pricing: { prompt: 0, completion: 0 }, metadata: {},
  }] }));
  let signedIn = options.signedIn ?? true;
  let ready = options.runtimeReady ?? false;
  const captcha = captchaFixture();
  const user = { id: "cloud-user-a", email: "a@example.test", display_name: "Cloud User A", role: "admin", is_active: true };
  const calls: { path: string; method: string; csrf?: string; authorization?: string }[] = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/")) calls.push({ path: url.pathname, method: request.method(), csrf: request.headers()["x-csrf-token"], authorization: request.headers()["authorization"] });
  });
  await page.route("**/api/cloud/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (await captcha.handle(route)) return;
    if (path === "/api/cloud/auth/login") {
      if (!captcha.consume(route.request().postDataJSON().captcha_proof)) return route.fulfill({ status: 403, json: { detail: { code: "captcha_required", message: "请先完成拼图验证" } } });
      signedIn = true; return route.fulfill({ json: { user, csrf_token: "memory-only-csrf" } });
    }
    if (path === "/api/cloud/auth/me") return route.fulfill(signedIn ? { json: { user, csrf_token: "memory-only-csrf" } } : { status: 401, json: { detail: "Sign in" } });
    if (path === "/api/cloud/auth/logout") { signedIn = false; return route.fulfill({ json: { ok: true } }); }
    if (path === "/api/cloud/runtime/status") return route.fulfill({ json: { status: ready ? "ready" : "stopped", ready, workspace: "/tenant/workspaces" } });
    if (path === "/api/cloud/runtime/start") {
      if (options.unavailable) return route.fulfill({ status: 503, json: { detail: { code: "runtime_unavailable", message: "运行环境尚未启动" } } });
      ready = true;
      return route.fulfill({ json: { status: "ready", ready: true, workspace: "/tenant/workspaces" } });
    }
    if (path === "/api/cloud/runtime/stop" || path === "/api/cloud/runtime/restart") {
      ready = path.endsWith("/restart");
      return route.fulfill({ json: { status: ready ? "ready" : "stopped", ready, workspace: "/tenant/workspaces" } });
    }
    return route.fulfill({ status: 404, json: { detail: "Unhandled cloud fixture" } });
  });
  return { engine, calls, captcha, expire: () => { signedIn = false; } };
}

