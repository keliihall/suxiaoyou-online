"use client";

import { useState } from "react";
import {
  CalendarCheck,
  Clock,
  FolderSync,
  GitPullRequest,
  Loader2,
  Mail,
  Plus,
  Repeat,
  Sunrise,
  Timer,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import {
  useAutomations,
  useTemplates,
  useCreateFromTemplate,
} from "@/hooks/use-automations";
import { humanizeSchedule } from "./helpers";
import { AutomationCard } from "./automation-card";
import { CreateAutomationDialog, EditAutomationDialog } from "./automation-dialogs";
import { AutomationReadOnlyNotice } from "./shared-ui";
import type { AutomationResponse, ScheduleConfig } from "@/types/automation";

/* ------------------------------------------------------------------ */
/* Constants                                                           */
/* ------------------------------------------------------------------ */

type Tab = "active" | "all" | "templates";

const ICON_MAP: Record<string, React.ElementType> = {
  Sunrise, CalendarCheck, Mail, GitPullRequest, FolderSync, Timer, Clock, Repeat,
};

/* ------------------------------------------------------------------ */
/* Tab content (embedded in Settings)                                  */
/* ------------------------------------------------------------------ */

export function AutomationsTabContent() {
  const { t } = useTranslation("automations");
  const [tab, setTab] = useState<Tab>("active");
  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  return (
    <div className="page-content space-y-6">
      <AutomationReadOnlyNotice />
      {/* Action bar */}
      <div className="page-toolbar flex justify-end">
        <Button
          size="sm"
          className="min-h-10 gap-2 rounded-xl px-4 text-[13px]"
          onClick={() => setShowCreate(true)}
        >
          <Plus className="h-4 w-4" />
          {t("createNew")}
        </Button>
      </div>

      {/* Tabs */}
      <div className="segmented-nav inline-flex max-w-full items-center gap-1 overflow-x-auto rounded-xl bg-[var(--surface-secondary)] p-1">
        {(["active", "all", "templates"] as Tab[]).map((tabKey) => (
          <button
            key={tabKey}
            onClick={() => setTab(tabKey)}
            aria-pressed={tab === tabKey}
            className={`min-h-10 whitespace-nowrap rounded-lg px-4 text-[13px] font-medium transition-colors ${
              tab === tabKey
                ? "bg-[var(--surface-primary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]"
                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
            }`}
          >
            {t(tabKey)}
          </button>
        ))}
      </div>

      {tab === "templates" ? <TemplatesTab onCreated={() => setTab("active")} /> : <AutomationsList filter={tab === "active" ? "enabled" : "all"} onEdit={setEditingId} />}

      {showCreate && <CreateAutomationDialog onClose={() => setShowCreate(false)} />}
      {editingId && <EditAutomationDialog automationId={editingId} onClose={() => setEditingId(null)} />}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Automations list                                                    */
/* ------------------------------------------------------------------ */

function AutomationsList({ filter, onEdit }: { filter: "enabled" | "all"; onEdit: (id: string) => void }) {
  const { t } = useTranslation("automations");
  const { data: automations, isLoading } = useAutomations();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
      </div>
    );
  }

  const items = (automations || []).filter((a: AutomationResponse) => filter === "all" || a.enabled);

  if (items.length === 0) {
    return (
      <div className="surface-card flex min-h-56 flex-col items-center justify-center gap-4 rounded-2xl border border-dashed border-[var(--border-default)] bg-[var(--surface-primary)] p-8 text-center text-[14px] text-[var(--text-secondary)]">
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[var(--surface-secondary)]"><Timer className="h-6 w-6 text-[var(--text-tertiary)]" /></span>
        {t("noAutomations")}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {items.map((a: AutomationResponse) => <AutomationCard key={a.id} automation={a} onEdit={onEdit} />)}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Templates tab                                                       */
/* ------------------------------------------------------------------ */

function TemplatesTab({ onCreated }: { onCreated: () => void }) {
  const { t } = useTranslation("automations");
  const { data: templates, isLoading } = useTemplates();
  const createFromTemplate = useCreateFromTemplate();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--text-tertiary)]" />
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {(templates || []).map((tpl) => {
        const IconComp = ICON_MAP[tpl.icon] || Timer;
        return (
          <button
            key={tpl.id}
            onClick={() => createFromTemplate.mutate(tpl.id, { onSuccess: onCreated })}
            disabled={createFromTemplate.isPending}
            className="surface-card rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-5 text-left shadow-[var(--shadow-sm)] transition-colors hover:border-[var(--border-heavy)] hover:bg-[var(--surface-secondary)] disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-tertiary)]"
          >
            <div className="mb-4 flex items-center gap-3">
              <IconComp className="h-[18px] w-[18px] shrink-0 text-[var(--text-secondary)]" />
              <span className="text-sm font-medium text-[var(--text-primary)]">{tpl.name}</span>
            </div>
            {/* Auxiliary template description retained in source: <p>{tpl.description}</p> */}
            <div className="flex items-center gap-2 text-[12px] text-[var(--text-secondary)]">
              {tpl.loop_max_iterations ? (
                <>
                  <Repeat className="h-3 w-3" />
                  <span>{t("loopIterations", { n: tpl.loop_max_iterations })}</span>
                </>
              ) : (
                <>
                  <Clock className="h-3 w-3" />
                  <span>{tpl.schedule_config ? humanizeSchedule(tpl.schedule_config as ScheduleConfig, t) : "—"}</span>
                </>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}
