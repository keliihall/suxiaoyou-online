"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import { IS_CLOUD } from "@/lib/cloud-session";
import { base64ToBlob, downloadBlob } from "@/lib/browser-files";
import type {
  WorkspaceMemoryResponse,
  WorkspaceMemoryListItem,
  WorkspaceMemoryUpdate,
} from "@/types/workspace-memory";

export function useWorkspaceMemory(
  workspacePath: string | null,
  opts?: { refetchInterval?: number | false },
) {
  return useQuery({
    queryKey: queryKeys.workspaceMemory(workspacePath ?? ""),
    queryFn: () =>
      api.get<WorkspaceMemoryResponse>(
        `${API.WORKSPACE_MEMORY.BASE}?workspace_path=${encodeURIComponent(workspacePath!)}`,
      ),
    enabled: !!workspacePath,
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: opts?.refetchInterval ?? false,
  });
}

export function useWorkspaceMemoryList() {
  return useQuery({
    queryKey: queryKeys.workspaceMemoryList,
    queryFn: () =>
      api.get<WorkspaceMemoryListItem[]>(API.WORKSPACE_MEMORY.LIST),
    staleTime: 0,
    refetchOnMount: "always",
  });
}

export function useUpdateWorkspaceMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: WorkspaceMemoryUpdate) =>
      api.put<{ status: string }>(API.WORKSPACE_MEMORY.BASE, data),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.workspaceMemory(variables.workspace_path),
      });
      queryClient.invalidateQueries({
        queryKey: queryKeys.workspaceMemoryList,
      });
    },
  });
}

export function useDeleteWorkspaceMemory() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (workspacePath: string) =>
      api.delete<{ removed: boolean }>(
        `${API.WORKSPACE_MEMORY.BASE}?workspace_path=${encodeURIComponent(workspacePath)}`,
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: queryKeys.workspaceMemoryList,
      });
    },
  });
}

export function useRefreshWorkspaceMemory() {
  return useMutation({
    mutationFn: (workspacePath: string) =>
      api.post<{ status: string }>(
        `${API.WORKSPACE_MEMORY.REFRESH}?workspace_path=${encodeURIComponent(workspacePath)}`,
      ),
  });
}

export function useExportWorkspaceMemory() {
  return useMutation({
    mutationFn: async (workspacePath: string) => {
      const result = await api.post<{ exported_to: string }>(
        `${API.WORKSPACE_MEMORY.EXPORT}?workspace_path=${encodeURIComponent(workspacePath)}`,
      );
      if (IS_CLOUD) {
        // The original export writes into the tenant workspace. Retrieve that
        // exact file through the authenticated API for a real browser download.
        const file = await api.post<{ content_base64: string; mime_type: string; name?: string }>(
          API.FILES.CONTENT_BINARY, { path: result.exported_to, workspace: workspacePath },
        );
        downloadBlob(base64ToBlob(file.content_base64, file.mime_type), file.name || "memory.md");
      }
      return result;
    },
  });
}
