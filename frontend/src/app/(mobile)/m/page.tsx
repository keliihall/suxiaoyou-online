"use client";

import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useRouter } from "next/navigation";
import { SquarePen, Settings, Loader2, ChevronRight, Inbox, MessageCircle, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import { formatRelativeTime } from "@/lib/utils";
import { isRemoteMode, autoConnectFromUrl } from "@/lib/remote-connection";
import { useRemoteHealth, type RemoteHealthStatus } from "@/hooks/use-remote-health";
import { PullToRefresh } from "@/components/mobile/pull-to-refresh";
import type { SessionResponse } from "@/types/session";
import { IS_CLOUD } from "@/lib/cloud-session";

/** Small colored dot indicating remote connection health. */
function ConnectionDot({ status }: { status: RemoteHealthStatus }) {
  const { t } = useTranslation("settings");
  if (status === "unknown") return null;
  const label = status === "connected" ? t("mobileConnected") : status === "limited" ? t("mobileConnectionLimited", { defaultValue: "连接受限" }) : t("mobileDisconnected");
  const color =
    status === "connected"
      ? "bg-emerald-500"
      : status === "limited"
        ? "bg-amber-500"
        : "bg-red-500";
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${color} shrink-0`}
      role="img"
      aria-label={label}
      title={label}
    />
  );
}

/** Build mobile task route — uses query param in static export (same pattern as desktop). */
function getTaskRoute(sessionId: string): string {
  return `/m/task/_?sessionId=${encodeURIComponent(sessionId)}`;
}

export default function MobileTaskListPage() {
  const router = useRouter();
  const { t, i18n } = useTranslation("common");
  const healthStatus = useRemoteHealth();
  const [sessions, setSessions] = useState<SessionResponse[]>([]);
  const [activeSessionIds, setActiveSessionIds] = useState<Set<string>>(new Set());
  const [needsInputIds, setNeedsInputIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const loadSessions = useCallback(async () => {
    try {
      const [data, active] = await Promise.all([
        api.get<SessionResponse[]>(API.SESSIONS.LIST(30, 0)),
        api.get<{ stream_id: string; session_id: string; needs_input?: boolean }[]>(API.CHAT.ACTIVE).catch(() => []),
      ]);
      setSessions(data);
      setActiveSessionIds(new Set(active.map((j) => j.session_id)));
      setNeedsInputIds(new Set(active.filter((j) => j.needs_input).map((j) => j.session_id)));
    } catch (err) {
      console.error("Failed to load sessions:", err);
      toast.error(t("mobileLoadTasksFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    if (!IS_CLOUD) autoConnectFromUrl();
    if (!IS_CLOUD && !isRemoteMode()) {
      router.replace("/m/settings");
      return;
    }
    loadSessions();
  }, [router, loadSessions]);

  // Auto-poll when there are active sessions so badges update without pull-to-refresh
  useEffect(() => {
    if (activeSessionIds.size === 0) return;
    const timer = setInterval(loadSessions, 10_000);
    return () => clearInterval(timer);
  }, [activeSessionIds.size, loadSessions]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await loadSessions();
    } finally {
      setRefreshing(false);
    }
  }, [loadSessions]);

  return (
    <main className="flex h-full min-h-0 flex-col bg-[var(--surface-secondary)]">
      {/* Header */}
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 pb-3 pt-[max(env(safe-area-inset-top),16px)]">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight">{t("appName")}</h1>
          <ConnectionDot status={healthStatus} />
        </div>
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => void handleRefresh()}
            disabled={loading || refreshing}
            className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--text-secondary)] disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)] disabled:cursor-not-allowed"
            aria-label={t("chat:refresh")}
          >
            <RefreshCw className={`h-[18px] w-[18px] ${refreshing ? "animate-spin" : ""}`} aria-hidden="true" />
          </button>
          <button
            onClick={() => router.push("/m/new")}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--brand-primary)] text-[var(--brand-primary-text)] shadow-[var(--shadow-sm)] transition-colors hover:bg-[var(--brand-primary-hover)]"
            aria-label={t("mobileNewTask")}
          >
            <SquarePen className="w-[18px] h-[18px]" />
          </button>
          <button
            onClick={() => router.push("/m/settings")}
            className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--surface-secondary)]"
            aria-label={t("settings")}
          >
            <Settings className="w-[18px] h-[18px] text-[var(--text-secondary)]" />
          </button>
        </div>
      </header>

      {/* Task list with pull-to-refresh */}
      <PullToRefresh onRefresh={handleRefresh}>
        <div className="mx-auto w-full max-w-3xl px-4 pt-4 pb-[max(env(safe-area-inset-bottom),16px)]" aria-busy={loading || refreshing}>
          {loading ? (
            <div role="status" className="flex items-center justify-center pt-24" aria-label={t("mobileLoadingModels")}>
              <Loader2 className="w-5 h-5 animate-spin text-[var(--text-tertiary)]" aria-hidden="true" />
            </div>
          ) : sessions.length === 0 ? (
            <div className="surface-card flex flex-col items-center justify-center gap-4 px-6 py-16 text-center">
              <div className="h-12 w-12 rounded-2xl bg-[var(--brand-soft)] flex items-center justify-center">
                <Inbox className="w-5 h-5 text-[var(--text-tertiary)]" />
              </div>
              <p className="text-sm text-[var(--text-tertiary)]">{t("mobileNoTasks")}</p>
              <button
                onClick={() => router.push("/m/new")}
                className="mt-2 min-h-11 rounded-[var(--radius-control)] bg-[var(--brand-primary)] px-5 py-3 text-sm font-medium text-[var(--brand-primary-text)] transition-colors hover:bg-[var(--brand-primary-hover)]"
              >
                {t("mobileCreateFirstTask")}
              </button>
            </div>
          ) : (
            <div className="space-y-3">
              {sessions.map((session) => (
                <button
                  key={session.id}
                  onClick={() => router.push(getTaskRoute(session.id))}
                  className="surface-card group flex w-full items-center gap-3 px-4 py-4 text-left transition-colors hover:border-[var(--brand-border)] hover:bg-[var(--brand-soft)]"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="line-clamp-2 min-w-0 max-w-full text-[15px] font-semibold leading-snug break-words">
                        {session.title || t("mobileUntitledTask")}
                      </p>
                      {needsInputIds.has(session.id) ? (
                        <span className="shrink-0 inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-2 py-1 text-xs font-medium text-amber-700 dark:text-amber-300">
                          <MessageCircle className="w-2.5 h-2.5" />
                          {t("mobileNeedsInput")}
                        </span>
                      ) : activeSessionIds.has(session.id) ? (
                        <span className="shrink-0 inline-flex items-center gap-1 rounded-md bg-emerald-500/10 px-2 py-1 text-xs font-medium text-emerald-700 dark:text-emerald-300">
                          <Loader2 className="w-2.5 h-2.5 animate-spin" />
                          {t("mobileRunning")}
                        </span>
                      ) : null}
                    </div>
                    <p className="text-[12px] text-[var(--text-tertiary)] mt-0.5">
                      {formatRelativeTime(
                        session.time_updated,
                        new Date(),
                        i18n.resolvedLanguage || i18n.language || "en",
                      )}
                      {session.summary_files > 0 && (
                        <span className="ml-1.5">
                          &middot; {t("mobileFileCount", { count: session.summary_files })}
                        </span>
                      )}
                    </p>
                  </div>
                  <ChevronRight className="w-4 h-4 text-[var(--text-tertiary)] shrink-0" aria-hidden="true" />
                </button>
              ))}
            </div>
          )}
        </div>
      </PullToRefresh>
    </main>
  );
}
