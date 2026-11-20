"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Copy, Loader2, RefreshCw, Server, Wifi } from "lucide-react";
import { api, apiErrorMessage } from "@/lib/api";
import { Button } from "@/components/ui/button";

type Runtime = { status: string; ready: boolean; workspace: string; error?: string };

export function CloudRemoteAccess() {
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [address, setAddress] = useState("");
  const refresh = useCallback(async () => {
    try { setRuntime(await api.get<Runtime>("/api/cloud/runtime/status")); }
    catch (failure) { setError(apiErrorMessage(failure, "读取运行环境失败")); }
  }, []);
  useEffect(() => { setAddress(`${window.location.origin}/c/new`); void refresh(); }, [refresh]);
  async function act(action: "start" | "restart" | "stop") {
    if (action !== "start" && !window.confirm(action === "stop" ? "停止运行环境？进行中的任务将中断。" : "重启运行环境？进行中的任务将中断。")) return;
    setBusy(true); setError("");
    try { setRuntime(await api.post<Runtime>(`/api/cloud/runtime/${action}`, undefined, { timeoutMs: 120_000 })); }
    catch (failure) { setError(apiErrorMessage(failure, "运行环境操作失败")); }
    finally { setBusy(false); }
  }
  const status = ({ ready: "运行中", starting: "启动中", stopped: "已停止", failed: "启动失败", unavailable: "暂不可用" } as Record<string, string>)[runtime?.status || ""] || "读取中";
  return (
    <main className="flex-1 overflow-y-auto bg-[var(--surface-chat)] scrollbar-auto">
      <section className="page-shell mx-auto w-full max-w-5xl px-5 pb-10 pt-20 sm:px-8 lg:py-10">
        <header className="page-header mb-8 flex min-h-12 items-center gap-3 border-b border-[var(--border-subtle)] pb-6">
          <Wifi className="h-6 w-6 text-[var(--text-secondary)]" />
          <h1 className="text-[26px] font-semibold tracking-tight text-[var(--text-primary)]">云端访问</h1>
        </header>
        <div className="page-content space-y-6">
          <section className="surface-card space-y-4 rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-5 shadow-[var(--shadow-sm)] sm:p-6">
            <label htmlFor="cloud-workbench-address" className="block text-[15px] font-semibold text-[var(--text-primary)]">工作台地址</label>
            <div className="flex gap-3">
              <input id="cloud-workbench-address" className="h-11 min-w-0 flex-1 rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)] px-4 text-[13px] text-[var(--text-secondary)] lg:h-10" readOnly value={address} />
              <Button variant="outline" size="icon" className="icon-action h-11 w-11 shrink-0 rounded-xl lg:h-10 lg:w-10" onClick={async () => {
                try {
                  await navigator.clipboard.writeText(address);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 2000);
                } catch { setError("无法复制，请选择地址后复制。"); }
              }} aria-label="复制工作台地址">
                {copied ? <Check size={18} /> : <Copy size={18} />}
              </Button>
            </div>
          </section>
          <section className="surface-card space-y-6 rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-5 shadow-[var(--shadow-sm)] sm:p-6">
            <div className="flex items-center gap-3">
              <Server size={18} className="shrink-0 text-[var(--text-secondary)]" />
              <h2 className="flex-1 text-[15px] font-semibold text-[var(--text-primary)]">个人运行环境</h2>
              <Button variant="ghost" size="icon" className="icon-action h-10 w-10 rounded-xl" onClick={() => void refresh()} aria-label="刷新运行环境"><RefreshCw size={18} /></Button>
            </div>
            <dl className="grid grid-cols-[auto_1fr] items-center gap-x-8 gap-y-4 text-[13px]">
              <dt className="text-[var(--text-secondary)]">状态</dt>
              <dd role="status"><span className="status-pill inline-flex items-center gap-2 rounded-lg bg-[var(--surface-secondary)] px-3 py-1.5 font-medium text-[var(--text-primary)]"><span className={`h-1.5 w-1.5 rounded-full ${runtime?.ready ? "bg-[var(--color-success)]" : "bg-[var(--text-tertiary)]"}`} />{status}</span></dd>
              <dt className="text-[var(--text-secondary)]">工作区</dt>
              <dd className="min-w-0 break-all font-mono text-[12px] text-[var(--text-primary)]">{runtime?.workspace || "—"}</dd>
            </dl>
            {runtime?.error && <p role="alert" className="rounded-xl border border-[var(--color-destructive)]/20 bg-[var(--color-destructive)]/5 p-4 text-[13px] text-[var(--color-destructive)]">{runtime.error}</p>}
            <div className="flex flex-wrap gap-3 border-t border-[var(--border-subtle)] pt-5">
              <Button className="min-h-10 gap-2 rounded-xl px-5" disabled={busy || runtime?.ready} onClick={() => void act("start")}>{busy && <Loader2 size={16} className="animate-spin" />}启动</Button>
              <Button variant="outline" className="min-h-10 rounded-xl px-5" disabled={busy || !runtime?.ready} onClick={() => void act("restart")}>重启</Button>
              <Button variant="outline" className="min-h-10 rounded-xl px-5" disabled={busy || !runtime?.ready} onClick={() => void act("stop")}>停止</Button>
            </div>
          </section>
          {error && <p role="alert" className="rounded-xl border border-[var(--color-destructive)]/20 bg-[var(--color-destructive)]/5 p-4 text-[13px] text-[var(--color-destructive)]">{error}</p>}
          {/* Cloud access uses the platform session. Tenant runtimes cannot create public tunnels. */}
        </div>
      </section>
    </main>
  );
}
