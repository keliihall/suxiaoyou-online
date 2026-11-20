"use client";

import { ArrowUp, Square } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger, TooltipProvider } from "@/components/ui/tooltip";

interface ChatActionsProps {
  isBusy: boolean;
  canSend: boolean;
  onSend: () => void;
  onStop: () => void;
  sendLabel?: string;
  sendHint?: string;
}

export function ChatActions({ isBusy, canSend, onSend, onStop, sendLabel, sendHint }: ChatActionsProps) {
  const { t } = useTranslation("chat");
  const resolvedSendLabel = sendLabel ?? t("sendAction");
  const resolvedSendHint = sendHint ?? t("sendActionHint");
  const actionLabel = isBusy ? t("stopAction") : resolvedSendLabel;

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className={`group h-11 min-h-11 w-11 shrink-0 rounded-full bg-transparent p-0 hover:bg-transparent disabled:bg-transparent disabled:opacity-100 ${isBusy ? "[&_svg]:size-3" : "[&_svg]:size-4"}`}
            onClick={isBusy ? onStop : onSend}
            disabled={!isBusy && !canSend}
            aria-label={actionLabel}
            data-testid="chat-primary-action"
            data-action={isBusy ? "stop" : "send"}
          >
            {/* A compact visual circle retains a full-size touch target. */}
            <span className="flex size-8 items-center justify-center rounded-full bg-[var(--brand-primary)] text-[var(--brand-primary-text)] transition-colors group-hover:bg-[var(--brand-primary-hover)] group-disabled:bg-[var(--surface-tertiary)] group-disabled:text-[var(--text-tertiary)]" aria-hidden="true">
              {isBusy
                ? <Square className="size-3 fill-current" />
                : <ArrowUp className="size-4" />}
            </span>
            <span className="sr-only">{actionLabel}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent>{isBusy ? actionLabel : resolvedSendHint}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
