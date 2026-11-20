import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  hasTerminalStepFinish,
  isActivityComplete,
  isActivityFailed,
} from "../../src/lib/activity-state.ts";
import { hasVisibleMessageOutput } from "../../src/lib/message-presentation.ts";
import type { ToolPart } from "../../src/types/message.ts";

function tool(
  name: string,
  status: ToolPart["state"]["status"],
  metadata: Record<string, unknown> = {},
): ToolPart {
  return {
    type: "tool",
    tool: name,
    call_id: `${name}-call`,
    state: {
      status,
      input: {},
      output: null,
      metadata,
      title: null,
      time_start: null,
      time_end: null,
      time_compacted: null,
    },
  };
}

test("explicit deliverables count as visible output while process writes remain in activity", () => {
  assert.equal(
    hasVisibleMessageOutput([
      tool("bash", "completed", {
        artifact_files: ["/workspace/suxiaoyou_written/news.mp3"],
      }),
    ]),
    true,
  );
  assert.equal(
    hasVisibleMessageOutput([
      tool("bash", "completed", {
        written_files: ["/workspace/suxiaoyou_written/generate_audio_helper.py"],
      }),
    ]),
    false,
  );
});

test("terminal lifecycle evidence wins over stale running tool state", () => {
  const staleTool = tool("bash", "running");
  assert.equal(
    isActivityComplete({
      toolParts: [staleTool],
      stepParts: [
        { type: "step-finish", reason: "stop", tokens: {}, cost: 0 },
      ],
    }),
    true,
  );
  assert.equal(
    isActivityComplete({
      toolParts: [staleTool],
      stepParts: [],
      isTerminal: true,
    }),
    true,
  );
  assert.equal(
    isActivityComplete({
      toolParts: [staleTool],
      stepParts: [],
      hasVisibleOutput: true,
    }),
    false,
  );
});

test("activity outcome follows the final terminal step instead of tool detours", () => {
  assert.equal(
    isActivityFailed({
      toolParts: [tool("web_search", "error"), tool("web_fetch", "completed")],
      stepParts: [
        { type: "step-finish", reason: "error", tokens: {}, cost: 0 },
        { type: "step-finish", reason: "stop", tokens: {}, cost: 0 },
      ],
    }),
    false,
  );
  assert.equal(
    isActivityFailed({
      toolParts: [tool("web_search", "error")],
      stepParts: [
        { type: "step-finish", reason: "error", tokens: {}, cost: 0 },
      ],
    }),
    true,
  );
});

test("a resumed step does not inherit an earlier attempt's task failure", () => {
  const activity = {
    toolParts: [tool("bash", "running")],
    stepParts: [
      { type: "step-finish", reason: "error", tokens: {}, cost: 0 } as const,
      { type: "step-start" },
    ],
    hasVisibleOutput: true,
  };
  const live = { ...activity, isTerminal: hasTerminalStepFinish(activity) };
  assert.equal(live.isTerminal, false);
  assert.equal(isActivityComplete(live), false);
  assert.equal(isActivityFailed(live), false);

  // Between STEP_START and the next tool call, old text is still visible.
  assert.equal(isActivityComplete({ ...live, toolParts: [] }), false);
});

test("a continued tool step supersedes an old terminal error", () => {
  const activity = {
    toolParts: [tool("read", "completed")],
    stepParts: [
      { type: "step-finish", reason: "error", tokens: {}, cost: 0 } as const,
      { type: "step-start" },
      { type: "step-finish", reason: "tool_use", tokens: {}, cost: 0 } as const,
    ],
  };
  assert.equal(hasTerminalStepFinish(activity), false);
  assert.equal(isActivityFailed(activity), false);
});

test("a genuine final error and authoritative terminal state remain visible", () => {
  const failed = {
    toolParts: [tool("bash", "running")],
    stepParts: [
      { type: "step-start" },
      { type: "step-finish", reason: "error", tokens: {}, cost: 0 } as const,
    ],
  };
  assert.equal(hasTerminalStepFinish(failed), true);
  assert.equal(isActivityComplete(failed), true);
  assert.equal(isActivityFailed(failed), true);
  assert.equal(isActivityComplete({ ...failed, isTerminal: true }), true);
  assert.equal(isActivityComplete({ ...failed, isTerminal: true, isAwaitingConfirmation: true }), false);
});

test("DONE clears generation before any database reconciliation", () => {
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const start = registry.indexOf("onCurrent(SSE_EVENTS.DONE");
  const end = registry.indexOf("const handleAgentError", start);
  const doneHandler = registry.slice(start, end);

  assert.match(doneHandler, /finishCurrentGeneration\(\)/);
  assert.doesNotMatch(doneHandler, /finishFromDatabase/);
  assert.ok(
    doneHandler.indexOf("finishCurrentGeneration()") <
      doneHandler.indexOf("invalidateQueries"),
  );
});
