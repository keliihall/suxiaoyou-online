"use client";

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  Mail, FileDiff, CalendarDays, Settings,
  Receipt, FolderOpen, Trash2,
} from "lucide-react";
import Link from "next/link";
import { useTranslation } from 'react-i18next';
import { ChatForm } from "./chat-form";
import { ChatHeader } from "./chat-header";
import { OfflineOverlay } from "@/components/layout/offline-overlay";
import { StreamingMessage } from "@/components/messages/assistant-message";
import { FileChip } from "./file-chip";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useChat } from "@/hooks/use-chat";
import { useChatStore } from "@/stores/chat-store";
import { useArtifactStore } from "@/stores/artifact-store";
import { useActivityStore } from "@/stores/activity-store";
import { useSettingsStore } from "@/stores/settings-store";
import { isInteractionAwaitingResolution } from "@/lib/interaction-response";
import { IS_CLOUD } from "@/lib/cloud-session";

const FEATURED_STARTERS = [
  { icon: Mail, textKey: "starterDraftFromNotes", promptKey: "starterDraftFromNotesPrompt" },
  { icon: FileDiff, textKey: "starterCompareDocs", promptKey: "starterCompareDocsPrompt" },
  { icon: CalendarDays, textKey: "starterWeeklyDigest", promptKey: "starterWeeklyDigestPrompt" },
  { icon: Receipt, textKey: "starterOrganizeBills", promptKey: "starterOrganizeBillsPrompt" },
  { icon: FolderOpen, textKey: "starterSummarizeFolder", promptKey: "starterSummarizeFolderPrompt" },
  { icon: Trash2, textKey: "starterCleanupFiles", promptKey: "starterCleanupFilesPrompt" },
];

const STARTERS_PER_MOUNT = 3;

interface LandingProps {
  directoryParam?: string | null;
}

function pickRandomStarters(count: number): typeof FEATURED_STARTERS {
  const pool = [...FEATURED_STARTERS];
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  return pool.slice(0, count);
}

export function Landing({ directoryParam = null }: LandingProps) {
  const { t } = useTranslation('chat');
  const {
    sendMessage,
    handleGoalCommand,
    sendTaskBatch,
    isGenerating,
    stopGeneration,
    pendingUserText,
    pendingAttachments,
    streamingParts,
    streamingText,
    streamingReasoning,
    pendingPermission,
    pendingQuestion,
    pendingPlanReview,
  } = useChat();
  const globalWorkspace = useSettingsStore((s) => s.workspaceDirectory);
  const activeProvider = useSettingsStore((s) => s.activeProvider);
  // Start with a deterministic slice so SSR and first client render match,
  // then shuffle post-mount. A new random pair is drawn every time Landing
  // remounts (i.e. every new chat opened).
  const [starters, setStarters] = useState(() =>
    FEATURED_STARTERS.slice(0, STARTERS_PER_MOUNT),
  );
  // Pick once for each new conversation. Suggestions remain still while the
  // user reads or types, and only the visible cards enter the tab order.
  useEffect(() => {
    setStarters(pickRandomStarters(STARTERS_PER_MOUNT));
  }, []);

  useEffect(() => {
    const chat = useChatStore.getState();
    chat.resetSession(null);
    chat.setFocusedSession(null);
    const settings = useSettingsStore.getState();
    settings.setWorkspaceDirectory(directoryParam || (IS_CLOUD ? settings.workspaceDirectory || "/tenant/workspaces" : null));
    useArtifactStore.getState().clearAll();
    useActivityStore.getState().close();
  }, [directoryParam]);

  // Capture the user text in local state so it persists even after
  // startGeneration() clears pendingUserText from the global store.
  // This prevents the user bubble from flashing away before navigation.
  const capturedTextRef = useRef<string | null>(null);
  if (pendingUserText) {
    capturedTextRef.current = pendingUserText;
  }
  if (!isGenerating) {
    capturedTextRef.current = null;
  }
  const displayText = pendingUserText ?? capturedTextRef.current;

  // When generating, switch to a chat-like layout — uses the same
  // StreamingMessage component as chat-view for visual consistency.
  if (isGenerating) {
    return (
      <div className="relative flex flex-1 flex-col h-full overflow-hidden">
        <OfflineOverlay />
        <ChatHeader />

        {/* Messages area — optimistic user bubble + streaming assistant */}
        <div className="flex-1 overflow-y-auto">
          {displayText && (
            <div className="px-4 py-3">
              <div className="mx-auto max-w-3xl xl:max-w-4xl">
                <motion.div
                  className="flex justify-end"
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
                >
                  <div className="max-w-[85%] sm:max-w-[70%] rounded-2xl bg-[var(--user-bubble-bg)] px-4 py-2.5 shadow-[var(--shadow-sm)] border border-[var(--border-default)]">
                    <div className="text-[13px] text-[var(--text-primary)] whitespace-pre-wrap break-words leading-relaxed">
                      {displayText}
                    </div>
                    {pendingAttachments && pendingAttachments.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {pendingAttachments.map((att) => (
                          <FileChip key={att.file_id} file={att} />
                        ))}
                      </div>
                    )}
                  </div>
                </motion.div>
              </div>
            </div>
          )}

          {/* Streaming assistant message — same component used in chat-view */}
          <div className="px-4 py-5">
            <div className="mx-auto max-w-3xl xl:max-w-4xl">
              <StreamingMessage
                sessionId={null}
                parts={streamingParts}
                streamingText={streamingText}
                streamingReasoning={streamingReasoning}
                isAwaitingConfirmation={
                  (!!pendingPermission && isInteractionAwaitingResolution(pendingPermission.responseState)) ||
                  (!!pendingQuestion && isInteractionAwaitingResolution(pendingQuestion.responseState)) ||
                  (!!pendingPlanReview && isInteractionAwaitingResolution(pendingPlanReview.responseState))
                }
              />
            </div>
          </div>
        </div>

        {/* Input */}
        <ChatForm
          isGenerating={isGenerating}
          onSend={sendMessage}
          onGoalCommand={handleGoalCommand}
          onSendTaskBatch={sendTaskBatch}
          onStop={stopGeneration}
          directory={globalWorkspace}
        />
      </div>
    );
  }

  return (
    <div className="relative flex flex-1 flex-col h-full overflow-hidden">
      <OfflineOverlay />
      <ChatHeader />

      <div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-4 py-6 sm:px-8 sm:pb-8">
        <div className="w-full max-w-3xl">
          {/* Provider setup prompt */}
          {!activeProvider && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
              className="mb-6 flex items-center gap-3 rounded-xl border border-[var(--border-default)] px-3 py-2"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-[var(--text-primary)]">
                  {t('setupProvider')}
                </p>
                {/* Auxiliary copy retained in source: <p className="text-xs text-[var(--text-secondary)] mt-0.5">
                  {t('setupProviderDesc')}
                </p> */}
              </div>
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Link
                      href="/settings?tab=providers"
                      aria-label={t('configureSettings')}
                      className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)]"
                    >
                      <Settings className="h-[18px] w-[18px]" aria-hidden="true" />
                    </Link>
                  </TooltipTrigger>
                  <TooltipContent>{t('configureSettings')}</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </motion.div>
          )}

          <div className="mb-5 text-center sm:mb-6">
            <h1 className="mx-auto max-w-[22em] text-2xl font-semibold leading-[1.35] tracking-tight text-[var(--text-primary)] sm:text-[28px]">
              {t('greeting')}
            </h1>
          </div>

          {/* Input — the focal point */}
          <ChatForm
            isGenerating={isGenerating}
            onSend={sendMessage}
            onGoalCommand={handleGoalCommand}
            onSendTaskBatch={sendTaskBatch}
            onStop={stopGeneration}
            directory={globalWorkspace}
            className="!px-0 !pb-0"
            starters={starters.map(starter => ({ key: starter.textKey, icon: starter.icon, label: t(starter.textKey), prompt: t(starter.promptKey) }))}
          />
        </div>
      </div>
    </div>
  );
}
