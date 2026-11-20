"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AppProviders } from "./app-providers";
import { activateCloudIdentity, leaveCloudWorkspace, type CloudIdentity } from "@/lib/cloud-session";
import { CloudIdentityContext } from "./cloud-identity-context";

type AuthResponse = { user: CloudIdentity; csrf_token: string };
type RuntimeStatus = { status?: string; state?: string; workspace?: string; workspace_root?: string; error?: string };

async function bootstrapRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/cloud${path}`, { ...init, credentials: "same-origin", cache: "no-store" });
  if (response.status === 401) {
    void leaveCloudWorkspace();
    throw new Error("登录已失效");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    throw new Error(typeof detail === "string" ? detail : detail?.message || `工作区启动失败（${response.status}）`);
  }
  return response.json();
}

export function CloudSessionBoundary({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CloudIdentity | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    let stopped = false;
    async function initialize() {
      try {
        const auth = await bootstrapRequest<AuthResponse>("/auth/me", { signal: abort.signal });
        if (stopped) return;
        activateCloudIdentity(auth.user, auth.csrf_token);
        let runtime = await bootstrapRequest<RuntimeStatus>("/runtime/status", { signal: abort.signal });
        if (!["ready", "running"].includes(runtime.status || runtime.state || "")) {
          runtime = await bootstrapRequest<RuntimeStatus>("/runtime/start", { method: "POST", headers: { "X-CSRF-Token": auth.csrf_token }, signal: abort.signal });
        }
        if (stopped) return;
        if (!["ready", "running"].includes(runtime.status || runtime.state || "")) throw new Error(runtime.error || "工作区尚未就绪");
        const [{ useSettingsStore }, { useSidebarStore }, { useAppearanceStore }] = await Promise.all([
          import("@/stores/settings-store"), import("@/stores/sidebar-store"), import("@/stores/appearance-store"),
        ]);
        await Promise.all([useSettingsStore.persist.rehydrate(), useSidebarStore.persist.rehydrate(), useAppearanceStore.persist.rehydrate()]);
        if (stopped) return;
        const settings = useSettingsStore.getState();
        const workspace = runtime.workspace || runtime.workspace_root;
        if (!settings.workspaceDirectory && workspace) settings.setWorkspaceDirectory(workspace);
        if (!settings.activeProvider) settings.setActiveProvider("byok");
        // The runtime already creates the first private workspace; cloud has no native onboarding.
        settings.completeOnboarding();
        setUser(auth.user);
      } catch (failure) {
        if (!stopped && !(failure instanceof DOMException && failure.name === "AbortError")) setError(failure instanceof Error ? failure.message : "无法连接工作区");
      }
    }
    void initialize();
    return () => { stopped = true; abort.abort(); };
  }, [revision]);

  useEffect(() => {
    if (!user) return;
    const verifyIdentity = async () => {
      try {
        const auth = await bootstrapRequest<AuthResponse>("/auth/me");
        if (auth.user.id !== user.id) await leaveCloudWorkspace("/c/new");
        else activateCloudIdentity(auth.user, auth.csrf_token);
      } catch { /* The next authenticated request also handles expiration. */ }
    };
    const interval = setInterval(() => { void verifyIdentity(); }, 30_000);
    const onFocus = () => { void verifyIdentity(); };
    window.addEventListener("focus", onFocus);
    return () => { clearInterval(interval); window.removeEventListener("focus", onFocus); };
  }, [user]);

  if (!user) return <main className="flex h-full min-h-screen items-center justify-center bg-[var(--surface-primary)] text-[var(--text-primary)]"><div className="flex flex-col items-center gap-4"><h1 className="text-xl font-medium">苏小有</h1>{error ? <><p role="alert">{error}</p><button onClick={() => { setError(""); setRevision(value => value + 1); }} className="rounded-lg border px-4 py-2">重新连接</button><a href="/cloud">账户与管理</a></> : <p role="status">正在启动工作区…</p>}</div></main>;
  return <CloudIdentityContext.Provider value={user}><AppProviders key={user.id}>{children}</AppProviders></CloudIdentityContext.Provider>;
}
