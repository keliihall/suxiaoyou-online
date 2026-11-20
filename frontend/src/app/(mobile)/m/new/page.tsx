"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ArrowUp, Loader2, Paperclip, Camera, X, ChevronDown, FolderOpen } from "lucide-react";
import { toast } from "sonner";
import { api, apiFetch, ApiError } from "@/lib/api";
import { API } from "@/lib/constants";
import { isRemoteMode, getRemoteConfig } from "@/lib/remote-connection";
import { useProviderModels } from "@/hooks/use-provider-models";
import { useSettingsStore } from "@/stores/settings-store";
import { MobileDirectoryBrowser } from "@/components/mobile/directory-browser";
import {
  clearPromptRequestId,
  promptRequestFingerprint,
  reservePromptRequestId,
  uploadSelectionFingerprint,
} from "@/lib/prompt-idempotency";
import type { FileAttachment, PromptRequest } from "@/types/chat";
import { IS_CLOUD } from "@/lib/cloud-session";

/**
 * Mobile new task page.
 *
 * Model list comes from useProviderModels() (reads Zustand activeProvider).
 * Selected model comes from useSettingsStore.selectedModel.
 * Both are synced by the mobile layout's useRemoteProviderSync().
 * No local provider/model state — single source of truth.
 */
export default function MobileNewTaskPage() {
  const router = useRouter();
  const { t } = useTranslation("common");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const cameraInputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [browsingDirs, setBrowsingDirs] = useState(false);
  const uploadedAttachmentsRef = useRef<{
    selectionFingerprint: string;
    attachments: FileAttachment[];
  } | null>(null);

  // Workspace directory — read from settings store (persisted in localStorage)
  const workspaceDirectory = useSettingsStore((s) => s.workspaceDirectory);
  const setWorkspaceDirectory = useSettingsStore((s) => s.setWorkspaceDirectory);

  // Read model state from Zustand (synced by layout)
  const { data: models, isLoading: loadingModels } = useProviderModels();
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const setSelectedModel = useSettingsStore((s) => s.setSelectedModel);

  useEffect(() => {
    if (!IS_CLOUD && !isRemoteMode()) {
      router.replace("/m/settings");
      return;
    }
    // If models loaded but nothing selected, pick first
    if (models.length > 0 && !selectedModel) {
      setSelectedModel(models[0].id, models[0].provider_id);
    }
  }, [router, models, selectedModel, setSelectedModel]);

  const handleTextChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      uploadedAttachmentsRef.current = null;
      setFiles((prev) => [...prev, ...Array.from(e.target.files!)]);
    }
  }, []);

  const removeFile = useCallback((index: number) => {
    uploadedAttachmentsRef.current = null;
    setFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const handleSubmit = useCallback(async () => {
    if ((!text.trim() && files.length === 0) || sending) return;
    const selectedModelInfo = models.find((model) => model.id === selectedModel);
    if (files.some((file) => file.type.startsWith("image/")) && selectedModelInfo?.capabilities.vision !== true) {
      toast.error(t("mobileVisionModelRequired"));
      return;
    }
    setSending(true);
    let promptRequestId: string | null = null;

    try {
      const selectionFingerprint = uploadSelectionFingerprint(files);
      let attachments = uploadedAttachmentsRef.current?.selectionFingerprint === selectionFingerprint
        ? uploadedAttachmentsRef.current.attachments
        : null;
      const remoteConfig = getRemoteConfig();

      if (!attachments) {
        attachments = [];
        for (const file of files) {
          const formData = new FormData();
          formData.append("file", file);
          const uploadUrl = remoteConfig
            ? `${remoteConfig.url}${API.FILES.UPLOAD}`
            : API.FILES.UPLOAD;
          const result = await apiFetch(uploadUrl, {
            method: "POST",
            headers: remoteConfig
              ? { Authorization: `Bearer ${remoteConfig.token}` }
              : {},
            body: formData,
          });
          if (!result.ok) {
            throw new Error(`Attachment upload failed: ${result.status}`);
          }
          attachments.push(await result.json() as FileAttachment);
        }
        uploadedAttachmentsRef.current = {
          selectionFingerprint,
          attachments,
        };
      }

      const promptPayload: PromptRequest = {
        text: text.trim(),
        model: selectedModel || null,
        provider_id: useSettingsStore.getState().selectedProviderId || null,
        attachments,
        workspace: workspaceDirectory || null,
      };
      promptRequestId = reservePromptRequestId(
        "mobile-new",
        promptRequestFingerprint(promptPayload),
      );
      const response = await api.post<{ stream_id: string; session_id: string }>(
        API.CHAT.PROMPT,
        { ...promptPayload, client_request_id: promptRequestId },
        { retryNetworkErrors: true },
      );
      clearPromptRequestId("mobile-new", promptRequestId);
      uploadedAttachmentsRef.current = null;

      router.push(`/m/task/_?sessionId=${encodeURIComponent(response.session_id)}&stream_id=${response.stream_id}`);
    } catch (err) {
      console.error("Failed to send task:", err);
      if (
        promptRequestId
        && err instanceof ApiError
        && err.status >= 400
        && err.status < 500
      ) {
        clearPromptRequestId("mobile-new", promptRequestId);
      }
      toast.error(t("mobileSendFailed"));
      setSending(false);
    }
  }, [text, sending, files, models, selectedModel, workspaceDirectory, router, t]);

  // Current model name for display
  return (
    <main className="flex h-full min-h-0 flex-col bg-[var(--surface-secondary)]">
      {/* Header */}
      <header className="flex shrink-0 items-center gap-3 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 pb-3 pt-[max(env(safe-area-inset-top),16px)]">
        <button
          onClick={() => router.back()}
          className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--surface-secondary)]"
          aria-label={t("settings:back")}
        >
          <ArrowLeft className="w-5 h-5" />
        </button>
        <h1 className="text-lg font-semibold tracking-tight">{t("mobileNewTask")}</h1>
      </header>

      {/* Main content — push input to bottom like a chat app */}
      <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col overflow-y-auto px-4 pt-6 pb-[max(env(safe-area-inset-bottom),16px)]">
        <div className="my-auto min-h-6 flex-1" aria-hidden="true" />

        {/* Model selector — single dropdown, filtered by provider from Settings */}
        <div className="relative mb-3 shrink-0">
          <select
            aria-label={t("chat:toolMetadataModel")}
            value={selectedModel ?? ""}
            onChange={(e) => {
              const model = models.find((m) => m.id === e.target.value);
              setSelectedModel(e.target.value, model?.provider_id ?? null);
            }}
            disabled={loadingModels || models.length === 0}
            className="min-h-12 w-full appearance-none rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-primary)] py-3 pr-10 pl-4 text-[16px] font-medium text-[var(--text-primary)] disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)] disabled:cursor-not-allowed focus:outline-none focus:border-[var(--brand-primary)]"
          >
            {loadingModels ? (
              <option>{t("mobileLoadingModels")}</option>
            ) : models.length === 0 ? (
              <option>{t("mobileNoModels")}</option>
            ) : (
              models.map((m) => (
                <option key={m.id} value={m.id}>{m.name}</option>
              ))
            )}
          </select>
          <ChevronDown className="absolute right-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[var(--text-tertiary)] pointer-events-none" />
        </div>

        {/* Workspace selector */}
        <div className="mb-4 flex min-h-12 shrink-0 items-center rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-primary)]">
          <button
            type="button"
            onClick={() => setBrowsingDirs(true)}
            className="flex min-h-12 min-w-0 flex-1 items-center gap-3 rounded-[var(--radius-control)] px-4 text-left transition-colors hover:bg-[var(--surface-secondary)]"
            aria-label={`${t("mobileSelectWorkspace")}${workspaceDirectory ? `: ${workspaceDirectory}` : ""}`}
          >
            <FolderOpen className="w-4 h-4 text-[var(--text-secondary)] shrink-0" aria-hidden="true" />
            <span className={`min-w-0 flex-1 text-[15px] truncate ${workspaceDirectory ? "text-[var(--text-primary)] font-medium" : "text-[var(--text-secondary)]"}`}>
              {workspaceDirectory
                ? workspaceDirectory.replace(/^\/Users\/[^/]+/, "~").replace(/^\/home\/[^/]+/, "~")
                : t("mobileNoWorkspace")}
            </span>
          </button>
          {workspaceDirectory && (
            <button
              type="button"
              onClick={() => setWorkspaceDirectory(null)}
              className="icon-action mr-1 flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--surface-tertiary)]"
              aria-label={t("chat:workspaceClear")}
            >
              <X className="w-3.5 h-3.5 text-[var(--text-tertiary)]" />
            </button>
          )}
        </div>

        <MobileDirectoryBrowser
          open={browsingDirs}
          onClose={() => setBrowsingDirs(false)}
          onSelect={setWorkspaceDirectory}
          initialPath={workspaceDirectory}
        />

        {/* Attached files */}
        {files.length > 0 && (
          <div className="mb-3 flex shrink-0 flex-wrap gap-2">
            {files.map((file, i) => (
              <div
                key={i}
                className="flex max-w-full items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-primary)] pl-3 text-[13px]"
              >
                <span className="truncate max-w-[180px]">{file.name}</span>
                <button
                  onClick={() => removeFile(i)}
                  className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--surface-tertiary)]"
                  aria-label={`${t("settings:remove")} ${file.name}`}
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Input area */}
        <div className="shrink-0 rounded-[var(--radius-panel)] border border-[var(--border-default)] bg-[var(--surface-primary)] shadow-[var(--shadow-sm)] transition-shadow focus-within:border-[var(--brand-primary)] focus-within:shadow-[var(--shadow-md)]" aria-busy={sending}>
          <div className="px-4 pt-3 pb-2">
            <textarea
              ref={textareaRef}
              value={text}
              onChange={handleTextChange}
              placeholder={t("mobilePromptPlaceholder")}
              aria-label={t("mobilePromptPlaceholder")}
              rows={1}
              className="w-full resize-none bg-transparent text-[16px] leading-relaxed outline-none placeholder:text-[var(--text-tertiary)] min-h-11 max-h-[200px]"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  handleSubmit();
                }
              }}
            />
          </div>

          <div className="flex items-center justify-between gap-2 px-3 pb-2.5">
            <div className="flex items-center gap-1">
              <button type="button" onClick={() => cameraInputRef.current?.click()} aria-label={t("settings:mobileOpenCamera")} className="icon-action flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] transition-colors hover:bg-[var(--surface-tertiary)]">
                <Camera className="w-[18px] h-[18px] text-[var(--text-tertiary)]" />
              </button>
              <input ref={cameraInputRef} aria-label={t("settings:mobileOpenCamera")} type="file" accept="image/*" capture="environment" onChange={handleFileSelect} className="hidden" />
              <button type="button" onClick={() => fileInputRef.current?.click()} aria-label={t("chat:attachFile")} className="icon-action flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] transition-colors hover:bg-[var(--surface-tertiary)]">
                <Paperclip className="w-[18px] h-[18px] text-[var(--text-tertiary)]" />
              </button>
              <input ref={fileInputRef} aria-label={t("chat:attachFile")} type="file" multiple onChange={handleFileSelect} className="hidden" />
            </div>

            <button
              onClick={handleSubmit}
              disabled={(!text.trim() && files.length === 0) || sending}
              aria-label={sending ? t("chat:uploading") : t("chat:sendAction")}
              className="flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] bg-[var(--brand-primary)] text-[var(--brand-primary-text)] transition-colors hover:bg-[var(--brand-primary-hover)] disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)] disabled:cursor-not-allowed"
            >
              {sending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <ArrowUp className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>

        {/* mobileTasksRunOnDesktop: auxiliary product explanation remains in source. */}
      </div>
    </main>
  );
}
