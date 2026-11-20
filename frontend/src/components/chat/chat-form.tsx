"use client";

import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CircleEllipsis, CornerUpLeft, GripVertical, ListPlus, MoreHorizontal, Play, Plus, RotateCcw, Trash2, X } from "lucide-react";
import { toast } from "sonner";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { Anchor as PopoverAnchor } from "@radix-ui/react-popover";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ChatTextarea } from "./chat-textarea";
import { ChatActions } from "./chat-actions";
import { ContextIndicator } from "./context-indicator";
import { HeaderModelDropdown } from "@/components/selectors/header-model-dropdown";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { ComposerOverflow, type ComposerStarter } from "./composer-overflow";
import { FileChip } from "./file-chip";
import { FileMentionPopup } from "./file-mention-popup";
import { useAutoResize } from "@/hooks/use-auto-resize";
import { uploadFile, browseFiles, attachByPath, ingestFiles } from "@/lib/upload";
import type { FileSearchResult } from "@/lib/upload";
import { cn } from "@/lib/utils";
import { formatElapsedDuration } from "@/lib/duration";
import { parseGoalCommand, type ParsedGoalCommand } from "@/lib/goal-command";
import { resolveComposerWorkspace } from "@/lib/session-inputs";
import type { FileAttachment, SessionInputMode, SessionInputResponse, SessionInputUpdateRequest } from "@/types/chat";
import { useArtifactStore } from "@/stores/artifact-store";
import { useChatSession } from "@/stores/chat-store";
import { useSettingsStore } from "@/stores/settings-store";
import { useProviderModels } from "@/hooks/use-provider-models";
import { useIndexStatus } from "@/hooks/use-index-status";
import { useAgents } from "@/hooks/use-agents";
import { hasImageAttachments, selectedModelSupportsVision } from "@/hooks/use-chat";
import { IS_DESKTOP } from "@/lib/constants";
import { IS_CLOUD, userStorage } from "@/lib/cloud-session";
import type { TaskBatchMode, TaskBatchTask } from "@/types/chat";

interface ChatFormProps {
  isGenerating: boolean;
  isCompacting?: boolean;
  onSend: (text: string, attachments?: FileAttachment[]) => Promise<boolean> | void;
  onQueue?: (text: string, attachments?: FileAttachment[], mode?: SessionInputMode) => Promise<boolean> | void;
  /**
   * Dispatch a parsed Goal command. Return `false` when the action was not
   * accepted; ChatForm then preserves the command draft for retry.
   */
  onGoalCommand?: (
    command: ParsedGoalCommand,
    attachments?: FileAttachment[],
  ) => Promise<boolean | void> | boolean | void;
  pendingInputs?: SessionInputResponse[];
  onCancelInput?: (inputId: string) => Promise<boolean> | void;
  onUpdateInput?: (inputId: string, update: SessionInputUpdateRequest) => Promise<boolean> | void;
  isProgressStalled?: boolean;
  lastBusinessProgressAt?: number | null;
  onSendTaskBatch?: (batch: { mode: TaskBatchMode; tasks: TaskBatchTask[] }) => Promise<boolean> | void;
  onStop: () => void;
  className?: string;
  sessionId?: string;
  directory?: string | null;
  starters?: ComposerStarter[];
}

/** Persistent per-session draft cache backed by localStorage. */
interface Draft {
  input: string;
  attachments: FileAttachment[];
  savedAt: number;
}

type TaskDraft = {
  id: string;
  title: string;
  prompt: string;
  agent: string;
  model: string;
  provider_id: string;
};

const DRAFT_STORAGE_KEY = "suxiaoyou-drafts";
const DRAFT_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

/** In-memory mirror of localStorage drafts — avoids repeated JSON parsing. */
let draftMirror: Map<string, Draft> | null = null;

function loadDrafts(): Map<string, Draft> {
  if (draftMirror) return draftMirror;
  try {
    const raw = userStorage.getItem(DRAFT_STORAGE_KEY);
    if (!raw) { draftMirror = new Map(); return draftMirror; }
    const parsed: Record<string, Draft> = JSON.parse(raw);
    const now = Date.now();
    // Evict expired drafts on load
    const entries = Object.entries(parsed).filter(
      ([, d]) => now - d.savedAt < DRAFT_MAX_AGE_MS,
    );
    draftMirror = new Map(entries);
  } catch {
    draftMirror = new Map();
  }
  return draftMirror;
}

function saveDraft(key: string, draft: Draft) {
  const map = loadDrafts();
  map.set(key, draft);
  flushDrafts(map);
}

function deleteDraft(key: string) {
  const map = loadDrafts();
  if (map.delete(key)) flushDrafts(map);
}

function flushDrafts(map: Map<string, Draft>) {
  try {
    userStorage.setItem(
      DRAFT_STORAGE_KEY,
      JSON.stringify(Object.fromEntries(map)),
    );
  } catch {
    // localStorage quota exceeded — silently skip
  }
}

function mergeAttachments(
  existing: FileAttachment[],
  incoming: FileAttachment[],
): { merged: FileAttachment[]; duplicateCount: number } {
  const keyOf = (f: FileAttachment) => `${f.path}::${f.size}::${f.name}`;
  const seen = new Set(existing.map(keyOf));
  const unique: FileAttachment[] = [];
  let duplicateCount = 0;

  for (const file of incoming) {
    const key = keyOf(file);
    if (seen.has(key)) {
      duplicateCount += 1;
      continue;
    }
    seen.add(key);
    unique.push(file);
  }

  return {
    merged: [...existing, ...unique],
    duplicateCount,
  };
}

type PathBackedFile = File & {
  path?: string;
};

function pathsFromDataTransfer(dataTransfer: DataTransfer): string[] {
  const paths = new Set<string>();

  for (const file of Array.from(dataTransfer.files) as PathBackedFile[]) {
    if (typeof file.path === "string" && file.path) {
      paths.add(file.path);
    }
  }

  for (const item of Array.from(dataTransfer.items ?? [])) {
    const file = item.kind === "file" ? item.getAsFile() as PathBackedFile | null : null;
    if (file?.path) paths.add(file.path);
  }

  return [...paths];
}

function normalizePastedPath(rawPath: string): string | null {
  let path = rawPath.trim();
  if (!path) return null;

  path = path.replace(/^["']|["']$/g, "");

  if (path.startsWith("file://")) {
    try {
      const url = new URL(path);
      const pathname = decodeURIComponent(url.pathname);
      return /^\/[A-Za-z]:\//.test(pathname) ? pathname.slice(1) : pathname;
    } catch {
      return null;
    }
  }

  const isUnixAbsolute = path.startsWith("/");
  const isWindowsAbsolute = /^[A-Za-z]:[\\/]/.test(path);
  const isUncPath = path.startsWith("\\\\");
  if (!isUnixAbsolute && !isWindowsAbsolute && !isUncPath) return null;

  if (isUnixAbsolute) {
    path = path.replace(/\\([ "'()[\]{}])/g, "$1");
  }

  return path;
}

function pathsFromPastedText(text: string): string[] {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith("#"));
  if (lines.length === 0) return [];

  const paths = lines.map(normalizePastedPath);
  if (paths.some((path) => !path)) return [];

  return [...new Set(paths as string[])];
}

function pointInsideElement(el: HTMLElement | null, position: { x: number; y: number }): boolean {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const contains = (x: number, y: number) =>
    x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
  return contains(position.x / ratio, position.y / ratio) || contains(position.x, position.y);
}

/**
 * Find the active @mention trigger in the input text relative to the cursor position.
 * Returns { active: true, query, startIndex } if cursor is inside an @mention,
 * or { active: false } otherwise.
 */
function detectMention(
  text: string,
  cursorPos: number,
): { active: true; query: string; startIndex: number } | { active: false } {
  // Look backwards from cursor for '@'
  const before = text.slice(0, cursorPos);
  const atIndex = before.lastIndexOf("@");
  if (atIndex === -1) return { active: false };

  // '@' must be at start of input or preceded by whitespace
  if (atIndex > 0 && !/\s/.test(before[atIndex - 1])) {
    return { active: false };
  }

  // Query is text between '@' and cursor — must not contain newlines or spaces
  const query = before.slice(atIndex + 1);
  if (/[\s]/.test(query)) return { active: false };

  return { active: true, query, startIndex: atIndex };
}

export function ChatForm({
  isGenerating,
  isCompacting = false,
  onSend,
  onQueue,
  onGoalCommand,
  pendingInputs = [],
  onCancelInput,
  onUpdateInput,
  isProgressStalled = false,
  lastBusinessProgressAt = null,
  onSendTaskBatch,
  onStop,
  className,
  sessionId,
  directory,
  starters,
}: ChatFormProps) {
  const { t, i18n } = useTranslation('chat');
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<FileAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [isDragOver, setIsDragOver] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchMode, setBatchMode] = useState<TaskBatchMode>("parallel");
  const [batchTasks, setBatchTasks] = useState<TaskDraft[]>([]);
  const [draggedInputId, setDraggedInputId] = useState<string | null>(null);
  const [dragOverInputId, setDragOverInputId] = useState<string | null>(null);
  const { ref, resize } = useAutoResize();
  const dropTargetRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const directoryInputRef = useRef<HTMLInputElement>(null);
  const moreTriggerRef = useRef<HTMLButtonElement>(null);
  const batchInteractedOutsideRef = useRef(false);
  const { data: providerModels, activeProvider } = useProviderModels();
  const { data: agents } = useAgents();
  const selectedAgent = useSettingsStore((s) => s.selectedAgent);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const selectedProviderId = useSettingsStore((s) => s.selectedProviderId);
  const noModelsAvailable = !activeProvider || providerModels.length === 0;
  // Surface the vision constraint up front: an image is attached but the
  // selected model can't read images. The send is also blocked server-side and
  // in useChat, but that only fires on send — leaving the composer looking like
  // nothing happened. This warns the moment the image is added.
  const imageNeedsVisionModel =
    hasImageAttachments(attachments) &&
    !!selectedModel &&
    !selectedModelSupportsVision(providerModels, selectedModel, selectedProviderId);

  const sendingRef = useRef(false);
  const taskDraftIdRef = useRef(0);
  const tauriDropHandledAtRef = useRef(0);
  const [statusNow, setStatusNow] = useState(() => Date.now());

  useEffect(() => {
    if (!isGenerating || !isProgressStalled) return;
    setStatusNow(Date.now());
    const timer = window.setInterval(() => setStatusNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [isGenerating, isProgressStalled]);

  // Track latest values for draft save-on-unmount (avoids stale closures)
  const inputRef = useRef(input);
  const attachmentsRef = useRef(attachments);
  inputRef.current = input;
  attachmentsRef.current = attachments;

  const draftKey = sessionId ?? "__new__";

  // Restore draft on mount (keyed by draftKey)
  useEffect(() => {
    const drafts = loadDrafts();
    const saved = drafts.get(draftKey);
    if (saved) {
      setInput(saved.input);
      setAttachments(saved.attachments);
      deleteDraft(draftKey);
    }
    // Save draft on unmount
    return () => {
      const currentInput = inputRef.current;
      const currentAttachments = attachmentsRef.current;
      if (currentInput.trim() || currentAttachments.length > 0) {
        saveDraft(draftKey, {
          input: currentInput,
          attachments: currentAttachments,
          savedAt: Date.now(),
        });
      }
    };
  }, [draftKey]);

  // @mention state
  const [mentionActive, setMentionActive] = useState(false);
  const [mentionQuery, setMentionQuery] = useState("");
  const [mentionStartIndex, setMentionStartIndex] = useState(-1);

  const hasWorkspace = !!directory && directory !== ".";

  const globalWorkspace = useSettingsStore((s) => s.workspaceDirectory);
  const effectiveWorkspace = resolveComposerWorkspace(
    sessionId,
    directory,
    globalWorkspace,
  );
  const { isIndexing } = useIndexStatus(effectiveWorkspace, sessionId);
  const formSession = useChatSession(sessionId ?? null);
  const compactingLabel = (() => {
    const streamingParts = formSession.streamingParts;
    for (let i = streamingParts.length - 1; i >= 0; i -= 1) {
      const part = streamingParts[i];
      if (part.type !== "compaction" || part.compactionStatus !== "in_progress") continue;
      const activePhase = part.phases?.find((phase) => phase.status === "started");
      if (!activePhase) return null;
      if (activePhase.phase === "prune") return "prune";
      if (activePhase.phase === "summarize" && activePhase.chars && activePhase.chars > 0) {
        return `summarize:${activePhase.chars}`;
      }
      return "summarize";
    }
    return null;
  })();
  const canQueueWhileGenerating = !!sessionId && !!onQueue;
  const isInputDisabled = isCompacting || noModelsAvailable || (isGenerating && !canQueueWhileGenerating);
  const isBatchDisabled = isInputDisabled || isGenerating;
  const visibleAgents = useMemo(
    () => (agents ?? []).filter((agent) => agent.mode !== "hidden"),
    [agents],
  );
  const defaultBatchAgents = useMemo(() => {
    const subagents = visibleAgents.filter((agent) => agent.mode === "subagent");
    return subagents.length > 0 ? subagents : visibleAgents;
  }, [visibleAgents]);

  const createTaskDraft = useCallback((index: number, prompt = ""): TaskDraft => {
    const agentName = defaultBatchAgents[index]?.name ?? defaultBatchAgents[0]?.name ?? selectedAgent ?? "build";
    taskDraftIdRef.current += 1;
    return {
      id: `task-${Date.now()}-${taskDraftIdRef.current}`,
      title: agentName === "build" || agentName === "plan" ? `Task ${index + 1}` : agentName,
      prompt,
      agent: agentName,
      model: selectedModel ?? "",
      provider_id: selectedProviderId ?? "",
    };
  }, [defaultBatchAgents, selectedAgent, selectedModel, selectedProviderId]);

  const handleBatchOpenChange = useCallback((open: boolean) => {
    setBatchOpen(open);
    if (open && batchTasks.length === 0) {
      setBatchTasks([
        createTaskDraft(0, input.trim()),
        createTaskDraft(1),
      ]);
    }
  }, [batchTasks.length, createTaskDraft, input]);

  const updateBatchTask = useCallback((id: string, patch: Partial<TaskDraft>) => {
    setBatchTasks((prev) => prev.map((task) => task.id === id ? { ...task, ...patch } : task));
  }, []);

  const addBatchTask = useCallback(() => {
    setBatchTasks((prev) => [...prev, createTaskDraft(prev.length)]);
  }, [createTaskDraft]);

  const removeBatchTask = useCallback((id: string) => {
    setBatchTasks((prev) => prev.length > 1 ? prev.filter((task) => task.id !== id) : prev);
  }, []);

  const useInputForFirstTask = useCallback(() => {
    const text = input.trim();
    if (!text) return;
    setBatchTasks((prev) => {
      const next = prev.length > 0 ? prev : [createTaskDraft(0)];
      return next.map((task, index) => index === 0 ? { ...task, prompt: text } : task);
    });
  }, [createTaskDraft, input]);

  const addAttachments = useCallback((files: FileAttachment[]) => {
    setAttachments((prev) => {
      const { merged, duplicateCount } = mergeAttachments(prev, files);
      if (duplicateCount > 0) {
        toast.info(t('duplicateFilesSkipped', { count: duplicateCount }));
      }
      return merged;
    });
    if (sessionId && effectiveWorkspace && files.length > 0) {
      ingestFiles(sessionId, effectiveWorkspace, files.map((r) => r.path));
    }
  }, [effectiveWorkspace, sessionId, t]);

  const handleAttachPaths = useCallback(async (paths: string[]) => {
    const uniquePaths = [...new Set(paths.filter(Boolean))];
    if (uniquePaths.length === 0) return;
    setUploading(true);
    try {
      const attached = await attachByPath(uniquePaths);
      if (attached.length > 0) {
        addAttachments(attached);
      }
    } catch (err) {
      console.error("Attach by path failed:", err);
      toast.error(t('failedUpload'));
    } finally {
      setUploading(false);
    }
  }, [addAttachments, t]);

  const handleFiles = useCallback(async (files: FileList | File[]) => {
    if (IS_CLOUD && files.length > 5000) {
      toast.error("一次最多上传 5000 个文件。");
      return;
    }
    setUploading(true);
    const results: FileAttachment[] = [];
    try {
      for (const file of Array.from(files)) results.push(await uploadFile(file, effectiveWorkspace));
    } catch (err) {
      console.error("Upload failed:", err);
      toast.error(t('failedUpload'));
    } finally {
      if (results.length) addAttachments(results);
      setUploading(false);
    }
  }, [addAttachments, t, effectiveWorkspace]);

  const handleDropDataTransfer = useCallback((dataTransfer: DataTransfer) => {
    const paths = pathsFromDataTransfer(dataTransfer);
    if (paths.length > 0) {
      void handleAttachPaths(paths);
      return;
    }

    const droppedFiles = Array.from(dataTransfer.files);
    if (droppedFiles.length === 0) return;
    if (Date.now() - tauriDropHandledAtRef.current < 750) return;
    window.setTimeout(() => {
      if (Date.now() - tauriDropHandledAtRef.current < 750) return;
      void handleFiles(droppedFiles);
    }, 120);
  }, [handleAttachPaths, handleFiles]);

  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (isInputDisabled) return;

    const clipboard = e.clipboardData;
    const clipboardPaths = pathsFromDataTransfer(clipboard);
    if (clipboardPaths.length > 0) {
      e.preventDefault();
      void handleAttachPaths(clipboardPaths);
      return;
    }

    const clipboardFiles = Array.from(clipboard.files);
    if (clipboardFiles.length > 0) {
      e.preventDefault();
      void handleFiles(clipboardFiles);
      return;
    }

    const text = clipboard.getData("text/uri-list") || clipboard.getData("text/plain");
    const pastedPaths = pathsFromPastedText(text);
    if (pastedPaths.length > 0) {
      e.preventDefault();
      void handleAttachPaths(pastedPaths);
    }
  }, [handleAttachPaths, handleFiles, isInputDisabled]);

  useEffect(() => {
    if (!IS_DESKTOP) return;
    let unlisten: (() => void) | undefined;
    let disposed = false;

    import("@tauri-apps/api/webview")
      .then(({ getCurrentWebview }) =>
        getCurrentWebview().onDragDropEvent((event) => {
          const payload = event.payload;
          if (payload.type === "enter" || payload.type === "over") {
            setIsDragOver(pointInsideElement(dropTargetRef.current, payload.position));
            return;
          }
          if (payload.type === "leave") {
            setIsDragOver(false);
            return;
          }
          if (payload.type === "drop") {
            setIsDragOver(false);
            if (!pointInsideElement(dropTargetRef.current, payload.position)) return;
            tauriDropHandledAtRef.current = Date.now();
            void handleAttachPaths(payload.paths);
          }
        }),
      )
      .then((fn) => {
        if (disposed) {
          fn();
        } else {
          unlisten = fn;
        }
      })
      .catch((err) => {
        console.warn("Tauri file drop listener unavailable:", err);
      });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [handleAttachPaths]);

  const handleSend = useCallback(async () => {
    if (
      sendingRef.current ||
      (!input.trim() && attachments.length === 0) ||
      isCompacting
    ) return;

    // Goal commands are control-plane input. Parse and dispatch them before
    // the ordinary send/queue path so their literal text never reaches the
    // model, even when this ChatForm has not been wired to a Goal API yet.
    const goalCommand = parseGoalCommand(input);
    if (goalCommand) {
      if (!goalCommand.ok) {
        if (goalCommand.error === "objective_required") {
          toast.error(t("goalCommandObjectiveRequired"));
        } else if (goalCommand.error === "objective_too_long") {
          toast.error(t("goalCommandObjectiveTooLong", {
            count: goalCommand.objectiveCharacters,
            limit: goalCommand.maxObjectiveCharacters,
          }));
        } else {
          toast.error(t("goalCommandUnexpectedArgument"));
        }
        return;
      }

      if (!onGoalCommand) {
        toast.error(t("goalCommandUnavailable"));
        return;
      }

      sendingRef.current = true;
      const submittedText = input;
      const submittedAttachments = attachments;
      try {
        const result = await onGoalCommand(
          goalCommand,
          submittedAttachments.length > 0 ? submittedAttachments : undefined,
        );
        if (result === false) return;

        // Preserve anything the user typed or attached while the asynchronous
        // command was running; only clear the exact command payload accepted
        // by the Goal API.
        const inputUnchanged = inputRef.current === submittedText;
        const attachmentsUnchanged = attachmentsRef.current === submittedAttachments;
        if (inputUnchanged) {
          setInput("");
          inputRef.current = "";
          setMentionActive(false);
          if (ref.current) {
            ref.current.style.height = "auto";
          }
        }
        if (attachmentsUnchanged) {
          setAttachments([]);
          attachmentsRef.current = [];
        }
        if (inputUnchanged && attachmentsUnchanged) {
          deleteDraft(draftKey);
        }
      } catch (error) {
        console.error("Goal command failed:", error);
        toast.error(t("goalCommandFailed"));
      } finally {
        sendingRef.current = false;
      }
      return;
    }

    if (isGenerating && !onQueue) return;

    sendingRef.current = true;
    try {
      const text = input;
      const files = attachments;
      setInput("");
      setAttachments([]);
      // Clear refs immediately so unmount cleanup won't save stale draft
      inputRef.current = "";
      attachmentsRef.current = [];
      setMentionActive(false);
      if (ref.current) {
        ref.current.style.height = "auto";
      }
      const result = isGenerating && onQueue
        ? await onQueue(text, files.length > 0 ? files : undefined, "queue")
        : await onSend(text, files.length > 0 ? files : undefined);
      // Restore input if send failed
      if (result === false) {
        setInput(text);
        setAttachments(files);
      } else {
        deleteDraft(draftKey);
      }
    } finally {
      sendingRef.current = false;
    }
  }, [input, attachments, isGenerating, isCompacting, onQueue, onSend, onGoalCommand, ref, draftKey, t]);

  const handleSendTaskBatch = useCallback(async () => {
    if (!onSendTaskBatch || isGenerating || isCompacting || sendingRef.current) return;
    if (attachments.length > 0) {
      toast.error(t("taskBatchAttachmentsUnsupported"));
      return;
    }

    const hasIncompleteTask = batchTasks.some((task) => !task.title.trim() || !task.prompt.trim());
    if (hasIncompleteTask) {
      toast.error(t("taskBatchIncomplete"));
      return;
    }

    const tasks = batchTasks.map((task) => ({
      title: task.title.trim(),
      prompt: task.prompt.trim(),
      agent: task.agent || selectedAgent || "build",
      model: task.model || null,
      provider_id: task.provider_id || null,
    }));
    if (tasks.length === 0) return;

    sendingRef.current = true;
    try {
      const result = await onSendTaskBatch({ mode: batchMode, tasks });
      if (result !== false) {
        setBatchOpen(false);
        setBatchTasks([]);
        setInput("");
        inputRef.current = "";
        deleteDraft(draftKey);
      }
    } finally {
      sendingRef.current = false;
    }
  }, [attachments.length, batchMode, batchTasks, draftKey, isCompacting, isGenerating, onSendTaskBatch, selectedAgent, t]);

  const handleBrowse = useCallback(async () => {
    setUploading(true);
    try {
      const results = await browseFiles(effectiveWorkspace);
      if (results.length > 0) {
        addAttachments(results);
      }
    } catch (err) {
      console.error("Browse failed, falling back to browser picker:", err);
      fileInputRef.current?.click();
    } finally {
      setUploading(false);
    }
  }, [addAttachments, effectiveWorkspace]);

  const handleRemoveAttachment = useCallback((fileId: string) => {
    setAttachments((prev) => prev.filter((a) => a.file_id !== fileId));
  }, []);

  const handleRestorePendingInput = useCallback(async (item: SessionInputResponse) => {
    if (
      sendingRef.current
      || input.trim()
      || attachments.length > 0
      || !onCancelInput
    ) return;

    sendingRef.current = true;
    try {
      // Cancellation is the durable ownership transfer. Restore only after it
      // succeeds so one user action can never leave both a queued record and
      // an editable draft that might later run twice.
      const cancelled = await onCancelInput(item.id);
      if (cancelled === false) return;

      setInput(item.text);
      setAttachments(item.attachments);
      inputRef.current = item.text;
      attachmentsRef.current = item.attachments;
      setMentionActive(false);
      requestAnimationFrame(() => {
        ref.current?.focus();
        resize();
      });
    } finally {
      sendingRef.current = false;
    }
  }, [attachments.length, input, onCancelInput, ref, resize]);

  // Handle @mention file selection
  const handleMentionSelect = useCallback(async (result: FileSearchResult) => {
    // Replace @query with @filename in the input
    const before = input.slice(0, mentionStartIndex);
    const after = input.slice(mentionStartIndex + 1 + mentionQuery.length);
    const newInput = `${before}@${result.name} ${after}`;
    setInput(newInput);
    setMentionActive(false);

    // Attach the file
    try {
      const attached = await attachByPath([result.absolute_path]);
      if (attached.length > 0) {
        addAttachments(attached);
      }
    } catch (err) {
      console.error("Failed to attach file:", err);
    }

    // Refocus and resize
    requestAnimationFrame(() => {
      ref.current?.focus();
      resize();
    });
  }, [input, mentionStartIndex, mentionQuery, ref, resize, addAttachments]);

  const handleMentionClose = useCallback(() => {
    setMentionActive(false);
  }, []);

  // Handle input changes — detect @mention trigger
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const value = e.target.value;
      const cursorPos = e.target.selectionStart ?? value.length;
      setInput(value);
      resize();

      if (!hasWorkspace) {
        if (mentionActive) setMentionActive(false);
        return;
      }

      const mention = detectMention(value, cursorPos);
      if (mention.active) {
        setMentionActive(true);
        setMentionQuery(mention.query);
        setMentionStartIndex(mention.startIndex);
      } else {
        if (mentionActive) setMentionActive(false);
      }
    },
    [hasWorkspace, mentionActive, resize],
  );

  // Also check mention state on cursor movement (click, arrow keys)
  const handleSelect = useCallback(
    (e: React.SyntheticEvent<HTMLTextAreaElement>) => {
      if (!hasWorkspace) return;
      const textarea = e.currentTarget;
      const cursorPos = textarea.selectionStart ?? 0;
      const mention = detectMention(textarea.value, cursorPos);
      if (mention.active) {
        setMentionActive(true);
        setMentionQuery(mention.query);
        setMentionStartIndex(mention.startIndex);
      } else {
        if (mentionActive) setMentionActive(false);
      }
    },
    [hasWorkspace, mentionActive],
  );

  // Watch for "Try fixing" requests from artifact renderers
  const fixRequest = useArtifactStore((s) => s.fixRequest);
  const clearFixRequest = useArtifactStore((s) => s.clearFixRequest);

  useEffect(() => {
    if (!fixRequest) return;
    setInput(fixRequest);
    clearFixRequest();
    // Focus the textarea
    requestAnimationFrame(() => {
      ref.current?.focus();
      resize();
    });
  }, [fixRequest, clearFixRequest, ref, resize]);

  const compactingStatusText = useMemo(() => {
    if (!isCompacting) return null;
    if (!compactingLabel) return t("contextCompactingNow");
    if (compactingLabel === "prune") return t("contextCompactingPrune");
    if (compactingLabel === "summarize") return t("contextCompactingSummarize");
    if (compactingLabel.startsWith("summarize:")) {
      const chars = Number(compactingLabel.split(":")[1] || 0);
      return t("contextCompactingSummarizeProgress", { chars });
    }
    return t("contextCompactingNow");
  }, [compactingLabel, isCompacting, t]);
  const canStartTaskBatch = !!onSendTaskBatch &&
    batchTasks.length > 0 &&
    batchTasks.every((task) => task.title.trim() && task.prompt.trim()) &&
    !isBatchDisabled &&
    !isIndexing;
  const stalledDuration = lastBusinessProgressAt === null
    ? null
    : formatElapsedDuration(
        Math.max(0, (statusNow - lastBusinessProgressAt) / 1_000),
        i18n.language,
      );

  return (
    <TooltipProvider delayDuration={150}>
    <div className={cn("shrink-0 px-3 pb-4 sm:px-6 sm:pb-6", className)}>
      <div className="mx-auto max-w-3xl xl:max-w-4xl">
        {isGenerating && isProgressStalled && (
          <div className="mb-2 flex">
            <div
              className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-[var(--border-default)] bg-[var(--surface-secondary)] px-2.5 py-1.5 text-[12px] leading-relaxed text-[var(--text-tertiary)]"
              data-testid="progress-stalled-notice"
            >
              <CircleEllipsis className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0">
                <span role="status" aria-live="polite">
                  {t("taskMayBeStalled")}
                </span>
                {stalledDuration && (
                  <span
                    className="text-[var(--text-quaternary)]"
                    aria-hidden="true"
                    data-testid="progress-stalled-duration"
                  >
                    {" · "}{t("taskNoProgressFor", { duration: stalledDuration })}
                  </span>
                )}
              </span>
            </div>
          </div>
        )}
        {pendingInputs.length > 0 && (
          <section
            className="mb-2 overflow-hidden rounded-xl border border-[var(--border-default)] bg-[var(--surface-primary)]"
            aria-label={t("pendingInputsTitle")}
            data-testid="pending-inputs"
          >
            <div className="px-3 py-2.5">
              <div className="flex items-center gap-2">
                <ListPlus className="h-3.5 w-3.5 shrink-0 text-[var(--text-tertiary)]" aria-hidden="true" />
                <span className="text-xs font-medium text-[var(--text-primary)]">
                  {t("pendingInputsTitle")}
                </span>
                <span className="text-[12px] tabular-nums text-[var(--text-tertiary)]">
                  {pendingInputs.length}
                </span>
              </div>
              <p
                className="mt-1 pl-[22px] text-[12px] leading-relaxed text-[var(--text-tertiary)]"
                data-testid="pending-inputs-guidance"
              >
                {t(isGenerating ? "pendingInputsRunningHint" : "pendingInputsIdleHint")}
              </p>
            </div>
            <ol className="max-h-36 divide-y divide-[var(--border-default)] overflow-y-auto border-t border-[var(--border-default)]">
              {pendingInputs.map((item, index) => {
                const canCancel = item.status === "queued" || item.status === "blocked";
                const canRestore = canCancel;
                const canMove = item.status === "queued";
                const canSteer = item.status === "queued" && item.mode === "queue";
                const queuedPosition = pendingInputs
                  .filter((candidate) => candidate.status === "queued")
                  .findIndex((candidate) => candidate.id === item.id) + 1;
                const hasDraft = !!input.trim() || attachments.length > 0;
                const summary = item.text.trim()
                  || item.attachments.map((attachment) => attachment.name).join(", ")
                  || t("pendingInputAttachmentOnly");
                return (
                  <li
                    key={item.id}
                    draggable={canMove}
                    onDragStart={(event) => {
                      if (!canMove) return;
                      setDraggedInputId(item.id);
                      setDragOverInputId(item.id);
                      event.dataTransfer.effectAllowed = "move";
                      event.dataTransfer.setData("text/plain", item.id);
                    }}
                    onDragOver={(event) => {
                      if (!canMove || !draggedInputId || draggedInputId === item.id) return;
                      event.preventDefault();
                      event.dataTransfer.dropEffect = "move";
                      setDragOverInputId(item.id);
                    }}
                    onDrop={(event) => {
                      event.preventDefault();
                      if (canMove && draggedInputId && draggedInputId !== item.id) {
                        void onUpdateInput?.(draggedInputId, { position: queuedPosition });
                      }
                      setDraggedInputId(null);
                      setDragOverInputId(null);
                    }}
                    onDragEnd={() => {
                      setDraggedInputId(null);
                      setDragOverInputId(null);
                    }}
                    className={cn(
                      "group flex min-w-0 items-center gap-2 border-l-2 border-transparent px-3 py-2 text-xs transition-colors hover:bg-[var(--surface-secondary)]",
                      canMove && "cursor-grab active:cursor-grabbing",
                      draggedInputId === item.id && "opacity-50",
                      dragOverInputId === item.id && draggedInputId !== item.id && "border-l-[var(--brand-primary)] bg-[var(--surface-tertiary)]",
                    )}
                    data-testid={`pending-input-${item.id}`}
                  >
                    {canMove && (
                      <GripVertical
                        className="h-3.5 w-3.5 shrink-0 text-[var(--text-tertiary)] opacity-45 group-hover:opacity-100"
                        aria-hidden="true"
                      />
                    )}
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center text-[12px] tabular-nums text-[var(--text-tertiary)]">
                      {index + 1}
                    </span>
                    <span
                      className="min-w-0 flex-1 truncate text-[var(--text-primary)]"
                      title={summary}
                    >
                      {summary}
                    </span>
                    {item.attachments.length > 0 && (
                      <span className="shrink-0 text-[12px] text-[var(--text-tertiary)]">
                        {t("pendingInputFiles", { count: item.attachments.length })}
                      </span>
                    )}
                    {item.mode === "steer" && (
                      <span className="shrink-0 rounded-full bg-[var(--surface-tertiary)] px-2 py-0.5 text-[12px] text-[var(--text-secondary)]">
                        {t("inputModeSteerShort")}
                      </span>
                    )}
                    {item.status !== "queued" && (
                      <span className="shrink-0 text-[12px] text-[var(--text-tertiary)]">
                        {t(`inputStatus_${item.status}`)}
                      </span>
                    )}
                    {canCancel && (
                      <DropdownMenu>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <DropdownMenuTrigger asChild>
                              <button
                                type="button"
                                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)] data-[state=open]:bg-[var(--surface-tertiary)]"
                                aria-label={t("inputMoreActionsAria", { position: index + 1 })}
                              >
                                <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
                              </button>
                            </DropdownMenuTrigger>
                          </TooltipTrigger>
                          <TooltipContent>{t("inputMoreActionsAria", { position: index + 1 })}</TooltipContent>
                        </Tooltip>
                        <DropdownMenuContent align="end" className="w-48">
                          {canSteer && (
                            <DropdownMenuItem
                              onSelect={() => void onUpdateInput?.(item.id, { mode: "steer" })}
                              disabled={!onUpdateInput}
                            >
                              <CornerUpLeft className="h-4 w-4" />
                              {t("inputSteer")}
                            </DropdownMenuItem>
                          )}
                          {canRestore && (
                            <DropdownMenuItem
                              onSelect={() => void handleRestorePendingInput(item)}
                              disabled={hasDraft || !onCancelInput}
                              title={hasDraft ? t("inputRestoreDraftBusy") : undefined}
                            >
                              <RotateCcw className="h-4 w-4" />
                              {t("inputRestore")}
                            </DropdownMenuItem>
                          )}
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            onSelect={() => void onCancelInput?.(item.id)}
                            disabled={!onCancelInput}
                            className="text-[var(--color-destructive)] focus:text-[var(--color-destructive)]"
                          >
                            <X className="h-4 w-4" />
                            {t("inputCancel")}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    )}
                  </li>
                );
              })}
            </ol>
          </section>
        )}
        <div
          ref={dropTargetRef}
          className={cn(
            "composer-shell relative",
            isDragOver && "ring-1 ring-[var(--border-heavy)]",
          )}
          onDragOver={(e) => {
            e.preventDefault();
            setIsDragOver(true);
          }}
          onDragLeave={() => setIsDragOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setIsDragOver(false);
            handleDropDataTransfer(e.dataTransfer);
          }}
        >
          {/* @mention popup — positioned above the form */}
          {hasWorkspace && (
            <FileMentionPopup
              query={mentionQuery}
              directory={directory!}
              onSelect={handleMentionSelect}
              onClose={handleMentionClose}
              visible={mentionActive}
            />
          )}

          {/* One composed surface keeps input and tools in the same hierarchy. */}
          <div className="rounded-[20px]">
          {/* Top section: file chips + textarea */}
          <div className="px-4 pb-2 pt-3 sm:px-5 sm:pb-3 sm:pt-5">
            {/* File chips */}
            {(attachments.length > 0 || uploading) && (
              <div className="flex flex-wrap gap-1.5 pb-2">
                {attachments.map((att) => (
                  <FileChip
                    key={att.file_id}
                    file={att}
                    onRemove={() => handleRemoveAttachment(att.file_id)}
                  />
                ))}
                {uploading && (
                  <div className="inline-flex items-center gap-1.5 text-xs text-[var(--text-tertiary)]">
                    <span className="animate-spin h-3 w-3 border border-current border-t-transparent rounded-full" />
                    {t('uploading')}
                  </div>
                )}
              </div>
            )}

            {imageNeedsVisionModel && (
              <div className="flex items-start gap-1.5 pb-2 text-xs text-[var(--color-warning)]">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" />
                <span>{t('imageNeedsVisionModel')}</span>
              </div>
            )}

            <ChatTextarea
              ref={ref}
              value={input}
              onChange={handleInputChange}
              onPaste={handlePaste}
              onSelect={handleSelect}
              onSubmit={handleSend}
              mentionActive={mentionActive}
              placeholder={
                noModelsAvailable
                  ? t("noModelPlaceholder")
                  : isGenerating
                    ? t("inputWhileRunningPlaceholder")
                    : hasWorkspace
                      ? t("placeholder") + t("placeholderMention")
                      : t("placeholder")
              }
              className="min-h-[48px] max-h-[200px] py-0 placeholder-shown:!h-12 sm:min-h-[56px] sm:py-1 sm:placeholder-shown:!h-14"
              disabled={isInputDisabled}
            />

          </div>

          {compactingStatusText && <div role="status" className="px-4 pb-2 text-xs text-[var(--text-secondary)]">{compactingStatusText}</div>}

          {/* Keep only frequent controls visible; additional tools live in More. */}
          <div className="flex min-w-0 items-center gap-1 px-2 pb-2 sm:gap-2 sm:px-3 sm:pb-3">
            {/* Hidden file input */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => {
                if (e.target.files && e.target.files.length > 0) {
                  handleFiles(e.target.files);
                  e.target.value = "";
                }
              }}
            />

            {IS_CLOUD && <input
              ref={element => { directoryInputRef.current = element; if (element) element.webkitdirectory = true; }}
              type="file" multiple className="hidden" aria-label="选择上传文件夹"
              onChange={event => {
                if (event.target.files?.length) void handleFiles(event.target.files);
                event.target.value = "";
              }}
            />}

            <Tooltip>
              <TooltipTrigger asChild>
              <button
                type="button"
                disabled={isInputDisabled}
                className="icon-action shrink-0 flex items-center justify-center h-11 w-11 rounded-xl hover:bg-[var(--surface-secondary)] transition-colors text-[var(--text-secondary)]"
                aria-label={t('attachFile')}
                onClick={handleBrowse}
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
              </button>
              </TooltipTrigger>
              <TooltipContent>{t('attachFile')}</TooltipContent>
            </Tooltip>

            <Popover open={batchOpen} onOpenChange={handleBatchOpenChange}>
              <PopoverAnchor asChild>
                <div className="shrink-0">
                  <ComposerOverflow
                    triggerRef={moreTriggerRef}
                    disabled={isInputDisabled}
                    batchDisabled={isBatchDisabled}
                    onOpenBatch={onSendTaskBatch ? () => handleBatchOpenChange(true) : undefined}
                    onUploadFolder={IS_CLOUD ? () => directoryInputRef.current?.click() : undefined}
                    onSelectStarter={prompt => useArtifactStore.getState().requestFix(prompt)}
                    starters={starters}
                    sessionId={sessionId}
                    directory={directory}
                    isIndexing={isIndexing}
                    context={sessionId ? <ContextIndicator sessionId={sessionId} compact /> : undefined}
                  />
                </div>
              </PopoverAnchor>
              {onSendTaskBatch && <PopoverContent
                  aria-label={t("taskBatchButton")}
                  align="start"
                  side="top"
                  sideOffset={8}
                  collisionPadding={16}
                  style={{ maxHeight: "min(680px, var(--radix-popover-content-available-height))" }}
                  className="w-[min(720px,calc(100vw-32px))] overflow-y-auto p-3"
                  onOpenAutoFocus={() => {
                    batchInteractedOutsideRef.current = false;
                  }}
                  onInteractOutside={() => {
                    batchInteractedOutsideRef.current = true;
                  }}
                  onCloseAutoFocus={event => {
                    event.preventDefault();
                    if (!batchInteractedOutsideRef.current) moreTriggerRef.current?.focus();
                    batchInteractedOutsideRef.current = false;
                  }}
                >
                  <div className="flex flex-col gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="flex rounded-full bg-[var(--surface-secondary)] p-0.5">
                        {(["parallel", "sequential"] as TaskBatchMode[]).map((mode) => (
                          <button
                            key={mode}
                            type="button"
                            onClick={() => setBatchMode(mode)}
                            className={cn(
                              "rounded-full px-3 py-1.5 text-[12px] font-medium transition-colors",
                              batchMode === mode
                                ? "bg-[var(--surface-primary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]"
                                : "text-[var(--text-secondary)] hover:text-[var(--text-primary)]",
                            )}
                          >
                            {t(mode === "parallel" ? "taskBatchModeParallelShort" : "taskBatchModeSequentialShort")}
                          </button>
                        ))}
                      </div>
                      <button
                        type="button"
                        onClick={useInputForFirstTask}
                        disabled={!input.trim()}
                        className="rounded-full px-3 py-1.5 text-[12px] font-medium text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)] disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-tertiary)]"
                      >
                        {t("taskBatchUseInput")}
                      </button>
                      <div className="flex-1" />
                      <button
                        type="button"
                        onClick={addBatchTask}
                        className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] font-medium text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)]"
                      >
                        <Plus className="h-3.5 w-3.5" />
                        {t("taskBatchAddTask")}
                      </button>
                    </div>

                    <div className="max-h-[55vh] overflow-y-auto pr-1">
                      <div className="grid gap-2">
                        {batchTasks.map((task, index) => (
                          <div
                            key={task.id}
                            className="rounded-lg border border-[var(--border-default)] bg-[var(--surface-primary)] p-3"
                          >
                            <div className="flex items-center gap-2">
                              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-[var(--surface-secondary)] text-[12px] font-medium text-[var(--text-secondary)]">
                                {index + 1}
                              </span>
                              <input
                                value={task.title}
                                onChange={(e) => updateBatchTask(task.id, { title: e.target.value })}
                                placeholder={t("taskBatchTaskTitlePlaceholder")}
                                className="ui-control min-w-0 flex-1 rounded-md border border-[var(--control-border)] bg-[var(--surface-primary)] px-2.5 py-1.5 text-[13px] text-[var(--text-primary)] outline-none focus:border-[var(--ring)] focus:ring-1 focus:ring-[var(--ring)]"
                              />
                              <Tooltip>
                                <TooltipTrigger asChild>
                                <button
                                type="button"
                                onClick={() => removeBatchTask(task.id)}
                                disabled={batchTasks.length <= 1}
                                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[var(--text-tertiary)] hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-tertiary)]"
                                aria-label={t("taskBatchRemoveTask")}
                              >
                                <Trash2 className="h-4 w-4" aria-hidden="true" />
                              </button>
                                </TooltipTrigger>
                                <TooltipContent>{t("taskBatchRemoveTask")}</TooltipContent>
                              </Tooltip>
                            </div>
                            <textarea
                              value={task.prompt}
                              onChange={(e) => updateBatchTask(task.id, { prompt: e.target.value })}
                              placeholder={t("taskBatchPromptPlaceholder")}
                              rows={3}
                              className="ui-control mt-2 w-full resize-none rounded-md border border-[var(--control-border)] bg-[var(--surface-primary)] px-2.5 py-2 text-[13px] leading-relaxed text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)] focus:border-[var(--ring)] focus:ring-1 focus:ring-[var(--ring)]"
                            />
                            <div className="mt-2 grid gap-2 sm:grid-cols-2">
                              <select
                                value={task.agent}
                                onChange={(e) => updateBatchTask(task.id, { agent: e.target.value })}
                                className="ui-control min-w-0 rounded-md border border-[var(--control-border)] bg-[var(--surface-primary)] px-2.5 py-1.5 text-[13px] text-[var(--text-primary)] outline-none focus:border-[var(--ring)] focus:ring-1 focus:ring-[var(--ring)]"
                              >
                                {visibleAgents.length === 0 && (
                                  <option value={task.agent}>{task.agent}</option>
                                )}
                                {visibleAgents.map((agent) => (
                                  <option key={agent.name} value={agent.name}>
                                    {agent.name}
                                  </option>
                                ))}
                              </select>
                              <select
                                value={task.model && task.provider_id ? `${task.provider_id}::${task.model}` : ""}
                                onChange={(e) => {
                                  const [providerId, modelId] = e.target.value.split("::");
                                  updateBatchTask(task.id, {
                                    provider_id: providerId || "",
                                    model: modelId || "",
                                  });
                                }}
                                className="ui-control min-w-0 rounded-md border border-[var(--control-border)] bg-[var(--surface-primary)] px-2.5 py-1.5 text-[13px] text-[var(--text-primary)] outline-none focus:border-[var(--ring)] focus:ring-1 focus:ring-[var(--ring)]"
                              >
                                <option value="">{t("taskBatchCurrentModel")}</option>
                                {providerModels.map((model) => (
                                  <option key={`${model.provider_id}:${model.id}`} value={`${model.provider_id}::${model.id}`}>
                                    {model.name}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="flex items-center justify-end gap-2 border-t border-[var(--border-default)] pt-3">
                      <button
                        type="button"
                        onClick={() => setBatchOpen(false)}
                        className="rounded-full px-3 py-1.5 text-[13px] font-medium text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)]"
                      >
                        {t("cancel")}
                      </button>
                      <button
                        type="button"
                        onClick={handleSendTaskBatch}
                        disabled={!canStartTaskBatch}
                        className="inline-flex items-center gap-1.5 rounded-full bg-[var(--text-primary)] px-3 py-1.5 text-[13px] font-medium text-[var(--surface-primary)] transition-opacity disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-tertiary)]"
                      >
                        <Play className="h-3.5 w-3.5" />
                        {t("taskBatchStart")}
                      </button>
                    </div>
                  </div>
                </PopoverContent>}
            </Popover>

            <div className="flex min-w-0 flex-1 items-center justify-end gap-1">
              <div className="flex min-w-0 flex-1 justify-end [&>button]:max-w-full [&>button]:min-h-11">
                <HeaderModelDropdown compact />
              </div>

              <div className="shrink-0">
                <ChatActions
                  isBusy={isGenerating || isCompacting}
                  canSend={
                    (input.trim().length > 0 || attachments.length > 0) &&
                    !isIndexing &&
                    !isInputDisabled &&
                    !imageNeedsVisionModel
                  }
                  onSend={handleSend}
                  onStop={onStop}
                  sendLabel={isGenerating
                    ? t("inputQueueAction")
                    : t("sendAction")}
                  // The longer inputQueueActionHint/sendActionHint stays in source.
                  sendHint={isGenerating ? t("inputQueueAction") : t("sendAction")}
                />
              </div>
            </div>
          </div>
          </div>

        </div>
      </div>
    </div>
    </TooltipProvider>
  );
}
