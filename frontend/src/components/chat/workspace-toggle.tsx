"use client";

import { useCallback, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import { FolderOpen, X } from "lucide-react";
import { Loader2 } from "lucide-react";
import { useSettingsStore } from "@/stores/settings-store";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import { browseDirectory } from "@/lib/upload";
import { isRemoteMode } from "@/lib/remote-connection";
import { MobileDirectoryBrowser } from "@/components/mobile/directory-browser";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface WorkspaceToggleProps {
  /** When provided, workspace changes are persisted to this session via PATCH. */
  sessionId?: string;
  /** The session's current directory (used when sessionId is provided). */
  directory?: string | null;
  /** Whether the workspace is currently being indexed. Shows spinner when true. */
  isIndexing?: boolean;
  /** Keeps directory dialog state mounted while its menu closes. */
  children?: (workspace: {
    currentPath: string | null | undefined;
    browseLabel: string;
    isIndexing: boolean;
    browse: () => Promise<void>;
    clear: () => Promise<void>;
  }) => ReactNode;
}

function getDisplayName(path: string | null | undefined): string | null {
  if (!path || path === ".") return null;
  const normalized = path.replace(/\\/g, "/").replace(/\/$/, "");
  const parts = normalized.split("/");
  return parts[parts.length - 1] || null;
}

export function WorkspaceToggle({ sessionId, directory, isIndexing, children }: WorkspaceToggleProps) {
  const { t } = useTranslation("chat");
  const queryClient = useQueryClient();
  const [browsingDirs, setBrowsingDirs] = useState(false);
  const remote = isRemoteMode();

  // For new chats (no sessionId), use global settings store
  const globalWorkspace = useSettingsStore((s) => s.workspaceDirectory);
  const setGlobalWorkspace = useSettingsStore((s) => s.setWorkspaceDirectory);

  // Resolved values depending on context
  const currentPath = sessionId ? directory : globalWorkspace;
  const displayName = getDisplayName(currentPath);
  const browseLabel = displayName ? `${t("workspaceSet")}: ${currentPath}` : t("workspaceSet");

  // Handle directory selection from either native picker (desktop) or mobile browser
  const applySelectedPath = useCallback(async (path: string) => {
    if (sessionId) {
      await api.patch(API.SESSIONS.DETAIL(sessionId), { directory: path });
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions.detail(sessionId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
    } else {
      setGlobalWorkspace(path);
    }
  }, [sessionId, queryClient, setGlobalWorkspace]);

  const handleBrowse = useCallback(async () => {
    if (remote) {
      // Remote mode: use directory browser instead of native OS dialog
      setBrowsingDirs(true);
      return;
    }
    try {
      const path = await browseDirectory(t("workspaceSet"), currentPath);
      if (path) {
        await applySelectedPath(path);
      }
    } catch (err) {
      console.error("Failed to browse directory:", err);
      toast.error("Failed to open folder picker");
    }
  }, [remote, t, applySelectedPath, currentPath]);

  const handleClear = useCallback(async () => {
    if (sessionId) {
      await api.patch(API.SESSIONS.DETAIL(sessionId), { directory: "." });
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions.detail(sessionId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.sessions.all });
    } else {
      setGlobalWorkspace(null);
    }
  }, [sessionId, queryClient, setGlobalWorkspace]);

  return (
    <TooltipProvider delayDuration={150}>
      {children ? children({ currentPath, browseLabel, isIndexing: !!isIndexing, browse: handleBrowse, clear: handleClear }) : displayName ? (
        <div
          className="inline-flex max-w-full items-center rounded-xl text-[var(--text-secondary)] sm:max-w-[280px]"
        >
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={handleBrowse}
                aria-label={browseLabel}
                aria-busy={isIndexing || undefined}
                data-testid="workspace-select"
                className="inline-flex min-h-11 min-w-0 items-center gap-2 rounded-xl px-3 py-1.5 text-[13px] transition-colors hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)]"
              >
                {isIndexing ? (
                  <Loader2 className="h-4 w-4 shrink-0 animate-spin" aria-hidden="true" />
                ) : (
                  <FolderOpen className="h-4 w-4 shrink-0" aria-hidden="true" />
                )}
                <span className="truncate">{displayName}</span>
              </button>
            </TooltipTrigger>
            <TooltipContent>{browseLabel}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={handleClear}
                aria-label={t("workspaceClear")}
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[var(--text-tertiary)] transition-colors hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)]"
              >
                <X className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            </TooltipTrigger>
            <TooltipContent>{t("workspaceClear")}</TooltipContent>
          </Tooltip>
        </div>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={handleBrowse}
              aria-label={browseLabel}
              data-testid="workspace-select"
              className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)]"
            >
              <FolderOpen className="h-4 w-4" aria-hidden="true" />
            </button>
          </TooltipTrigger>
          <TooltipContent>{browseLabel}</TooltipContent>
        </Tooltip>
      )}
      {remote && (
        <MobileDirectoryBrowser
          open={browsingDirs}
          onClose={() => setBrowsingDirs(false)}
          onSelect={(path) => void applySelectedPath(path)}
          initialPath={currentPath}
        />
      )}
    </TooltipProvider>
  );
}
