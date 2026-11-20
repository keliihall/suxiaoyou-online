"use client";

import { useRef, useState } from "react";
import {
  ArrowUpRight,
  Check,
  Loader2,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { useAutomationRuns } from "@/hooks/use-automations";
import { getChatRoute } from "@/lib/routes";
import { humanizeCron, relativeTime, formatDuration, parseCron, buildCron } from "./helpers";
import type { TaskRunResponse } from "@/types/automation";

/* ------------------------------------------------------------------ */
/* Shared styles                                                       */
/* ------------------------------------------------------------------ */

export const inputClass = "ui-control w-full h-10 rounded-xl border border-[var(--control-border)] bg-[var(--surface-primary)] px-3 text-[13px] text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] focus:outline-none focus:ring-1 focus:ring-[var(--border-focus)]";

const selectClass = "ui-control h-10 rounded-xl border border-[var(--control-border)] bg-[var(--surface-primary)] px-3 text-[13px] text-[var(--text-primary)] focus:outline-none focus:ring-1 focus:ring-[var(--border-focus)] appearance-none cursor-pointer";

export function AutomationReadOnlyNotice({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation("automations");
  return (
    <div
      className={`surface-card flex items-center gap-3 rounded-2xl border border-[var(--border-default)] bg-[var(--surface-secondary)] ${compact ? "p-3" : "p-4"}`}
      data-testid="automation-read-only-notice"
    >
      <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-[var(--brand-primary)]" />
      <div className="min-w-0">
        <p className="text-xs font-medium text-[var(--text-primary)]">{t("readOnlyTitle")}</p>
        {/* Auxiliary copy retained in source: <p className="mt-0.5 text-[12px] leading-relaxed text-[var(--text-secondary)]">
          {t("readOnlyDescription")}
        </p> */}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Dialog overlay                                                      */
/* ------------------------------------------------------------------ */

export function DialogOverlay({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  const { t } = useTranslation("automations");
  const returnFocus = useRef<HTMLElement | null>(
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent
        aria-describedby={undefined}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocus.current?.focus();
        }}
        className="max-w-lg gap-0 p-0 [&>button:last-child]:hidden"
      >
        <DialogTitle className="sr-only">{t("title")}</DialogTitle>
        {children}
      </DialogContent>
    </Dialog>
  );
}

/* ------------------------------------------------------------------ */
/* Status badge                                                        */
/* ------------------------------------------------------------------ */

type TFunc = (key: string, opts?: Record<string, unknown>) => string;

export function StatusBadge({ status, sessionId, t }: { status: string | null; sessionId: string | null; t: TFunc }) {
  if (!status) return null;

  const config: Record<string, { icon: React.ElementType; color: string; labelKey: string }> = {
    running: { icon: Loader2, color: "text-[var(--status-warning-text)]", labelKey: "statusRunning" },
    success: { icon: Check, color: "text-[var(--status-success-text)]", labelKey: "statusSuccess" },
    error:   { icon: XCircle, color: "text-[var(--status-danger-text)]", labelKey: "statusError" },
    timeout: { icon: XCircle, color: "text-[var(--status-warning-text)]", labelKey: "statusTimeout" },
  };

  const normalizedStatus = status.startsWith("running") ? "running" : status;
  const c = config[normalizedStatus];
  if (!c) return null;
  const Icon = c.icon;
  const loopMatch = status.match(/^running:(\d+\/\d+)$/);
  const loopSuffix = loopMatch ? ` ${loopMatch[1]}` : "";

  const badge = (
    <span className={`inline-flex items-center gap-1 text-[12px] ${c.color}`}>
      <Icon className={`h-3 w-3 ${normalizedStatus === "running" ? "animate-spin" : ""}`} />
      {t(c.labelKey)}{loopSuffix}
    </span>
  );

  if (normalizedStatus !== "running" && sessionId) {
    return (
      <Link
        href={getChatRoute(sessionId)}
        className={`inline-flex items-center gap-1 text-[12px] ${c.color} hover:underline`}
      >
        <Icon className="h-3 w-3" />
        {t(c.labelKey)}
        <ArrowUpRight className="h-2.5 w-2.5" />
      </Link>
    );
  }

  return badge;
}

/* ------------------------------------------------------------------ */
/* Triggered-by badge                                                  */
/* ------------------------------------------------------------------ */

export function TriggeredByBadge({ triggeredBy, t }: { triggeredBy: string; t: (key: string) => string }) {
  const map: Record<string, { label: string; color: string }> = {
    schedule:         { label: t("triggerSchedule"), color: "bg-[var(--surface-secondary)] text-[var(--text-secondary)]" },
    manual:           { label: t("triggerManual"), color: "bg-[var(--surface-secondary)] text-[var(--text-secondary)]" },
    startup_catchup:  { label: t("triggerCatchup"), color: "bg-[var(--surface-secondary)] text-[var(--text-secondary)]" },
  };
  const info = map[triggeredBy] || { label: triggeredBy, color: "bg-[var(--surface-secondary)] text-[var(--text-secondary)]" };
  return <span className={`text-[12px] px-1.5 py-0.5 rounded ${info.color}`}>{info.label}</span>;
}

/* ------------------------------------------------------------------ */
/* Run history panel                                                   */
/* ------------------------------------------------------------------ */

export function RunHistoryPanel({ automationId, t }: { automationId: string; t: TFunc }) {
  const { i18n } = useTranslation();
  const { data: runs, isLoading } = useAutomationRuns(automationId);

  if (isLoading) {
    return <div className="flex justify-center py-4"><Loader2 className="h-4 w-4 animate-spin text-[var(--text-tertiary)]" /></div>;
  }

  if (!runs || runs.length === 0) {
    return <p className="text-xs text-[var(--text-tertiary)] py-3 text-center">{t("noRuns")}</p>;
  }

  return (
    <div className="space-y-1.5 max-h-48 overflow-y-auto">
      {runs.map((run: TaskRunResponse) => (
        <div key={run.id} className="rounded px-1 py-1.5 hover:bg-[var(--surface-secondary)]/50">
          <div className="flex items-center gap-2 text-[12px]">
            <StatusBadge status={run.status} sessionId={run.session_id} t={t} />
            <TriggeredByBadge triggeredBy={run.triggered_by} t={t} />
            <span className="text-[var(--text-tertiary)]">
              {formatDuration(run.started_at, run.finished_at, i18n.language)}
            </span>
            <span className="text-[var(--text-tertiary)] ml-auto">
              {relativeTime(run.started_at, t)}
            </span>
            {run.session_id && run.status !== "running" && (
              <Link href={getChatRoute(run.session_id)} className="text-[var(--text-tertiary)] hover:text-[var(--text-primary)]">
                <ArrowUpRight className="h-3 w-3" />
              </Link>
            )}
          </div>
          {run.error_message && (
            <p
              className="mt-1 break-words rounded-lg bg-[var(--status-danger-bg)] px-3 py-2 text-[12px] leading-relaxed text-[var(--status-danger-text)]"
              title={run.error_message}
              data-testid="automation-run-error"
            >
              {t("runErrorDetail", { error: run.error_message })}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Delete confirmation dialog                                          */
/* ------------------------------------------------------------------ */

export function DeleteConfirmDialog({ name, onConfirm, onCancel, isPending, t }: {
  name: string;
  onConfirm: () => void;
  onCancel: () => void;
  isPending: boolean;
  t: (key: string) => string;
}) {
  return (
    <DialogOverlay onClose={onCancel}>
      <div className="px-4 py-4">
        <p className="text-sm text-[var(--text-primary)]">{t("confirmDelete")}</p>
        <p className="text-xs text-[var(--text-tertiary)] mt-1">{name}</p>
      </div>
      <div className="flex justify-end gap-2 px-4 py-3 border-t border-[var(--border-default)]">
        <Button variant="ghost" size="sm" className="h-10 rounded-xl px-4 text-[13px]" onClick={onCancel}>{t("cancel")}</Button>
        <Button variant="destructive" size="sm" className="h-10 rounded-xl px-4 text-[13px]" onClick={onConfirm} disabled={isPending}>
          {isPending && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" />}
          {t("delete")}
        </Button>
      </div>
    </DialogOverlay>
  );
}

/* ------------------------------------------------------------------ */
/* Schedule config editor (shared between create & edit)               */
/* ------------------------------------------------------------------ */

const ALL_DAYS = [1, 2, 3, 4, 5, 6, 0]; // Mon-Sun display order
const DAY_KEYS = ["", "mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
function dayKey(d: number): string { return d === 0 ? "sun" : (DAY_KEYS[d] || ""); }

export function ScheduleEditor({ scheduleType, setScheduleType, cronExpr, setCronExpr, intervalHours, setIntervalHours, t }: {
  scheduleType: "cron" | "interval";
  setScheduleType: (v: "cron" | "interval") => void;
  cronExpr: string;
  setCronExpr: (v: string) => void;
  intervalHours: number;
  setIntervalHours: (v: number) => void;
  t: (key: string) => string;
}) {
  const parsed = parseCron(cronExpr);
  const [showRawCron, setShowRawCron] = useState(false);

  const updateCron = (hour: number, minute: number, dow: number[]) => {
    setCronExpr(buildCron(hour, minute, dow));
  };

  const toggleDay = (d: number) => {
    const next = parsed.dow.includes(d) ? parsed.dow.filter((x) => x !== d) : [...parsed.dow, d];
    updateCron(parsed.hour, parsed.minute, next);
  };

  const setDayPreset = (days: number[]) => updateCron(parsed.hour, parsed.minute, days);
  const isAllDays = parsed.dow.length === 7 || parsed.dow.length === 0;
  const isWeekdays = parsed.dow.length === 5 && [1, 2, 3, 4, 5].every((d) => parsed.dow.includes(d));
  const isWeekends = parsed.dow.length === 2 && parsed.dow.includes(0) && parsed.dow.includes(6);

  return (
    <div>
      <label className="text-xs font-medium text-[var(--text-secondary)] mb-1 block">{t("schedule")}</label>
      <div className="flex gap-2 mb-3">
        {(["cron", "interval"] as const).map((st) => (
          <button key={st} type="button" onClick={() => setScheduleType(st)} aria-pressed={scheduleType === st}
            className={`ui-control px-3 py-1 text-xs rounded-md border transition-colors ${
              scheduleType === st
                ? "border-[var(--text-primary)] text-[var(--text-primary)] bg-[var(--surface-secondary)]"
                : "border-[var(--border-default)] text-[var(--text-tertiary)]"
            }`}
          >
            {st === "cron" ? t("triggerSchedule") : t("interval")}
          </button>
        ))}
      </div>
      {scheduleType === "cron" ? (
        <div className="space-y-3">
          {/* Time picker */}
          <div className="flex items-center gap-2">
            <select
              aria-label={t("scheduleHour")}
              value={parsed.hour}
              onChange={(e) => updateCron(Number(e.target.value), parsed.minute, parsed.dow)}
              className={selectClass + " w-16 text-center"}
            >
              {Array.from({ length: 24 }, (_, i) => (
                <option key={i} value={i}>{String(i).padStart(2, "0")}</option>
              ))}
            </select>
            <span className="text-xs text-[var(--text-tertiary)]">:</span>
            <select
              aria-label={t("scheduleMinute")}
              value={parsed.minute}
              onChange={(e) => updateCron(parsed.hour, Number(e.target.value), parsed.dow)}
              className={selectClass + " w-16 text-center"}
            >
              {[0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].map((m) => (
                <option key={m} value={m}>{String(m).padStart(2, "0")}</option>
              ))}
            </select>
          </div>

          {/* Day-of-week presets */}
          <div className="flex gap-1.5">
            {([
              { label: t("days.everyday"), days: [0, 1, 2, 3, 4, 5, 6], active: isAllDays },
              { label: t("days.weekdays"), days: [1, 2, 3, 4, 5], active: isWeekdays },
              { label: t("days.weekends"), days: [0, 6], active: isWeekends },
            ] as const).map((p) => (
              <button
                key={p.label}
                type="button"
                aria-pressed={p.active}
                onClick={() => setDayPreset([...p.days])}
                className={`ui-control px-2 py-0.5 text-[12px] rounded border transition-colors ${
                  p.active
                    ? "border-[var(--text-primary)] text-[var(--text-primary)] bg-[var(--surface-secondary)]"
                    : "border-[var(--border-default)] text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>

          {/* Day-of-week toggles */}
          <div className="flex gap-1">
            {ALL_DAYS.map((d) => (
              <button
                key={d}
                type="button"
                aria-pressed={parsed.dow.includes(d)}
                onClick={() => toggleDay(d)}
                className={`ui-control min-w-0 flex-1 py-1.5 text-[12px] rounded-md border transition-colors ${
                  parsed.dow.includes(d)
                    ? "border-[var(--text-primary)] text-[var(--text-primary)] bg-[var(--surface-secondary)]"
                    : "border-[var(--border-default)] text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]"
                }`}
              >
                {t(`days.${dayKey(d)}`)}
              </button>
            ))}
          </div>

          {/* Summary + raw cron toggle */}
          <div className="flex items-center justify-between">
            <p className="text-[12px] text-[var(--text-tertiary)]">
              {humanizeCron(cronExpr, t)}
            </p>
            <button
              type="button"
              aria-expanded={showRawCron}
              aria-label={t("cronHelp")}
              onClick={() => setShowRawCron(!showRawCron)}
              className="ui-control px-2 text-[12px] text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors"
            >
              {showRawCron ? "—" : "cron"}
            </button>
          </div>

          {/* Raw cron (collapsed by default) */}
          {showRawCron && (
            <input type="text" aria-label={t("cronHelp")} value={cronExpr} onChange={(e) => setCronExpr(e.target.value)}
              placeholder="0 8 * * 1" className={`${inputClass} font-mono`} />
          )}
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <span className="text-xs text-[var(--text-secondary)]">{t("every")}</span>
          <input type="number" aria-label={`${t("interval")} (${t("hours")})`} min={1} max={168} value={intervalHours}
            onChange={(e) => setIntervalHours(Number(e.target.value) || 1)}
            className="ui-control w-16 h-10 rounded-md border border-[var(--control-border)] bg-[var(--surface-primary)] px-3 text-[13px] text-[var(--text-primary)] text-center focus:outline-none focus:ring-1 focus:ring-[var(--border-focus)]"
          />
          <span className="text-xs text-[var(--text-secondary)]">{t("hours")}</span>
        </div>
      )}
    </div>
  );
}
