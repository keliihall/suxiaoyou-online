"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { FolderOpen, ChevronRight, ArrowLeft, Check, Home, Loader2, FolderPlus } from "lucide-react";
import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import { useTranslation } from "react-i18next";
import { IS_CLOUD } from "@/lib/cloud-session";
import { Dialog, DialogOverlay, DialogPortal, DialogTitle } from "@/components/ui/dialog";

interface DirectoryEntry {
  name: string;
  path: string;
}

interface ListDirectoryResponse {
  path: string;
  parent: string | null;
  dirs: DirectoryEntry[];
}

interface MobileDirectoryBrowserProps {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string) => void;
  initialPath?: string | null;
  title?: string;
}

export function MobileDirectoryBrowser({
  open,
  onClose,
  onSelect,
  initialPath,
  title,
}: MobileDirectoryBrowserProps) {
  const { t } = useTranslation("common");
  const [currentPath, setCurrentPath] = useState<string>("");
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [dirs, setDirs] = useState<DirectoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  const loadDirectory = useCallback(async (path?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.post<ListDirectoryResponse>(IS_CLOUD ? "/api/cloud-runtime/files/directories" : API.FILES.LIST_DIRECTORY, {
        path: path || null,
      });
      setCurrentPath(res.path);
      setParentPath(res.parent);
      setDirs(res.dirs);
    } catch (err) {
      console.error("Failed to list directory:", err);
      setError(IS_CLOUD ? "无法打开这个云端文件夹。" : "Unable to open directory.");
    } finally {
      setLoading(false);
    }
  }, []);

  const createDirectory = async () => {
    if (!newName.trim() || creating) return;
    setCreating(true);
    setError(null);
    try {
      const result = await api.post<{ path: string }>("/api/cloud-runtime/files/directories/create", {
        parent: currentPath || null, name: newName.trim(),
      });
      setNewName("");
      await loadDirectory(result.path);
    } catch {
      setError("无法创建文件夹，请检查名称是否有效或已经存在。");
    } finally {
      setCreating(false);
    }
  };

  useEffect(() => {
    if (open) {
      loadDirectory(initialPath);
    }
  }, [open, initialPath, loadDirectory]);

  const handleSelect = useCallback(() => {
    onSelect(currentPath);
    onClose();
  }, [currentPath, onSelect, onClose]);

  // Derive display path — show ~ for home prefix
  const displayPath = IS_CLOUD
    ? currentPath.replace(/^\/tenant\/workspaces/, "云端工作区")
    : currentPath.replace(/^\/Users\/[^/]+/, "~").replace(/^\/home\/[^/]+/, "~");

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!nextOpen) onClose(); }}>
      <DialogPortal>
        <DialogOverlay />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed inset-0 z-50 flex h-dvh min-h-0 flex-col bg-[var(--surface-secondary)] text-[var(--text-primary)] outline-none"
          onOpenAutoFocus={() => {
            previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            previousFocusRef.current?.focus();
          }}
        >
          {/* Header */}
          <header className="flex shrink-0 items-center gap-2 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 pb-3 pt-[max(env(safe-area-inset-top),16px)]">
            <button
              onClick={onClose}
              className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--surface-secondary)]"
              aria-label={t("settings:back")}
            >
              <ArrowLeft className="w-5 h-5" />
            </button>
            <DialogTitle className="min-w-0 flex-1 text-base font-semibold leading-snug tracking-tight">{title || t("mobileSelectWorkspace")}</DialogTitle>
            <button
              onClick={handleSelect}
              disabled={loading || !currentPath || Boolean(error)}
              className="flex min-h-11 shrink-0 items-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--brand-primary)] px-4 text-sm font-medium text-[var(--brand-primary-text)] transition-colors hover:bg-[var(--brand-primary-hover)] disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)] disabled:cursor-not-allowed"
            >
              <Check className="w-4 h-4" />
              {t("mobileSelect")}
            </button>
          </header>

          {/* Current path display */}
          <div className="shrink-0 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 py-3">
            <p className="text-[13px] text-[var(--text-secondary)] break-all font-mono">
              {displayPath}
            </p>
          </div>

          {IS_CLOUD && <form className="flex shrink-0 flex-wrap items-center gap-2 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 py-3" onSubmit={event => { event.preventDefault(); void createDirectory(); }} aria-busy={creating}>
            <input value={newName} onChange={event => setNewName(event.target.value)} maxLength={180} placeholder="新文件夹名称" aria-label="新文件夹名称" className="min-h-11 min-w-0 flex-1 rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-secondary)] px-3 py-2 text-[16px]" />
            <button type="submit" disabled={!newName.trim() || creating || loading} className="flex min-h-11 shrink-0 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-secondary)] px-3 py-2 text-sm font-medium disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)] disabled:cursor-not-allowed"><FolderPlus className="h-4 w-4" aria-hidden="true" />{creating ? "创建中…" : "新建文件夹"}</button>
          </form>}
          {error && <p role="alert" className="px-4 py-2 text-sm text-[var(--color-destructive)]">{error}</p>}

          {/* Navigation buttons */}
          <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 py-2">
            {parentPath && (
              <button
                onClick={() => loadDirectory(parentPath)}
                className="flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] px-3 py-2 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-secondary)]"
              >
                <ArrowLeft className="w-3.5 h-3.5" />
                {t("mobileUp")}
              </button>
            )}
            <button
              onClick={() => loadDirectory(null)}
              className="flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] px-3 py-2 text-sm font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-secondary)]"
            >
              <Home className="w-3.5 h-3.5" />
              {t("mobileHome")}
            </button>
          </div>

          {/* Directory list */}
          <div className="min-h-0 flex-1 overflow-y-auto" aria-busy={loading}>
            {loading ? (
              <div role="status" aria-label={t("mobileLoadingModels")} className="flex items-center justify-center pt-16">
                <Loader2 className="w-5 h-5 animate-spin text-[var(--text-tertiary)]" aria-hidden="true" />
              </div>
            ) : dirs.length === 0 ? (
              <div className="flex flex-col items-center justify-center pt-16 gap-2">
                <FolderOpen className="w-8 h-8 text-[var(--text-tertiary)]" />
                <p className="text-sm text-[var(--text-tertiary)]">{t("mobileNoSubdirectories")}</p>
              </div>
            ) : (
              <div className="mx-auto w-full max-w-3xl space-y-2 p-4">
                {dirs.map((dir) => (
                  <button
                    key={dir.path}
                    onClick={() => loadDirectory(dir.path)}
                    className="surface-card flex min-h-14 w-full items-center gap-3 px-4 py-3 transition-colors hover:bg-[var(--brand-soft)] active:bg-[var(--surface-tertiary)]"
                  >
                    <FolderOpen className="w-5 h-5 text-[var(--text-tertiary)] shrink-0" />
                    <span className="flex-1 text-[15px] text-[var(--text-primary)] text-left truncate">
                      {dir.name}
                    </span>
                    <ChevronRight className="w-4 h-4 text-[var(--text-tertiary)] shrink-0" />
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Bottom safe area */}
          <div className="h-[env(safe-area-inset-bottom)] shrink-0" />
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
}
