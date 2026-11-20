import type { StepFinishPart, ToolPart } from "../types/message.ts";

export interface ActivityCompletionState {
  toolParts: ToolPart[];
  stepParts: Array<{ type: string } | StepFinishPart>;
  hasVisibleOutput?: boolean;
  isAwaitingConfirmation?: boolean;
  /** Authoritative lifecycle state supplied by history or the terminal event. */
  isTerminal?: boolean;
}

function latestStepFinish(data: ActivityCompletionState): StepFinishPart | undefined {
  for (let index = data.stepParts.length - 1; index >= 0; index -= 1) {
    const part = data.stepParts[index];
    // A new step supersedes the previous attempt's terminal boundary.
    if (part.type === "step-start") return undefined;
    if (part.type === "step-finish") return part as StepFinishPart;
  }
  return undefined;
}

export function hasTerminalStepFinish(data: ActivityCompletionState): boolean {
  const finish = latestStepFinish(data);
  return finish !== undefined && finish.reason !== "tool_use";
}

/**
 * A completed activity is not necessarily a successful one. Keep this
 * separate from `isActivityComplete` so individual tool detours never inherit
 * task-level failure styling, while a genuinely failed response still does.
 */
export function isActivityFailed(data: ActivityCompletionState): boolean {
  return latestStepFinish(data)?.reason === "error";
}

export function hasRunningActivityTools(data: ActivityCompletionState): boolean {
  return data.toolParts.some(
    (tool) => tool.state.status === "running" || tool.state.status === "pending",
  );
}

/**
 * Terminal lifecycle evidence is authoritative.  A dropped tool-result event
 * must not leave a completed response displaying "Finalizing" forever.
 */
export function isActivityComplete(data: ActivityCompletionState): boolean {
  if (data.isAwaitingConfirmation) return false;
  // An explicitly live response may already contain output from an earlier
  // attempt. That output does not complete the step that is now running.
  if (data.isTerminal !== undefined) return data.isTerminal;
  if (hasTerminalStepFinish(data)) return true;
  return !!data.hasVisibleOutput && !hasRunningActivityTools(data);
}
