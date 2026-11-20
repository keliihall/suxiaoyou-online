"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Check, ChevronDown, ChevronRight, FileText, FolderOpen, MoreHorizontal } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { useWorkspaceStore, type WorkspaceFile } from "@/stores/workspace-store";
import { useArtifactStore } from "@/stores/artifact-store";
import { useChatStore } from "@/stores/chat-store";
import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

function FileItem({ file }: { file: WorkspaceFile }) {
  const { t } = useTranslation("chat");
  const handleClick = () => {
    const store = useArtifactStore.getState();
    // Match by filePath first, then fall back to matching by title (for artifacts
    // created by the artifact tool which don't have filePath set yet)
    const baseName = file.name.replace(/\.[^.]+$/, "");
    const existing = store.artifacts.find(
      (a) => a.filePath === file.path || (!a.filePath && a.title === baseName),
    );
    if (existing) {
      // Re-open with filePath set so future lookups match directly
      store.openArtifact({ ...existing, filePath: file.path });
      return;
    }
    store.openArtifact({
      id: `workspace-${file.path}`,
      type: "file-preview",
      title: file.name,
      content: "",
      filePath: file.path,
    });
  };

  const displayName =
    file.type === "instructions" ? t("instructionsFile", { name: file.name }) : file.name;

  return (
    <button
      className="w-full flex min-h-9 items-center gap-2.5 px-4 py-2 text-left transition-colors hover:bg-[var(--surface-secondary)]"
      onClick={handleClick}
    >
      <FileText className="h-4 w-4 shrink-0 text-[var(--text-tertiary)]" />
      <span className="text-[13px] text-[var(--text-secondary)] truncate">
        {displayName}
      </span>
    </button>
  );
}

function Scratchpad() {
  const { t } = useTranslation("chat");
  const content = useWorkspaceStore((s) => s.scratchpadContent);
  const setContent = useWorkspaceStore((s) => s.setScratchpadContent);
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="mx-4 mb-3">
      <button
        className={cn(
          "flex items-center gap-1.5 w-full px-3 py-2 rounded-lg text-left transition-colors",
          "border",
          expanded
            ? "border-[var(--border-focus)] bg-[var(--surface-primary)]"
            : "border-[var(--border-default)] hover:border-[var(--text-tertiary)]",
        )}
        onClick={() => setExpanded(!expanded)}
      >
        {expanded ? (
          <ChevronDown className="h-3 w-3 text-[var(--text-tertiary)]" />
        ) : (
          <ChevronRight className="h-3 w-3 text-[var(--text-tertiary)]" />
        )}
        <span className="text-[13px] text-[var(--text-tertiary)]">
          {t("scratchpad")}
        </span>
      </button>
      {expanded && (
        <textarea
          className={cn(
            "w-full mt-1.5 px-3 py-2 text-[13px] leading-relaxed rounded-lg resize-none",
            "bg-[var(--surface-primary)] text-[var(--text-primary)]",
            "placeholder:text-[var(--text-tertiary)]",
            "border border-[var(--border-focus)] focus:outline-none",
            "min-h-[80px]",
          )}
          placeholder={t("scratchpadPlaceholder")}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          autoFocus
        />
      )}
    </div>
  );
}

export function FilesCard() {
  const { t } = useTranslation("chat");
  const workspaceFiles = useWorkspaceStore((s) => s.workspaceFiles);
  const fileScope = useWorkspaceStore((s) => s.fileScope);
  const sessionId = useChatStore((s) => s.focusedSessionId);
  const [loadingFiles, setLoadingFiles] = useState(false);
  const collapsed = useWorkspaceStore((s) => s.collapsedSections["files"]);
  const toggleSection = useWorkspaceStore((s) => s.toggleSection);
  const toggleWorkingFiles = async () => {
    if (!sessionId || loadingFiles) return;
    const scope = fileScope === "all" ? "deliverables" : "all";
    const store = useWorkspaceStore.getState();
    store.setFileScope(scope);
    const revision = store.beginFileRequest();
    setLoadingFiles(true);
    try {
      const res = await api.get<{ files: WorkspaceFile[] }>(`${API.SESSIONS.FILES(sessionId)}?scope=${scope}`);
      if (useChatStore.getState().focusedSessionId !== sessionId || useWorkspaceStore.getState().fileScope !== scope) return;
      useWorkspaceStore.getState().commitFileRequest(revision, res.files ?? []);
    } catch {
      if (useChatStore.getState().focusedSessionId === sessionId && useWorkspaceStore.getState().fileRequestRevision === revision) {
        toast.error(t("failedLoadFile"));
      }
    } finally {
      setLoadingFiles(false);
    }
  };

  return (
    <div className="surface-card overflow-hidden">
      <div className="flex items-center pr-3">
      <button
        className="flex min-w-0 flex-1 items-center justify-between px-4 py-4 text-left transition-colors hover:bg-[var(--surface-secondary)]"
        onClick={() => toggleSection("files")}
      >
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-2xl border border-[var(--border-default)] bg-[var(--surface-secondary)]">
            <FolderOpen className="h-4 w-4 text-[var(--text-tertiary)]" />
          </div>
          <div className="min-w-0">
            <span className="block text-[13px] font-medium text-[var(--text-primary)]">
              {t("files")}
            </span>
          </div>
        </div>
        <div className="ml-3 flex items-center gap-2">
          {workspaceFiles.length > 0 && (
            <span className="rounded-full border border-[var(--border-default)] bg-[var(--surface-secondary)] px-2 py-0.5 text-[10px] font-medium text-[var(--text-tertiary)]">
              {workspaceFiles.length}
            </span>
          )}
          <ChevronDown
            className={cn(
              "h-4 w-4 text-[var(--text-tertiary)] transition-transform duration-200",
              collapsed && "-rotate-90",
            )}
          />
        </div>
      </button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--text-tertiary)] transition-colors hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)]"
            aria-label={t("fileOptions")}
            title={t("fileOptions")}
            disabled={!sessionId || loadingFiles}
          >
            <MoreHorizontal className="h-4 w-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => void toggleWorkingFiles()}>
            <Check className={cn("h-4 w-4", fileScope !== "all" && "invisible")} />
            {t("showWorkingFiles")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      </div>
      <AnimatePresence initial={false}>
        {!collapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: "spring", damping: 25, stiffness: 300 }}
            className="overflow-hidden"
          >
            <div className="border-t border-[var(--border-subtle)] pb-1 pt-2">
              {workspaceFiles.length > 0 ? (
                <div className="space-y-0.5">
                  {workspaceFiles.map((file) => (
                    <FileItem key={file.path} file={file} />
                  ))}
                </div>
              ) : null}
              <div className="mt-2">
                <Scratchpad />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
