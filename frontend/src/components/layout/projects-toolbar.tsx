"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import {
  Check,
  FolderClosed,
  FolderPlus,
  ListFilter,
  Loader2,
  Maximize2,
  Minimize2,
  MessageSquare,
  Clock3,
  SquarePen,
} from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { browseDirectory } from "@/lib/upload";
import { cn } from "@/lib/utils";
import { useSidebarStore, type OrganizeMode, type SortBy } from "@/stores/sidebar-store";

interface ProjectsToolbarProps {
  projectDirectories?: string[];
  variant?: "projects" | "chats" | "primary";
  collapsed?: boolean;
  compact?: boolean;
}

export function ProjectsToolbar({ projectDirectories = [], variant = "projects", collapsed = false, compact = false }: ProjectsToolbarProps) {
  const { t } = useTranslation("common");
  const router = useRouter();
  const collapseAllProjects = useSidebarStore((s) => s.collapseAllProjects);
  const expandAllProjects = useSidebarStore((s) => s.expandAllProjects);
  const collapsedProjects = useSidebarStore((s) => s.collapsedProjects);
  const setOpen = useSidebarStore((s) => s.setOpen);
  const [isPickingDirectory, setIsPickingDirectory] = useState(false);

  const allCollapsed =
    projectDirectories.length > 0 &&
    projectDirectories.every((d) => collapsedProjects[d]);
  const toggleLabel = t(allCollapsed ? "expandAll" : "collapseAll");

  const handleToggleAll = () => {
    if (projectDirectories.length === 0) return;
    if (allCollapsed) {
      expandAllProjects();
    } else {
      collapseAllProjects(projectDirectories);
    }
  };

  const handleAddProject = async () => {
    if (isPickingDirectory) return;
    setIsPickingDirectory(true);
    try {
      const path = await browseDirectory(t("addProject"));
      if (path) {
        setOpen(false);
        router.push(`/c/new?directory=${encodeURIComponent(path)}`);
      }
    } catch (err) {
      console.error("Failed to pick project directory:", err);
      toast.error(t("addProjectFailed"));
    } finally {
      setIsPickingDirectory(false);
    }
  };

  const handleNewChat = () => {
    setOpen(false);
    router.push("/c/new");
  };

  if (variant === "primary") {
    return (
      <nav
        aria-label={`${t("addProject")} / ${t("newChat")}`}
        data-testid="sidebar-primary-actions"
        className={cn(
          "shrink-0 border-b border-[var(--border-subtle)]",
          collapsed ? "flex flex-col items-center gap-1 px-2 pb-3" : compact ? "flex items-center gap-1 px-3 pb-3" : "grid grid-cols-2 gap-2 px-3 pb-4",
        )}
      >
        <Tooltip open={compact || collapsed ? undefined : false}>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={handleAddProject}
              disabled={isPickingDirectory}
              className={cn(
                "flex min-h-11 min-w-0 items-center justify-center gap-2 rounded-xl text-[13px] font-medium text-[var(--text-secondary)] transition-colors hover:bg-[var(--sidebar-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-tertiary)]",
                compact || collapsed ? "h-11 w-11 shrink-0" : "border border-[var(--border-default)] bg-[var(--surface-primary)] px-2",
              )}
              aria-label={t("addProject")}
            >
              {isPickingDirectory ? (
                <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
              ) : (
                <FolderPlus className="h-3.5 w-3.5 shrink-0" />
              )}
              {!compact && !collapsed && <span className="truncate">{t("addProject")}</span>}
            </button>
          </TooltipTrigger>
          <TooltipContent side={collapsed ? "right" : "bottom"}>{t("addProject")}</TooltipContent>
        </Tooltip>
        <Tooltip open={compact || collapsed ? undefined : false}>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={handleNewChat}
              className={cn(
                "flex min-h-11 min-w-0 items-center justify-center gap-2 rounded-xl bg-[var(--brand-primary)] text-[13px] font-medium text-[var(--brand-primary-text)] transition-colors hover:bg-[var(--brand-primary-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--sidebar-bg)]",
                compact || collapsed ? "h-11 w-11 shrink-0" : "px-2",
              )}
              aria-label={t("newChat")}
            >
              <SquarePen className="h-3.5 w-3.5 shrink-0" />
              {!compact && !collapsed && <span className="truncate">{t("newChat")}</span>}
            </button>
          </TooltipTrigger>
          <TooltipContent side={collapsed ? "right" : "bottom"}>{t("newChat")}</TooltipContent>
        </Tooltip>
      </nav>
    );
  }

  if (variant === "chats") {
    return (
      <div className="flex items-center gap-0.5">
        <FilterPopover />
      </div>
    );
  }

  return (
    <div className="flex items-center gap-0.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={handleToggleAll}
            disabled={projectDirectories.length === 0}
            className="icon-action h-10 w-10 rounded-lg text-[var(--text-tertiary)] transition-colors hover:bg-[var(--sidebar-active)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-tertiary)]"
            aria-label={toggleLabel}
          >
            {allCollapsed ? (
              <Maximize2 className="h-3 w-3" />
            ) : (
              <Minimize2 className="h-3 w-3" />
            )}
          </button>
        </TooltipTrigger>
        <TooltipContent side="top">{toggleLabel}</TooltipContent>
      </Tooltip>

      <FilterPopover />
    </div>
  );
}

function FilterPopover() {
  const { t } = useTranslation("common");
  const organizeMode = useSidebarStore((s) => s.organizeMode);
  const setOrganizeMode = useSidebarStore((s) => s.setOrganizeMode);
  const sortBy = useSidebarStore((s) => s.sortBy);
  const setSortBy = useSidebarStore((s) => s.setSortBy);

  const organizeOptions: Array<{
    value: OrganizeMode;
    label: string;
    icon: React.ReactNode;
  }> = [
    { value: "by-project", label: t("organizeByProject"), icon: <FolderClosed className="h-3.5 w-3.5" /> },
    { value: "chronological", label: t("organizeChronological"), icon: <Clock3 className="h-3.5 w-3.5" /> },
    { value: "chats-first", label: t("organizeChatsFirst"), icon: <MessageSquare className="h-3.5 w-3.5" /> },
  ];

  const sortOptions: Array<{ value: SortBy; label: string }> = [
    { value: "created", label: t("sortByCreated") },
    { value: "updated", label: t("sortByUpdated") },
  ];

  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              className="icon-action h-10 w-10 rounded-lg text-[var(--text-tertiary)] transition-colors hover:bg-[var(--sidebar-active)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] data-[state=open]:bg-[var(--sidebar-active)] data-[state=open]:text-[var(--text-primary)]"
              aria-label={t("filterSortOrganize")}
            >
              <ListFilter className="h-3 w-3" />
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent side="top">{t("filterSortOrganize")}</TooltipContent>
      </Tooltip>
      <PopoverContent
        align="end"
        sideOffset={6}
        className="w-60 p-1"
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <SectionLabel>{t("organize")}</SectionLabel>
        {organizeOptions.map((opt) => (
          <FilterRow
            key={opt.value}
            selected={organizeMode === opt.value}
            onClick={() => setOrganizeMode(opt.value)}
            icon={opt.icon}
            label={opt.label}
          />
        ))}
        <div className="my-1 h-px bg-[var(--border-subtle)]" />
        <SectionLabel>{t("organizeSortBy")}</SectionLabel>
        {sortOptions.map((opt) => (
          <FilterRow
            key={opt.value}
            selected={sortBy === opt.value}
            onClick={() => setSortBy(opt.value)}
            label={opt.label}
          />
        ))}
      </PopoverContent>
    </Popover>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2 pb-1 pt-1.5 text-ui-3xs font-semibold uppercase tracking-[0.12em] text-[var(--text-tertiary)]">
      {children}
    </div>
  );
}

function FilterRow({
  selected,
  onClick,
  icon,
  label,
}: {
  selected: boolean;
  onClick: () => void;
  icon?: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={cn(
        "ui-control flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
        "text-[var(--text-secondary)] hover:bg-[var(--sidebar-active)] hover:text-[var(--text-primary)]",
      )}
    >
      {icon && <span className="shrink-0 text-[var(--text-tertiary)]">{icon}</span>}
      <span className="flex-1 truncate">{label}</span>
      {selected && <Check className="h-3.5 w-3.5 shrink-0 text-[var(--text-primary)]" />}
    </button>
  );
}
