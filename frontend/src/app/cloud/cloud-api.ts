/** Cookie-authenticated cloud transport. Credentials and user data are memory-only. */
export const CLOUD_API = "/api/cloud";
let csrfToken = "";
let unauthorized: (() => void) | undefined;
const pending = new Set<AbortController>();

export class CloudError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export function setCloudSession(token: string, onUnauthorized?: () => void) {
  csrfToken = token;
  unauthorized = onUnauthorized;
}

export function clearCloudSession() {
  csrfToken = "";
  unauthorized = undefined;
  for (const controller of pending) controller.abort();
  pending.clear();
}

export async function cloudRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  pending.add(controller);
  const headers = new Headers(options.headers);
  const method = options.method?.toUpperCase() ?? "GET";
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const onAbort = () => controller.abort();
  options.signal?.addEventListener("abort", onAbort, { once: true });
  if (options.signal?.aborted) controller.abort();
  try {
    const response = await fetch(`${CLOUD_API}${path}`, { ...options, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload.detail;
      const code = detail?.code ?? String(response.status);
      const translated: Record<string, string> = { invalid_password: "当前密码不正确", forbidden: "此操作需要管理员权限", csrf_rejected: "安全凭证已失效，请刷新页面后重试", origin_rejected: "请求来源未获允许，请使用管理员提供的工作台地址", account_disabled: "账户已停用，请联系管理员", quota_exceeded: "本月可用额度不足", concurrency_exceeded: "同时运行的任务已达到上限" };
      const message = translated[code] || (Array.isArray(detail) ? detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.slice(1).join(".") || "参数"}：${item.msg || "格式无效"}`).join("；") : typeof detail === "string" ? detail : detail?.message);
      if (response.status === 401 && path !== "/auth/login") unauthorized?.();
      throw new CloudError(response.status, code, message || `请求未成功（${response.status}）`);
    }
    if (response.status === 204) return undefined as T;
    // Keep the external abort listener until the response body is consumed.
    return await response.json();
  } finally {
    pending.delete(controller);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

export const postCloud = <T,>(path: string, body?: unknown) => cloudRequest<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export async function downloadCloud(path: string, filename: string) {
  const controller = new AbortController();
  pending.add(controller);
  try {
    const response = await fetch(`${CLOUD_API}${path}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal });
    if (!response.ok) {
      if (response.status === 401) unauthorized?.();
      const payload = await response.json().catch(() => ({}));
      throw new CloudError(response.status, payload.detail?.code ?? String(response.status), payload.detail?.message || "下载失败，请稍后重试。");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } finally { pending.delete(controller); }
}

export function cloudErrorMessage(error: unknown) {
  if (error instanceof CloudError) {
    if (error.status === 401) return "登录已失效，请重新登录。";
    if (error.status === 429) return `暂时无法提交：${error.message}。请查看额度或等待正在运行的任务结束。`;
    if (error.status === 403) return `操作未获授权：${error.message}`;
    if (error.status === 404) return "资源不存在，或你没有访问权限。";
    return error.message;
  }
  return error instanceof Error && error.name === "AbortError" ? "请求已取消。" : "连接服务失败，请检查网络后重试。";
}
