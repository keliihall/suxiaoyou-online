"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { FolderPlus, LayoutDashboard, Loader2, LogOut, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Plug, Search, Settings, ShieldCheck, SquarePen, Timer, Wifi } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { useCloudIdentity } from "@/components/providers/cloud-identity-context";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { IS_CLOUD, logoutCloudWorkspace } from "@/lib/cloud-session";
import { browseDirectory } from "@/lib/upload";
import { cn } from "@/lib/utils";
import { useSidebarStore } from "@/stores/sidebar-store";

// Keep primary actions visually equal, including the current workbench and new chat.
const actionClass = "flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-[var(--text-secondary)] transition-colors hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] data-[state=open]:bg-[var(--sidebar-hover)] data-[state=open]:text-[var(--text-primary)]";
const iconClass = "h-[18px] w-[18px] shrink-0";

export function SidebarCollapseButton({ collapsed }: { collapsed: boolean }) {
  const { t } = useTranslation("common");
  const toggle = useSidebarStore((s) => s.toggle);
  const label = t(collapsed ? "openSidebar" : "toggleSidebar");

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          data-testid="sidebar-collapse-toggle"
          aria-label={label}
          aria-expanded={!collapsed}
          aria-controls="workbench-sidebar-content"
          onClick={(event) => {
            const button = event.currentTarget;
            const restoreFocus = button.ownerDocument.activeElement === button;
            const scope = button.closest("[data-sidebar-footer]") ?? button.parentElement;
            toggle();
            // The footer moves this control to match its visual and Tab order.
            if (restoreFocus) requestAnimationFrame(() => {
              const nextButton = scope?.querySelector<HTMLButtonElement>('[data-testid="sidebar-collapse-toggle"]');
              if (nextButton?.isConnected) nextButton.focus({ preventScroll: true });
            });
          }}
          className={actionClass}
        >
          {collapsed ? <PanelLeftOpen className={iconClass} /> : <PanelLeftClose className={iconClass} />}
        </button>
      </TooltipTrigger>
      <TooltipContent side={collapsed ? "right" : "top"}>{label}</TooltipContent>
    </Tooltip>
  );
}

export function WebSidebarNav({ collapsed }: { collapsed: boolean }) {
  const { t } = useTranslation("common");
  const pathname = usePathname();
  const router = useRouter();
  const setOpen = useSidebarStore((s) => s.setOpen);
  const setSearchModalOpen = useSidebarStore((s) => s.setSearchModalOpen);
  const [isPickingDirectory, setIsPickingDirectory] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const moreLabel = `${t("workbench")} · ${t("moreActions")}`;
  const tipSide = collapsed ? "right" : "bottom";

  const addProject = async () => {
    if (isPickingDirectory) return;
    setIsPickingDirectory(true);
    try {
      const path = await browseDirectory(t("addProject"));
      if (path) {
        setOpen(false);
        router.push(`/c/new?directory=${encodeURIComponent(path)}`);
      }
    } catch (error) {
      console.error("Failed to pick project directory:", error);
      toast.error(t("addProjectFailed"));
    } finally {
      setIsPickingDirectory(false);
    }
  };

  return (
    <div className={cn("shrink-0 pb-3", collapsed ? "px-2" : "px-3")}>
      <nav aria-label={t("workbench")} data-testid="sidebar-primary-actions" className={cn("flex gap-1", collapsed ? "flex-col items-center" : "items-center")}>
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" aria-label={t("searchChats")} onClick={() => setSearchModalOpen(true)} className={actionClass}>
              <Search className={iconClass} />
            </button>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>{t("searchChats")}</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Link href="/c/new" onClick={() => setOpen(false)} aria-label={t("workbench")} aria-current={pathname.startsWith("/c/") ? "page" : undefined} className={actionClass}>
              <LayoutDashboard className={iconClass} />
            </Link>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>{t("workbench")}</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Link href="/c/new" onClick={() => setOpen(false)} aria-label={t("newChat")} className={actionClass}>
              <SquarePen className={iconClass} />
            </Link>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>{t("newChat")}</TooltipContent>
        </Tooltip>
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <Tooltip open={menuOpen ? false : undefined}>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <button type="button" aria-label={moreLabel} data-testid="sidebar-primary-more" className={actionClass}>
                  <MoreHorizontal className={iconClass} />
                </button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side={tipSide}>{moreLabel}</TooltipContent>
          </Tooltip>
          <DropdownMenuContent side={collapsed ? "right" : "bottom"} align="start" sideOffset={8}>
            <DropdownMenuItem asChild className="min-h-11">
              <Link href="/automations" onClick={() => setOpen(false)} aria-current={pathname.startsWith("/automations") ? "page" : undefined}><Timer />{t("automations")}</Link>
            </DropdownMenuItem>
            <DropdownMenuItem asChild className="min-h-11">
              <Link href="/plugins" onClick={() => setOpen(false)} aria-current={pathname.startsWith("/plugins") ? "page" : undefined}><Plug />{t("plugins")}</Link>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled={isPickingDirectory} onSelect={() => { void addProject(); }} className="min-h-11">
              {isPickingDirectory ? <Loader2 className="animate-spin" /> : <FolderPlus />}{t("addProject")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </nav>
    </div>
  );
}

export function WebSidebarFooter({ collapsed }: { collapsed: boolean }) {
  const { t } = useTranslation("common");
  const user = useCloudIdentity();
  const setOpen = useSidebarStore((s) => s.setOpen);
  const [menuOpen, setMenuOpen] = useState(false);
  const accountLabel = user?.role === "admin" ? "云端管理" : "账户与额度";
  const moreLabel = `${accountLabel} · ${t("moreActions")}`;
  const tipSide = collapsed ? "right" : "top";

  return (
    <div data-sidebar-footer="" className={cn("flex shrink-0 items-center gap-1 border-t border-[var(--border-subtle)] py-2", collapsed ? "flex-col px-2" : "px-3")}>
      {!collapsed && <SidebarCollapseButton collapsed={false} />}
      <Tooltip>
        <TooltipTrigger asChild>
          <Link href="/settings" onClick={() => setOpen(false)} aria-label={t("settings")} className={actionClass}>
            <Settings className={iconClass} />
          </Link>
        </TooltipTrigger>
        <TooltipContent side={tipSide}>{t("settings")}</TooltipContent>
      </Tooltip>
      {IS_CLOUD && user && <>
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <Tooltip open={menuOpen ? false : undefined}>
            <TooltipTrigger asChild>
              <DropdownMenuTrigger asChild>
                <button type="button" aria-label={moreLabel} data-testid="sidebar-account-more" className={actionClass}>
                  <MoreHorizontal className={iconClass} />
                </button>
              </DropdownMenuTrigger>
            </TooltipTrigger>
            <TooltipContent side={tipSide}>{moreLabel}</TooltipContent>
          </Tooltip>
          <DropdownMenuContent side={collapsed ? "right" : "top"} align="start" sideOffset={8}>
            <DropdownMenuItem asChild className="min-h-11"><Link href="/remote" onClick={() => setOpen(false)}><Wifi />云端访问</Link></DropdownMenuItem>
            <DropdownMenuItem asChild className="min-h-11"><a href="/cloud?manage=1"><ShieldCheck />{accountLabel}</a></DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" onClick={() => void logoutCloudWorkspace()} aria-label="退出登录" className={actionClass}>
              <LogOut className={iconClass} />
            </button>
          </TooltipTrigger>
          <TooltipContent side={tipSide}>退出登录</TooltipContent>
        </Tooltip>
      </>}
      {collapsed && <SidebarCollapseButton collapsed />}
    </div>
  );
}
