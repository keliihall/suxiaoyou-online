"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Plug, Search, Timer } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSidebarStore } from "@/stores/sidebar-store";
import { cn } from "@/lib/utils";
import { WebSidebarNav } from "./sidebar-web-controls";

interface SidebarNavProps {
  collapsed?: boolean;
  compact?: boolean;
}

export function SidebarNav({ collapsed = false, compact = false }: SidebarNavProps) {
  const { t } = useTranslation("common");
  const pathname = usePathname();
  const setSearchModalOpen = useSidebarStore((s) => s.setSearchModalOpen);
  const setOpen = useSidebarStore((s) => s.setOpen);
  const iconOnly = collapsed || compact;
  const tipSide = collapsed ? "right" : "bottom";
  const actionClass = cn(
    "flex min-h-11 shrink-0 items-center rounded-xl text-[13px] font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
    iconOnly ? "h-11 w-11 justify-center" : "w-full gap-3 px-3",
  );
  const items = [
    { href: "/c/new", icon: LayoutDashboard, label: t("workbench"), active: pathname.startsWith("/c/") },
    { href: "/automations", icon: Timer, label: t("automations"), active: pathname.startsWith("/automations") },
    { href: "/plugins", icon: Plug, label: t("plugins"), active: pathname.startsWith("/plugins") },
  ];

  // Native and mobile retain their full navigation; Web uses the compact rail.
  if (compact) return <WebSidebarNav collapsed={collapsed} />;

  return (
    <div className={cn("shrink-0 pb-3", collapsed ? "px-2" : "px-3")}>
      <nav
        aria-label={t("workbench")}
        className={cn("gap-1", collapsed ? "flex flex-col items-center" : compact ? "flex items-center" : "grid gap-2")}
      >
        <Tooltip open={iconOnly ? undefined : false}>
          <TooltipTrigger asChild>
            <button
              type="button"
              aria-label={t("searchChats")}
              onClick={() => setSearchModalOpen(true)}
              className={cn(actionClass, !iconOnly && "border border-[var(--border-default)] bg-[var(--surface-primary)]")}
            >
              <Search className="h-[18px] w-[18px] shrink-0" />
              {!iconOnly && <span>{t("searchChats")}</span>}
            </button>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>{t("searchChats")}</TooltipContent>
        </Tooltip>
        {items.map(({ href, icon: Icon, label, active }) => (
          <Tooltip key={href} open={iconOnly ? undefined : false}>
            <TooltipTrigger asChild>
              <Link
                href={href}
                onClick={() => setOpen(false)}
                aria-label={label}
                aria-current={active ? "page" : undefined}
                className={cn(actionClass, active && "bg-[var(--sidebar-active)] text-[var(--text-primary)]")}
              >
                <Icon className="h-[18px] w-[18px] shrink-0" />
                {!iconOnly && <span>{label}</span>}
              </Link>
            </TooltipTrigger>
            <TooltipContent side={tipSide}>{label}</TooltipContent>
          </Tooltip>
        ))}
      </nav>
    </div>
  );
}
