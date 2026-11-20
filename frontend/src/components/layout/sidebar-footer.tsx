"use client";

import { LogOut, Settings, ShieldCheck, Wifi } from "lucide-react";
import { useTranslation } from "react-i18next";
import Link from "next/link";
import { useCloudIdentity } from "@/components/providers/cloud-identity-context";
import { IS_CLOUD, logoutCloudWorkspace } from "@/lib/cloud-session";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSidebarStore } from "@/stores/sidebar-store";
import { cn } from "@/lib/utils";
import { WebSidebarFooter } from "./sidebar-web-controls";

interface SidebarFooterProps {
  collapsed?: boolean;
  compact?: boolean;
}

export function SidebarFooter({ collapsed = false, compact = false }: SidebarFooterProps) {
  const { t } = useTranslation("common");
  const user = useCloudIdentity();
  const setOpen = useSidebarStore((s) => s.setOpen);
  const iconOnly = collapsed || compact;
  const tipSide = collapsed ? "right" : "top";
  const accountLabel = user?.role === "admin" ? "云端管理" : "账户与额度";
  const itemClass = cn(
    "flex min-h-11 shrink-0 items-center rounded-xl text-[13px] font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
    iconOnly ? "h-11 w-11 justify-center" : "w-full gap-3 px-3",
  );

  if (compact) return <WebSidebarFooter collapsed={collapsed} />;

  return (
    <div className={cn(
      "shrink-0 gap-1 border-t border-[var(--border-subtle)] py-3",
      collapsed ? "flex flex-col items-center px-2" : compact ? "flex items-center px-3" : "grid px-3",
    )}>
      <Tooltip open={iconOnly ? undefined : false}>
        <TooltipTrigger asChild>
          <Link href="/settings" onClick={() => setOpen(false)} aria-label={t("settings")} className={itemClass}>
            <Settings className="h-[18px] w-[18px] shrink-0" />
            {!iconOnly && <span>{t("settings")}</span>}
          </Link>
        </TooltipTrigger>
        <TooltipContent side={tipSide}>{t("settings")}</TooltipContent>
      </Tooltip>
      {IS_CLOUD && user && <>
        <Tooltip open={iconOnly ? undefined : false}>
          <TooltipTrigger asChild>
            <Link href="/remote" onClick={() => setOpen(false)} aria-label="云端访问" className={itemClass}>
              <Wifi className="h-[18px] w-[18px] shrink-0" />
              {!iconOnly && <span>云端访问</span>}
            </Link>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>云端访问</TooltipContent>
        </Tooltip>
        <Tooltip open={iconOnly ? undefined : false}>
          <TooltipTrigger asChild>
            <a href="/cloud?manage=1" aria-label={accountLabel} className={itemClass}>
              <ShieldCheck className="h-[18px] w-[18px] shrink-0" />
              {!iconOnly && <span>{accountLabel}</span>}
            </a>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>{accountLabel}</TooltipContent>
        </Tooltip>
        <Tooltip open={iconOnly ? undefined : false}>
          <TooltipTrigger asChild>
            <button type="button" onClick={() => void logoutCloudWorkspace()} className={itemClass} aria-label="退出登录">
              <LogOut className="h-[18px] w-[18px] shrink-0" />
              {!iconOnly && <span className="truncate">{user.display_name || user.email}</span>}
            </button>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>退出登录</TooltipContent>
        </Tooltip>
      </>}
    </div>
  );
}
