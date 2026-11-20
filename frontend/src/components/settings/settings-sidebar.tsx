"use client";

import { useCallback } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { useIsMacOS } from "@/hooks/use-platform";
import { IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";
import { useSidebarStore } from "@/stores/sidebar-store";
import { SidebarResizeHandle } from "@/components/layout/sidebar-resize-handle";
import { SidebarHeader } from "@/components/layout/sidebar-header";
import { SidebarCollapseButton } from "@/components/layout/sidebar-web-controls";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { COLLAPSED_SIDEBAR_WIDTH } from "@/stores/sidebar-store";
import { SETTINGS_TABS, type SettingsTabId } from "./settings-tabs";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";

interface SettingsSidebarProps {
  returnHref?: string;
}

export function SettingsSidebar({
  returnHref = "/c/new",
}: SettingsSidebarProps) {
  const { t } = useTranslation(["settings"]);
  const router = useRouter();
  const searchParams = useSearchParams();
  const activeTab = (searchParams.get("tab") as SettingsTabId) || "general";
  const isMac = useIsMacOS();
  const sidebarWidth = useSidebarStore((s) => s.width);
  const isCollapsed = useSidebarStore((s) => s.isCollapsed);
  const collapsed = !IS_DESKTOP && isCollapsed;

  const navigateTab = useCallback(
    (tab: string) => {
      router.replace(`/settings?tab=${tab}`, { scroll: false });
    },
    [router],
  );

  const topOffset = IS_DESKTOP && !isMac ? TITLE_BAR_HEIGHT : 0;

  return (
    <TooltipProvider delayDuration={200}>
    <aside
      aria-label="Settings sidebar"
      data-testid={collapsed ? "sidebar-rail" : undefined}
      data-collapsed={collapsed}
      className="workbench-sidebar sidebar-glass fixed inset-y-0 left-0 z-30 flex flex-col overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--sidebar-translucent-bg)] backdrop-blur-xl"
      style={{ width: collapsed ? COLLAPSED_SIDEBAR_WIDTH : sidebarWidth, top: topOffset }}
    >
      {!collapsed && <SidebarResizeHandle />}
      <div
        data-tauri-drag-region
        data-testid="settings-drag-region"
        aria-hidden="true"
        className="shrink-0"
        style={{ height: IS_DESKTOP && isMac ? 48 : IS_DESKTOP ? 0 : 12 }}
      />

      {IS_DESKTOP ? (
        <div className="flex min-h-18 shrink-0 items-center gap-3 px-5 text-[16px] font-semibold tracking-tight text-[var(--text-primary)]"><span className="flex h-8 w-8 items-center justify-center"><SuxiaoyouLogo size={27} /></span>{t("settings:title", { defaultValue: "设置" })}</div>
      ) : (
        <SidebarHeader title={t("settings:title")} collapsed={collapsed} />
      )}

      <div id="workbench-sidebar-content" className="flex min-h-0 flex-1 flex-col">
      <div className={cn("shrink-0 pb-4", collapsed ? "px-2" : "px-3")}>
        <Tooltip open={collapsed ? undefined : false}>
        <TooltipTrigger asChild>
        <Link
          href={returnHref}
          data-testid="settings-back-to-app"
          aria-label={t("settings:backToApp")}
          className={cn(
            "flex min-h-11 items-center rounded-xl text-[13px] font-medium text-[var(--text-secondary)] outline-none transition-colors hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)] focus-visible:bg-[var(--sidebar-hover)] focus-visible:text-[var(--text-primary)] focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--sidebar-translucent-bg)]",
            collapsed ? "h-11 w-11 justify-center" : "w-full gap-3 border border-[var(--border-default)] bg-[var(--surface-primary)] px-3",
          )}
        >
          <ArrowLeft className="h-4 w-4 shrink-0" />
          {!collapsed && <span className="truncate">{t("settings:backToApp")}</span>}
        </Link>
        </TooltipTrigger>
        <TooltipContent side="right">{t("settings:backToApp")}</TooltipContent>
        </Tooltip>
      </div>

      <nav className={cn("min-h-0 flex-1 space-y-1 overflow-y-auto scrollbar-auto", collapsed ? "px-2" : "px-3")}>
        {SETTINGS_TABS.map(({ id, icon: Icon, labelKey }) => (
          <Tooltip key={id} open={collapsed ? undefined : false}>
          <TooltipTrigger asChild>
          <button
            type="button"
            onClick={() => navigateTab(id)}
            aria-label={t(`settings:${labelKey}`)}
            aria-current={activeTab === id ? "page" : undefined}
            className={cn(
              "flex min-h-11 items-center rounded-xl text-[13px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
              collapsed ? "h-11 w-11 justify-center" : "w-full gap-3 px-3 py-2",
              activeTab === id
                ? "bg-[var(--sidebar-active)] text-[var(--text-primary)] font-semibold"
                : "text-[var(--text-secondary)] hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)]",
            )}
          >
            <Icon className="h-[18px] w-[18px] shrink-0" />
            {!collapsed && <span className="truncate">{t(`settings:${labelKey}`)}</span>}
          </button>
          </TooltipTrigger>
          <TooltipContent side="right">{t(`settings:${labelKey}`)}</TooltipContent>
          </Tooltip>
        ))}
      </nav>
      </div>
      {!IS_DESKTOP && (
        <div className={cn("flex shrink-0 border-t border-[var(--border-subtle)] py-2", collapsed ? "justify-center px-2" : "px-3")}>
          <SidebarCollapseButton collapsed={collapsed} />
        </div>
      )}
    </aside>
    </TooltipProvider>
  );
}
