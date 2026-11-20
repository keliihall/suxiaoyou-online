export type WorkMode = "plan" | "ask" | "auto";

export interface WorkModeSettings {
  workMode: WorkMode;
  selectedAgent: string;
  safeMode: boolean;
  permissionPresets: { fileChanges: boolean; runCommands: boolean };
}

export function settingsForWorkMode(mode: WorkMode): WorkModeSettings {
  return {
    workMode: mode,
    selectedAgent: mode === "plan" ? "plan" : "build",
    safeMode: mode === "plan",
    permissionPresets: {
      fileChanges: mode === "auto",
      runCommands: mode === "auto",
    },
  };
}

export function defaultWorkModeSettings(isCloud: boolean): WorkModeSettings {
  return settingsForWorkMode(isCloud ? "auto" : "ask");
}

export function migrateCloudWorkModeDefault<T extends Partial<WorkModeSettings>>(
  state: T,
  persistedVersion: number,
  isCloud: boolean,
): T {
  if (!isCloud || persistedVersion >= 6 || state.workMode === "auto") return state;
  if (state.workMode === "plan" || state.selectedAgent === "plan" || state.safeMode === true) {
    return { ...state, ...settingsForWorkMode("plan") };
  }
  // v5 did not distinguish its inherited Ask default from an explicit Ask
  // choice. Apply the requested Online Auto default once to that old state;
  // v6 and later retain every manual mode selection, including Ask.
  return { ...state, ...settingsForWorkMode("auto") };
}
