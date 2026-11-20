import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import test from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);
const sourceRoot = new URL("../../src/", import.meta.url);

// Load the actual store and its local imports with an isolated browser storage
// and build-time cloud flag. Zustand itself is the installed implementation.
function settingsHarness(isCloud = true, values = new Map<string, string>()) {
  const modules = new Map<string, { exports: unknown }>();
  const localStorage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
  function load(relativePath: string): unknown {
    const filename = fileURLToPath(new URL(relativePath, sourceRoot));
    const cached = modules.get(filename);
    if (cached) return cached.exports;
    const moduleRecord: { exports: unknown } = { exports: {} };
    modules.set(filename, moduleRecord);
    const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const execute = runInNewContext(`(function(require, module, exports) { ${compiled}\n})`, {
      process: { env: { NEXT_PUBLIC_CLOUD_MODE: String(isCloud) } },
      localStorage,
      console,
    }, { filename });
    execute((specifier: string) => specifier.startsWith("@/")
      ? load(`${specifier.slice(2)}.ts`)
      : require(specifier), moduleRecord, moduleRecord.exports);
    return moduleRecord.exports;
  }
  const identity = load("lib/cloud-session.ts") as typeof import("../../src/lib/cloud-session.ts");
  const store = (load("stores/settings-store.ts") as typeof import("../../src/stores/settings-store.ts")).useSettingsStore;
  return {
    store,
    values,
    async login(id = "user-a") {
      identity.activateCloudIdentity({ id, email: `${id}@example.test`, role: "user" }, "test-csrf");
      await store.persist.rehydrate();
    },
  };
}

function stateOf(harness: ReturnType<typeof settingsHarness>) {
  const { workMode, selectedAgent, safeMode, permissionPresets } = harness.store.getState();
  // Convert cross-realm objects to plain values for strict assertions.
  return JSON.parse(JSON.stringify({ workMode, selectedAgent, safeMode, permissionPresets }));
}

const auto = {
  workMode: "auto", selectedAgent: "build", safeMode: false,
  permissionPresets: { fileChanges: true, runCommands: true },
} as const;
const ask = {
  workMode: "ask", selectedAgent: "build", safeMode: false,
  permissionPresets: { fileChanges: false, runCommands: false },
} as const;
const plan = {
  workMode: "plan", selectedAgent: "plan", safeMode: true,
  permissionPresets: { fileChanges: false, runCommands: false },
} as const;

test("Online starts in Auto even before identity hydration and sends both existing presets", async () => {
  const harness = settingsHarness();
  assert.deepEqual(stateOf(harness), auto);
  assert.equal(harness.values.size, 0);
  await harness.login();
  assert.deepEqual(stateOf(harness), auto);
  const { selectedAgent, permissionPresets } = harness.store.getState();
  assert.deepEqual({ agent: selectedAgent, permission_presets: {
    file_changes: permissionPresets.fileChanges, run_commands: permissionPresets.runCommands,
  } }, { agent: "build", permission_presets: { file_changes: true, run_commands: true } });
});

test("Online migrates the old Ask default once without changing unrelated preferences", async () => {
  const values = new Map([["suxiaoyou:cloud:user-a:suxiaoyou-settings", JSON.stringify({
    version: 5, state: { ...ask, language: "zh", workspaceDirectory: "/workspace/private" },
  })]]);
  const harness = settingsHarness(true, values);
  await harness.login();
  assert.deepEqual(stateOf(harness), auto);
  assert.equal(harness.store.getState().language, "zh");
  assert.equal(harness.store.getState().workspaceDirectory, "/workspace/private");
  assert.equal(JSON.parse(values.get("suxiaoyou:cloud:user-a:suxiaoyou-settings")!).version, 6);
});

for (const previous of [plan, auto]) {
  test(`Online retains existing ${previous.workMode} mode during migration`, async () => {
    const values = new Map([["suxiaoyou:cloud:user-a:suxiaoyou-settings", JSON.stringify({ version: 5, state: previous })]]);
    const harness = settingsHarness(true, values);
    await harness.login();
    assert.deepEqual(stateOf(harness), previous);
  });
}

for (const chosen of [ask, plan]) {
  test(`manual ${chosen.workMode} selection survives a fresh page and hydration`, async () => {
    const harness = settingsHarness();
    await harness.login();
    harness.store.getState().setWorkMode(chosen.workMode);
    const refreshed = settingsHarness(true, harness.values);
    await refreshed.login();
    assert.deepEqual(stateOf(refreshed), chosen);
  });
}

test("another cloud account receives its own default without inheriting a manual mode", async () => {
  const first = settingsHarness();
  await first.login("user-a");
  first.store.getState().setWorkMode("ask");
  // Account changes perform a full navigation, which creates a fresh store.
  const second = settingsHarness(true, first.values);
  await second.login("user-b");
  assert.deepEqual(stateOf(second), auto);
  second.store.getState().setWorkMode("plan");
  const firstAgain = settingsHarness(true, first.values);
  await firstAgain.login("user-a");
  assert.deepEqual(stateOf(firstAgain), ask);
});

test("desktop retains its Ask default and existing Ask preference", async () => {
  const values = new Map([["suxiaoyou-settings", JSON.stringify({ version: 5, state: ask })]]);
  const harness = settingsHarness(false, values);
  assert.deepEqual(stateOf(harness), ask);
  await harness.login();
  assert.deepEqual(stateOf(harness), ask);
});
