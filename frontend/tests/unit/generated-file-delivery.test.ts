import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { artifactTypeFromExtension } from "../../src/lib/artifacts.ts";
import { collectDeliveredFilePaths, fileCardsForTool, hasVisibleMessageOutput, presentedFilePathsForParts } from "../../src/lib/message-presentation.ts";
import type { ToolPart } from "../../src/types/message.ts";

function tool(name: string, metadata: Record<string, unknown>, status: ToolPart["state"]["status"] = "completed", input: Record<string, unknown> = {}): ToolPart {
  return {
    type: "tool", tool: name, call_id: `${name}-call`,
    state: { status, metadata, input, output: null, title: null, time_start: null, time_end: null, time_compacted: null },
  };
}

test("audio and video outputs have native preview types", () => {
  for (const extension of ["mp3", "wav", "m4a", "aac", "flac", "ogg", "opus"]) {
    assert.equal(artifactTypeFromExtension(`/workspace/output.${extension}`), "audio");
  }
  for (const extension of ["mp4", "webm", "mov", "mkv", "avi"]) {
    assert.equal(artifactTypeFromExtension(`/workspace/output.${extension}`), "video");
  }

  const filePreview = readFileSync(
    "src/components/artifacts/renderers/file-preview-renderer.tsx",
    "utf8",
  );
  const mediaPreview = readFileSync(
    "src/components/artifacts/renderers/media-renderer.tsx",
    "utf8",
  );
  assert.match(filePreview, /type === "audio" \|\| type === "video"/);
  assert.match(mediaPreview, /<audio[\s\S]*controls/);
  assert.match(mediaPreview, /<video[\s\S]*controls/);
  assert.match(mediaPreview, /MAX_MEDIA_PREVIEW_BYTES = 50 \* 1024 \* 1024/);
  assert.match(mediaPreview, /URL\.revokeObjectURL\(objectUrl\)/);
});

test("workspace mutations are process evidence, never automatic final cards", () => {
  for (const name of ["bash", "code_execute", "write", "edit", "office"]) {
    const part = tool(name, {
      file_path: "/workspace/final_report.docx",
      written_files: ["/workspace/final_report.docx", "/workspace/draft.md", "/workspace/render.png", "/workspace/data.json"],
    });
    assert.deepEqual(fileCardsForTool(part, new Set()), []);
    assert.equal(hasVisibleMessageOutput([part]), false);
  }
});

test("explicit plugin, image and presented outputs retain scripts and media without name heuristics", () => {
  const plugin = tool("custom_generator", {
    artifact_files: ["/workspace/requested_helper.py", { path: "/workspace/transcript.txt" }, { file_path: "/workspace/result.mp3" }],
    written_files: ["/workspace/draft.md"],
  });
  assert.deepEqual(collectDeliveredFilePaths(plugin), ["/workspace/requested_helper.py", "/workspace/transcript.txt", "/workspace/result.mp3"]);
  assert.equal(fileCardsForTool(plugin, new Set()).length, 3);
  assert.equal(hasVisibleMessageOutput([plugin]), true);
  for (const extension of ["png", "jpg", "jpeg", "gif", "webp", "bmp", "unlisted"]) {
    const filePath = `/workspace/temp.${extension}`;
    assert.deepEqual(fileCardsForTool(tool("image_generate", { file_path: filePath }), new Set()), [{ filePath, title: undefined }]);
  }
  const presented = tool("present_file", {}, "completed", { file_path: "/workspace/requested_script.py", title: "Script" });
  assert.deepEqual(fileCardsForTool(presented, new Set()), [{ filePath: "/workspace/requested_script.py", title: "Script" }]);
  assert.deepEqual(collectDeliveredFilePaths(tool("plugin", { artifact_delivery: true, file_path: "/workspace/result.custom" })), ["/workspace/result.custom"]);
});

test("failed or pending delivery cannot render a card or suppress an earlier successful output", () => {
  const path = "/workspace/result.png";
  const image = tool("image_generate", { file_path: path });
  for (const status of ["error", "pending", "running"] as const) {
    const failed = tool("present_file", { file_path: path }, status);
    assert.equal(hasVisibleMessageOutput([failed]), false);
    assert.deepEqual(fileCardsForTool(failed, new Set()), []);
    const markers = presentedFilePathsForParts([image, failed]);
    assert.equal(markers.size, 0);
    assert.equal(fileCardsForTool(image, markers).length, 1);
  }
  const success = tool("present_file", { file_path: path });
  const markers = presentedFilePathsForParts([image, success]);
  assert.equal(fileCardsForTool(image, markers).length, 0);
  assert.equal(fileCardsForTool(success, markers).length, 1);
});

test("history and stream rendering use the same delivery projection", () => {
  const messageContent = readFileSync(
    "src/components/messages/message-content.tsx",
    "utf8",
  );
  assert.match(messageContent, /hasVisibleMessageOutput\(parts\)/);

  const streamRegistry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  assert.match(streamRegistry, /Array\.isArray\(resultMetadata\.artifact_files\)/);
  assert.match(streamRegistry, /Array\.isArray\(resultMetadata\.written_files\)/);
  assert.match(streamRegistry, /"code_execute"/);
  assert.match(streamRegistry, /"present_file"/);
  assert.match(streamRegistry, /commitFileRequest\(fileRevision/);
});
