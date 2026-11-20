"use client";

import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Check, FolderOpen, FolderUp, GitBranch, ListChecks, Loader2, MessageCircleQuestion, MoreHorizontal, X, Zap, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSettingsStore } from "@/stores/settings-store";
import { WorkspaceToggle } from "./workspace-toggle";
import { cn } from "@/lib/utils";

export interface ComposerStarter {
  key: string;
  icon: LucideIcon;
  label: string;
  prompt: string;
}

interface ComposerOverflowProps {
  triggerRef: RefObject<HTMLButtonElement | null>;
  disabled: boolean;
  batchDisabled: boolean;
  onOpenBatch?: () => void;
  onUploadFolder?: () => void;
  onSelectStarter: (prompt: string) => void;
  starters?: ComposerStarter[];
  sessionId?: string;
  directory?: string | null;
  isIndexing?: boolean;
  context?: ReactNode;
}

export function ComposerOverflow({ triggerRef, disabled, batchDisabled, onOpenBatch, onUploadFolder, onSelectStarter, starters = [], sessionId, directory, isIndexing, context }: ComposerOverflowProps) {
  const { t } = useTranslation(["chat", "common"]);
  const [open, setOpen] = useState(false);
  const afterCloseRef = useRef<(() => void) | null>(null);
  const workMode = useSettingsStore(state => state.workMode);
  const setWorkMode = useSettingsStore(state => state.setWorkMode);
  const modes = [
    { key: "plan" as const, icon: ListChecks, label: t("modePlan") },
    { key: "ask" as const, icon: MessageCircleQuestion, label: t("modeAsk") },
    { key: "auto" as const, icon: Zap, label: t("modeAuto") },
  ];
  const activeMode = modes.find(mode => mode.key === workMode) ?? modes[2];
  const moreLabel = t("common:moreActions");
  const rowClass = "flex min-h-11 w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-[13px] text-[var(--text-primary)] transition-colors hover:bg-[var(--surface-secondary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:text-[var(--text-tertiary)]";

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const runAfterClose = (action: () => void) => {
    afterCloseRef.current = action;
    setOpen(false);
  };

  return (
    <WorkspaceToggle sessionId={sessionId} directory={directory} isIndexing={isIndexing}>
      {workspace => (
        <Popover open={open} onOpenChange={setOpen}>
          <Tooltip>
            <TooltipTrigger asChild>
              <PopoverTrigger asChild>
                <button
                  ref={triggerRef}
                  type="button"
                  data-testid="composer-more"
                  aria-label={moreLabel}
                  disabled={disabled}
                  className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)]"
                >
                  <MoreHorizontal className="h-[18px] w-[18px]" aria-hidden="true" />
                </button>
              </PopoverTrigger>
            </TooltipTrigger>
            <TooltipContent>{moreLabel}</TooltipContent>
          </Tooltip>
          <PopoverContent
            aria-label={moreLabel}
            align="start"
            side="top"
            sideOffset={8}
            collisionPadding={12}
            style={{ maxHeight: "min(80dvh, var(--radix-popover-content-available-height))" }}
            className="w-80 overflow-y-auto p-2"
            onCloseAutoFocus={event => {
              const action = afterCloseRef.current;
              afterCloseRef.current = null;
              if (action) {
                event.preventDefault();
                triggerRef.current?.focus();
                requestAnimationFrame(action);
              }
            }}
          >
            {starters.length > 0 && <div data-testid="landing-starters" className="mb-2 border-b border-[var(--border-default)] pb-2">
              {starters.map(starter => (
                <button
                  key={starter.key}
                  type="button"
                  data-testid={`starter-${starter.key}`}
                  onClick={() => runAfterClose(() => onSelectStarter(starter.prompt))}
                  className={rowClass}
                >
                  <starter.icon className="h-4 w-4 shrink-0 text-[var(--text-secondary)]" aria-hidden="true" />
                  <span>{starter.label}</span>
                </button>
              ))}
            </div>}

            <div className="px-3 pt-1 text-xs text-[var(--text-secondary)]">
              {t("actionModeLabel")} · <span className="text-[var(--text-primary)]">{activeMode.label}</span>
            </div>
            <div className="my-2 flex gap-1 px-1">
              {modes.map(mode => (
                <Tooltip key={mode.key}>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      aria-label={mode.label}
                      aria-pressed={workMode === mode.key}
                      onClick={() => { setWorkMode(mode.key); setOpen(false); }}
                      className={cn("relative flex h-11 min-w-11 flex-1 items-center justify-center rounded-lg text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-secondary)]", workMode === mode.key && "bg-[var(--surface-secondary)] text-[var(--text-primary)]")}
                    >
                      <mode.icon className="h-[18px] w-[18px]" aria-hidden="true" />
                      {workMode === mode.key && <Check className="absolute bottom-1 right-1 h-2.5 w-2.5" aria-hidden="true" />}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>{mode.label}</TooltipContent>
                </Tooltip>
              ))}
            </div>

            {onUploadFolder && <button type="button" className={rowClass} onClick={() => { onUploadFolder(); setOpen(false); }}>
              <FolderUp className="h-4 w-4 shrink-0 text-[var(--text-secondary)]" aria-hidden="true" />
              <span>上传文件夹</span>
            </button>}
            {onOpenBatch && <button type="button" disabled={batchDisabled} className={rowClass} onClick={() => runAfterClose(onOpenBatch)}>
              <GitBranch className="h-4 w-4 shrink-0 text-[var(--text-secondary)]" aria-hidden="true" />
              <span>{t("taskBatchButton")}</span>
            </button>}

            <div className="mt-2 border-t border-[var(--border-default)] pt-2">
              <div className="px-3 py-1 text-xs text-[var(--text-secondary)]">{t("workspaceCurrent")}</div>
              <div className="flex min-w-0 items-start">
                <button type="button" data-testid="workspace-select" aria-label={workspace.browseLabel} aria-busy={workspace.isIndexing || undefined} className={cn(rowClass, "min-w-0 flex-1")} onClick={() => runAfterClose(() => void workspace.browse())}>
                  {workspace.isIndexing ? <Loader2 className="h-4 w-4 shrink-0 animate-spin text-[var(--text-secondary)]" aria-hidden="true" /> : <FolderOpen className="h-4 w-4 shrink-0 text-[var(--text-secondary)]" aria-hidden="true" />}
                  <span className="min-w-0 break-all">{workspace.currentPath && workspace.currentPath !== "." ? workspace.currentPath : t("workspaceNone")}</span>
                </button>
                {workspace.currentPath && workspace.currentPath !== "." && <Tooltip>
                  <TooltipTrigger asChild>
                    <button type="button" aria-label={t("workspaceClear")} className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)]" onClick={() => runAfterClose(() => void workspace.clear())}>
                      <X className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>{t("workspaceClear")}</TooltipContent>
                </Tooltip>}
              </div>
            </div>
            {context && <div className="mt-2 flex items-center justify-between border-t border-[var(--border-default)] px-3 pt-2 text-xs text-[var(--text-secondary)] [&_button]:min-h-11 [&_button]:min-w-11">
              <span>{t("contextUsage")}</span>{context}
            </div>}
          </PopoverContent>
        </Popover>
      )}
    </WorkspaceToggle>
  );
}
