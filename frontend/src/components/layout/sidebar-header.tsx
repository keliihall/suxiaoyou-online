"use client";

import { IS_DESKTOP } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";
import { useTranslation } from "react-i18next";

interface SidebarHeaderProps {
  collapsed?: boolean;
  title?: string;
}

export function SidebarHeader({ collapsed = false, title }: SidebarHeaderProps) {
  const { t } = useTranslation("common");

  return (
    <div className="shrink-0">
      {/* Native window controls retain their own drag strip and toggle. */}
      {IS_DESKTOP && <div data-tauri-drag-region aria-hidden="true" className="h-14" />}
      <div
        data-tauri-drag-region={IS_DESKTOP ? "" : undefined}
        className={cn(
          "flex items-center gap-2",
          IS_DESKTOP ? "min-h-18 px-5" : "min-h-16 px-3",
          collapsed && "justify-center px-2",
        )}
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center">
          <SuxiaoyouLogo size={27} />
        </span>
        {!collapsed && (
            <span className="min-w-0 flex-1 truncate text-[14px] font-semibold tracking-tight text-[var(--text-primary)]">
              {title ?? t("appName")}
            </span>
        )}
      </div>
    </div>
  );
}
