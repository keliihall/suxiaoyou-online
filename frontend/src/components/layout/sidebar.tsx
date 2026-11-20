"use client";

import { Suspense } from "react";
import dynamic from "next/dynamic";
import { motion } from "framer-motion";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarHeader } from "./sidebar-header";
import { SessionList } from "./session-list";
import { SidebarFooter } from "./sidebar-footer";
import { SearchCommandDialog } from "./search-command-dialog";
import { SidebarResizeHandle } from "./sidebar-resize-handle";
import { COLLAPSED_SIDEBAR_WIDTH, useSidebarStore } from "@/stores/sidebar-store";
import { cn } from "@/lib/utils";
import { useIsMacOS } from "@/hooks/use-platform";
import { IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";

const SidebarNav = dynamic(
  () => import("./sidebar-nav").then((mod) => mod.SidebarNav),
  {
    ssr: false,
    loading: () => <div className="px-3 pt-1 pb-2" aria-hidden="true" />,
  },
);

export function Sidebar() {
  const isCollapsed = useSidebarStore((s) => s.isCollapsed);
  const width = useSidebarStore((s) => s.width);
  const isMac = useIsMacOS();
  const railCollapsed = !IS_DESKTOP && isCollapsed;
  const nativeHidden = IS_DESKTOP && isCollapsed;

  // macOS: sidebar extends to the window top (traffic lights overlay the
  // sidebar header). Windows/Linux: sit below the 32px custom title bar.
  const topOffset = IS_DESKTOP && !isMac ? TITLE_BAR_HEIGHT : 0;

  return (
    <TooltipProvider delayDuration={200}>
      <motion.aside
        aria-label="Chat sidebar"
        aria-hidden={nativeHidden ? true : undefined}
        inert={nativeHidden ? true : undefined}
        data-testid={railCollapsed ? "sidebar-rail" : undefined}
        data-collapsed={isCollapsed}
        className="workbench-sidebar sidebar-glass fixed inset-y-0 left-0 z-30 flex flex-col overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--sidebar-translucent-bg)] backdrop-blur-xl"
        style={IS_DESKTOP ? { top: topOffset } : undefined}
        initial={false}
        animate={{ width: nativeHidden ? 0 : railCollapsed ? COLLAPSED_SIDEBAR_WIDTH : width }}
        transition={{ type: "spring", damping: 30, stiffness: 300 }}
      >
        <SidebarHeader collapsed={railCollapsed} />
        <div id="workbench-sidebar-content" className="flex min-h-0 flex-1 flex-col overflow-y-auto">
          <SidebarNav collapsed={railCollapsed} compact={!IS_DESKTOP} />
          {/* Keep list state and pending actions mounted while the rail is shown. */}
          <div
            className={cn("flex min-h-0 flex-1 flex-col", railCollapsed && "hidden")}
            aria-hidden={railCollapsed ? true : undefined}
            inert={railCollapsed ? true : undefined}
          >
            <Suspense fallback={<div className="flex-1" />}>
              <SessionList />
            </Suspense>
          </div>
        </div>
        <SidebarFooter collapsed={railCollapsed} compact={!IS_DESKTOP} />
        {!isCollapsed && <SidebarResizeHandle />}
      </motion.aside>
      <SearchCommandDialog />
    </TooltipProvider>
  );
}
