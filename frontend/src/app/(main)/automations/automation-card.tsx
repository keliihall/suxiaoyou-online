"use client";

import { useState } from "react";
import {
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  Clock,
  FolderOpen,
  History,
  Loader2,
  Pencil,
  Play,
  Repeat,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  useUpdateAutomation,
  useDeleteAutomation,
  useRunAutomation,
} from "@/hooks/use-automations";
import { queryKeys } from "@/lib/constants";
import { getChatRoute } from "@/lib/routes";
import { humanizeSchedule, relativeTime, formatTime } from "./helpers";
import { StatusBadge, RunHistoryPanel, DeleteConfirmDialog } from "./shared-ui";
import type { AutomationResponse, ScheduleConfig } from "@/types/automation";

export function AutomationCard({ automation: a, onEdit }: { automation: AutomationResponse; onEdit: (id: string) => void }) {
  const { t } = useTranslation("automations");
  const toggleMut = useUpdateAutomation();
  const deleteMut = useDeleteAutomation();
  const runMut = useRunAutomation();
  const queryClient = useQueryClient();
  const [showHistory, setShowHistory] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);

  const handleRun = () => {
    runMut.mutate(a.id, {
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: queryKeys.automations.all });
        queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
      },
    });
  };

  const handleDeleteConfirm = () => {
    if (!deleteTarget) return;
    deleteMut.mutate(deleteTarget.id, { onSuccess: () => setDeleteTarget(null) });
  };

  const isRunning = (a.last_run_status?.startsWith("running") ?? false) || runMut.isPending;

  return (
    <>
      <div className="surface-card rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-5 shadow-[var(--shadow-sm)] transition-colors hover:border-[var(--border-heavy)]">
        {/* Row 1: Name + actions */}
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className="min-w-0 flex-1 basis-40 text-left" onClick={() => onEdit(a.id)}>
            <span className="text-sm font-medium text-[var(--text-primary)] hover:underline">{a.name}</span>
            {/* Auxiliary automation description retained in source: {a.description && <p>{a.description}</p>} */}
          </button>

          <Button
            variant="ghost" size="sm" className="h-10 gap-2 rounded-xl px-3 text-[13px]"
            onClick={handleRun} disabled={isRunning}
          >
            {isRunning
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <Play className="h-3.5 w-3.5" />}
            {isRunning ? t("statusRunning") : t("runNow")}
          </Button>

          <button
            type="button" className="inline-flex items-center justify-center icon-action h-10 w-10 rounded-xl text-[var(--text-tertiary)] hover:text-[var(--text-primary)] hover:bg-[var(--surface-secondary)] transition-colors"
            onClick={() => onEdit(a.id)}
            aria-label={t("editAutomation")}
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>

          <Switch
            aria-label={a.name}
            checked={a.enabled}
            onCheckedChange={(enabled) => toggleMut.mutate({ id: a.id, data: { enabled } })}
          />

          <button
            type="button"
            disabled={deleteMut.isPending}
            className={`inline-flex items-center justify-center icon-action h-10 w-10 rounded-xl transition-colors ${
              deleteMut.isPending
                ? "cursor-not-allowed bg-[var(--surface-secondary)] text-[var(--text-tertiary)]"
                : "text-[var(--text-tertiary)] hover:text-[var(--color-destructive)] hover:bg-[var(--surface-secondary)]"
            }`}
            onClick={() => setDeleteTarget({ id: a.id, name: a.name })}
            aria-label={t("delete")}
          >
            {deleteMut.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
          </button>
        </div>

        {/* Row 2: Schedule + status meta */}
        <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12px] text-[var(--text-secondary)]">
          <span className="inline-flex items-center gap-1">
            {a.loop_max_iterations ? (
              <>
                <Repeat className="h-3 w-3" />
                {t("loopIterations", { n: a.loop_max_iterations })}
              </>
            ) : (
              <>
                <Clock className="h-3 w-3" />
                {a.schedule_config ? humanizeSchedule(a.schedule_config as ScheduleConfig, t) : "—"}
              </>
            )}
          </span>

          {a.workspace && (
            <span className="inline-flex items-center gap-1 truncate max-w-[180px]" title={a.workspace}>
              <FolderOpen className="h-3 w-3 shrink-0" />
              {a.workspace.replace(/\\/g, "/").split("/").pop()}
            </span>
          )}

          {a.next_run_at && !isRunning && (
            <span>{t("nextRun")}: {formatTime(a.next_run_at)}</span>
          )}

          {a.run_count > 0 && (
            <span>{t("runCount")} {a.run_count} {t("times")}</span>
          )}

          {a.run_count > 0 && (
            <button
              type="button"
              onClick={() => setShowHistory(!showHistory)}
              className="inline-flex items-center gap-0.5 ml-auto text-[var(--text-tertiary)] hover:text-[var(--text-primary)]"
            >
              <History className="h-3 w-3" />
              {t("history")}
              {showHistory ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            </button>
          )}
        </div>

        {/* Row 3: Last run result */}
        {a.last_run_at && (
          <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-[var(--border-subtle)] pt-4">
            <StatusBadge status={a.last_run_status} sessionId={a.last_session_id} t={t} />
            <span className="text-[12px] text-[var(--text-tertiary)]">
              {relativeTime(a.last_run_at, t)}
            </span>
            {a.last_session_id && a.last_run_status !== "running" && (
              <Link
                href={getChatRoute(a.last_session_id)}
                className="ml-auto text-[12px] text-[var(--text-tertiary)] hover:text-[var(--text-primary)] inline-flex items-center gap-0.5"
              >
                {t("viewResult")}
                <ArrowUpRight className="h-3 w-3" />
              </Link>
            )}
          </div>
        )}

        {/* Row 4: Expandable run history */}
        {showHistory && (
          <div className="mt-2 pt-2 border-t border-[var(--border-default)]/50">
            <RunHistoryPanel automationId={a.id} t={t} />
          </div>
        )}
      </div>

      {/* Delete confirmation */}
      {deleteTarget && (
        <DeleteConfirmDialog
          name={deleteTarget.name}
          onConfirm={handleDeleteConfirm}
          onCancel={() => setDeleteTarget(null)}
          isPending={deleteMut.isPending}
          t={t}
        />
      )}
    </>
  );
}
