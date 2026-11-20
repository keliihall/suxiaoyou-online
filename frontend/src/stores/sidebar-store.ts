"use client";

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import { userStorage } from "@/lib/cloud-session";
import { IS_DESKTOP, SIDEBAR_WIDTH, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH } from "@/lib/constants";

export type OrganizeMode = "by-project" | "chronological" | "chats-first";
export type SortBy = "created" | "updated";

/** Web keeps essential navigation available when the conversation list is folded. */
export const COLLAPSED_SIDEBAR_WIDTH = 64;

interface SidebarStore {
  /** Mobile drawer open state */
  isOpen: boolean;
  /** Desktop sidebar collapsed state */
  isCollapsed: boolean;
  /** Whether the search input is visible */
  isSearchOpen: boolean;
  searchQuery: string;
  /** Project directories that user has collapsed (default: expanded) */
  collapsedProjects: Record<string, boolean>;
  /** Command-palette search dialog open state */
  isSearchModalOpen: boolean;
  /** How session list is organized */
  organizeMode: OrganizeMode;
  /** Which timestamp sessions are sorted by */
  sortBy: SortBy;
  /** Current sidebar width (drag-resizable) */
  width: number;
  setOpen: (open: boolean) => void;
  /** Toggle desktop sidebar collapse */
  toggle: () => void;
  toggleSearch: () => void;
  setSearchQuery: (query: string) => void;
  toggleProjectCollapsed: (directory: string) => void;
  setSearchModalOpen: (open: boolean) => void;
  setOrganizeMode: (mode: OrganizeMode) => void;
  setSortBy: (sortBy: SortBy) => void;
  collapseAllProjects: (directories: string[]) => void;
  expandAllProjects: () => void;
  setWidth: (width: number) => void;
}

function clampWidth(w: number): number {
  return Math.max(SIDEBAR_MIN_WIDTH, Math.min(SIDEBAR_MAX_WIDTH, Math.round(w)));
}

export const useSidebarStore = create<SidebarStore>()(
  persist(
    (set) => ({
      isOpen: false,
      isCollapsed: !IS_DESKTOP,
      isSearchOpen: false,
      searchQuery: "",
      collapsedProjects: {},
      isSearchModalOpen: false,
      organizeMode: "by-project",
      sortBy: "updated",
      width: SIDEBAR_WIDTH,
      setOpen: (open) => set({ isOpen: open }),
      toggle: () => set((s) => ({ isCollapsed: !s.isCollapsed })),
      toggleSearch: () =>
        set((s) => ({
          isSearchOpen: !s.isSearchOpen,
          searchQuery: s.isSearchOpen ? "" : s.searchQuery,
        })),
      setSearchQuery: (query) => set({ searchQuery: query }),
      toggleProjectCollapsed: (directory) =>
        set((s) => {
          const next = { ...s.collapsedProjects };
          if (next[directory]) delete next[directory];
          else next[directory] = true;
          return { collapsedProjects: next };
        }),
      setSearchModalOpen: (open) => set({ isSearchModalOpen: open }),
      setOrganizeMode: (mode) => set({ organizeMode: mode }),
      setSortBy: (sortBy) => set({ sortBy }),
      collapseAllProjects: (directories) =>
        set(() => {
          const next: Record<string, boolean> = {};
          for (const d of directories) next[d] = true;
          return { collapsedProjects: next };
        }),
      expandAllProjects: () => set({ collapsedProjects: {} }),
      setWidth: (width) => set({ width: clampWidth(width) }),
    }),
    {
      name: "suxiaoyou-sidebar",
      storage: createJSONStorage(() => userStorage),
      version: 1,
      // Fold existing Web sidebars once; subsequent choices persist in version 1.
      migrate: (persisted) => {
        const saved = persisted as Partial<SidebarStore>;
        return { ...saved, isCollapsed: IS_DESKTOP ? saved.isCollapsed === true : true };
      },
      partialize: (s) => ({
        isCollapsed: s.isCollapsed,
        collapsedProjects: s.collapsedProjects,
        organizeMode: s.organizeMode,
        sortBy: s.sortBy,
        width: s.width,
      }),
      merge: (persisted, current) => {
        const merged = { ...current, ...(persisted as Partial<SidebarStore>) };
        return {
          ...merged,
          isCollapsed: merged.isCollapsed === true,
          width: clampWidth(merged.width ?? SIDEBAR_WIDTH),
        };
      },
    },
  ),
);
