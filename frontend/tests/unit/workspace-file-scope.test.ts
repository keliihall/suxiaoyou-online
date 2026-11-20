import assert from "node:assert/strict";
import test from "node:test";
import { useWorkspaceStore } from "../../src/stores/workspace-store.ts";
import { StreamLeaseRegistry } from "../../src/lib/stream-lifecycle.ts";
import { readFileSync } from "node:fs";

const delivery = { name: "final.txt", path: "/workspace/final.txt", type: "deliverable" as const };
const working = { name: "draft.md", path: "/workspace/draft.md", type: "working" as const };

test("file scope defaults to delivery and resets when entering another session", () => {
  const store = useWorkspaceStore.getState();
  store.resetForSession();
  assert.equal(useWorkspaceStore.getState().fileScope, "deliverables");
  store.setFileScope("all");
  store.setWorkspaceFiles([delivery, working]);
  const pending = store.beginFileRequest();
  store.resetForSession();
  store.commitFileRequest(pending, [delivery, working]);
  assert.equal(useWorkspaceStore.getState().fileScope, "deliverables");
  assert.deepEqual(useWorkspaceStore.getState().workspaceFiles, []);
});

test("scope round trips invalidate old requests even when the final scope matches", () => {
  const store = useWorkspaceStore.getState();
  store.resetForSession();
  const staleDeliveryRequest = store.beginFileRequest();
  store.setFileScope("all");
  const staleAllRequest = store.beginFileRequest();
  store.setFileScope("deliverables");
  const freshRequest = store.beginFileRequest();
  store.commitFileRequest(freshRequest, [delivery]);
  store.commitFileRequest(staleAllRequest, [delivery, working]);
  store.commitFileRequest(staleDeliveryRequest, []);
  assert.deepEqual(useWorkspaceStore.getState().workspaceFiles, [delivery]);
});

test("a delayed hydration response cannot erase a newer successful stream delivery", () => {
  const store = useWorkspaceStore.getState();
  store.resetForSession();
  const hydration = store.beginFileRequest();
  const afterPresentation = store.beginFileRequest();
  store.commitFileRequest(afterPresentation, [delivery]);
  store.commitFileRequest(hydration, []);
  assert.deepEqual(useWorkspaceStore.getState().workspaceFiles, [delivery]);
});

test("the last presentation response remains committable after DONE releases its stream lease", async () => {
  const store = useWorkspaceStore.getState();
  store.resetForSession();
  const streams = new StreamLeaseRegistry();
  const stream = streams.expect("session", "last-stream");
  assert.equal(streams.isCurrent(stream), true);
  const revision = store.beginFileRequest();
  let resolveResponse!: (files: typeof delivery[]) => void;
  const httpResponse = new Promise<typeof delivery[]>((resolve) => { resolveResponse = resolve; });
  const refresh = httpResponse.then((files) => store.commitFileRequest(revision, files));
  streams.clear("session", stream); // DONE's stopCurrentStream lifecycle.
  assert.equal(streams.isCurrent(stream), false);
  resolveResponse([delivery]);
  await refresh;
  assert.deepEqual(useWorkspaceStore.getState().workspaceFiles, [delivery]);

  // The real stream callback must use that independent file-request lease,
  // rather than discard the Promise because DONE closed the EventSource.
  const source = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const fileRefresh = source.slice(source.indexOf("const fileScope = useWorkspaceStore.getState().fileScope"));
  const callback = fileRefresh.slice(fileRefresh.indexOf(".then((res) => {"), fileRefresh.indexOf("}).catch"));
  assert.match(callback, /commitFileRequest\(fileRevision/);
  assert.doesNotMatch(callback, /isCurrentGeneration/);
});
