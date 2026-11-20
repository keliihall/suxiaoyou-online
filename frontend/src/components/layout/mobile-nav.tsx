"use client";

import { useEffect, useState } from "react";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SidebarHeader } from "./sidebar-header";
import { SidebarNav } from "./sidebar-nav";
import { SessionList } from "./session-list";
import { SidebarFooter } from "./sidebar-footer";
import { ProjectsToolbar } from "./projects-toolbar";
import { useSidebarStore } from "@/stores/sidebar-store";
import { useIsMacOS } from "@/hooks/use-platform";
import { IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";

export function MobileNav() {
  const { isOpen, setOpen } = useSidebarStore();
  const isMac = useIsMacOS();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;

  return (
    <Sheet open={isOpen} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="icon-action lg:hidden fixed top-2 left-2 z-40 h-11 w-11 rounded-xl border border-[var(--border-default)] bg-[var(--surface-primary)] shadow-[var(--shadow-sm)]"
          style={IS_DESKTOP && !isMac ? { top: TITLE_BAR_HEIGHT + 12 } : undefined}
        >
          <Menu className="h-5 w-5" />
          <span className="sr-only">Toggle sidebar</span>
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="workbench-sidebar w-[min(320px,calc(100vw-40px))] p-0">
        <TooltipProvider delayDuration={200}>
          <div className="flex flex-col h-full">
            <SidebarHeader />
            <SidebarNav />
            <ProjectsToolbar variant="primary" />
            <SessionList />
            <SidebarFooter />
          </div>
        </TooltipProvider>
      </SheetContent>
    </Sheet>
  );
}
