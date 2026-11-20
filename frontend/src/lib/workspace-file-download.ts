import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import { base64ToBlob, downloadBlob } from "@/lib/browser-files";

interface BinaryContentResponse {
  content_base64: string;
  name: string;
  mime_type: string;
  size: number;
}

/** Download authorized workspace bytes rather than opening a server path. */
export async function downloadWorkspaceFile(
  filePath: string,
  workspace: string | null,
): Promise<void> {
  const response = await api.post<BinaryContentResponse>(
    API.FILES.CONTENT_BINARY,
    { path: filePath, workspace },
    { timeoutMs: 120_000 },
  );
  const fileName = filePath.split(/[\\/]/).pop() || "download";
  downloadBlob(
    base64ToBlob(response.content_base64, response.mime_type),
    response.name || fileName,
  );
}
