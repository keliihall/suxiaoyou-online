import type { PartData, ToolPart } from "../types/message.ts";

export const VISIBLE_TOOL_PARTS = new Set([
  "artifact",
  "present_file",
  "submit_plan",
]);

export const FILE_CARD_TOOL_PARTS = new Set([
  "present_file",
  "write",
  "edit",
  "image_generate",
  "office",
  "code_execute",
  "bash",
]);

export const GENERATED_FILE_TOOL_PARTS = new Set([
  "write",
  "edit",
  "image_generate",
  "office",
  "code_execute",
  "bash",
]);

/** Delivery intent, mirrored by backend session/file_delivery.py.
 * A workspace write is process evidence; only a successful explicit delivery
 * becomes a card. No filename or extension can disqualify a requested output.
 */
export function collectDeliveredFilePaths(part: ToolPart): string[] {
  if (part.state.status !== "completed") return [];
  const metadata = (part.state.metadata ?? {}) as Record<string, unknown>;
  const paths: string[] = [];
  if (Array.isArray(metadata.artifact_files)) {
    for (const value of metadata.artifact_files) {
      const path = value && typeof value === "object"
        ? (value as Record<string, unknown>).path || (value as Record<string, unknown>).file_path
        : value;
      if (typeof path === "string" && path) paths.push(path);
    }
  }
  const primary = metadata.file_path || (part.tool === "present_file" ? part.state.input?.file_path : undefined);
  if (
    (part.tool === "present_file" || part.tool === "image_generate" || metadata.artifact_delivery === true) &&
    typeof primary === "string" && primary
  ) paths.push(primary);
  return [...new Set(paths)];
}

export function collectToolFilePaths(part: ToolPart): string[] {
  const input = (part.state.input ?? {}) as Record<string, unknown>;
  const metadata = (part.state.metadata ?? {}) as Record<string, unknown>;

  if (part.tool === "present_file") {
    const filePath = metadata.file_path || input.file_path;
    return typeof filePath === "string" ? [filePath] : [];
  }

  const declared: string[] = [];
  if (
    (
      part.tool === "write" ||
      part.tool === "edit" ||
      part.tool === "image_generate" ||
      part.tool === "office"
    ) &&
    typeof metadata.file_path === "string"
  ) {
    declared.push(metadata.file_path);
  }

  for (const key of ["artifact_files", "written_files"] as const) {
    const values = metadata[key];
    if (!Array.isArray(values)) continue;
    for (const value of values) {
      if (typeof value === "string" && value) {
        declared.push(value);
      } else if (value && typeof value === "object") {
        const item = value as Record<string, unknown>;
        const path = item.path || item.file_path;
        if (typeof path === "string" && path) declared.push(path);
      }
    }
  }

  if (typeof metadata.file_path === "string" && metadata.artifact_delivery === true) {
    declared.push(metadata.file_path);
  }

  return [...new Set(declared)];
}

export function isFileCardToolPart(part: PartData): boolean {
  return (
    part.type === "tool" &&
    (
      FILE_CARD_TOOL_PARTS.has((part as ToolPart).tool) ||
      collectDeliveredFilePaths(part as ToolPart).length > 0
    )
  );
}

export function fileCardsForTool(part: ToolPart, presentedFilePaths: Set<string>) {
  const input = (part.state.input ?? {}) as Record<string, unknown>;
  const metadata = (part.state.metadata ?? {}) as Record<string, unknown>;
  const title =
    typeof metadata.title === "string"
      ? metadata.title
      : typeof input.title === "string"
        ? input.title
        : undefined;

  return collectDeliveredFilePaths(part)
    .filter((filePath) => part.tool === "present_file" || !presentedFilePaths.has(filePath))
    .map((filePath) => ({ filePath, title: part.tool === "present_file" ? title : undefined }));
}

export function presentedFilePathsForParts(parts: PartData[]): Set<string> {
  const paths = new Set<string>();
  for (const part of parts) {
    if (part.type !== "tool" || part.tool !== "present_file") continue;
    for (const filePath of collectDeliveredFilePaths(part)) paths.add(filePath);
  }
  return paths;
}

/** True when MessageContent will render something outside the activity trace. */
export function hasVisibleMessageOutput(parts: PartData[]): boolean {
  const presentedFilePaths = presentedFilePathsForParts(parts);
  return parts.some((part) => {
    if (
      part.type === "text" ||
      part.type === "file" ||
      part.type === "compaction" ||
      part.type === "subtask"
    ) {
      return true;
    }
    if (part.type !== "tool") return false;
    if (part.tool === "present_file") return fileCardsForTool(part, presentedFilePaths).length > 0;
    if (part.tool === "artifact" && part.state.status === "error") return false;
    if (VISIBLE_TOOL_PARTS.has(part.tool)) return true;
    return (
      (GENERATED_FILE_TOOL_PARTS.has(part.tool) || collectToolFilePaths(part).length > 0) &&
      fileCardsForTool(part, presentedFilePaths).length > 0
    );
  });
}
