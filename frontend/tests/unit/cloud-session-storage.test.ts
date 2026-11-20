import assert from "node:assert/strict";
import test from "node:test";

process.env.NEXT_PUBLIC_CLOUD_MODE = "true";
const { activateCloudIdentity, userStorage, userStorageKey, cloudAuthHeaders } = await import("../../src/lib/cloud-session.ts");
const records = new Map<string, string>();
Object.defineProperty(globalThis, "localStorage", { value: {
  getItem: (key: string) => records.get(key) ?? null,
  setItem: (key: string, value: string) => { records.set(key, value); },
  removeItem: (key: string) => { records.delete(key); },
}, configurable: true });

test("anonymous cloud UI cannot read the previous desktop preference namespace", () => {
  records.set("suxiaoyou-settings", "desktop-private-rule");
  assert.equal(userStorageKey("suxiaoyou-settings"), null);
  assert.equal(userStorage.getItem("suxiaoyou-settings"), null);
  userStorage.setItem("suxiaoyou-settings", "anonymous-write");
  assert.equal(records.get("suxiaoyou-settings"), "desktop-private-rule");
});

test("account switches isolate drafts, permission preferences and pending request IDs", () => {
  activateCloudIdentity({ id: "account-a", email: "a@example.test", role: "user" }, "csrf-a");
  for (const key of ["suxiaoyou-settings", "suxiaoyou-drafts", "suxiaoyou:pending-prompt:v1:new"]) userStorage.setItem(key, `private-a:${key}`);
  activateCloudIdentity({ id: "account-b", email: "b@example.test", role: "user" }, "csrf-b");
  for (const key of ["suxiaoyou-settings", "suxiaoyou-drafts", "suxiaoyou:pending-prompt:v1:new"]) assert.equal(userStorage.getItem(key), null);
  userStorage.setItem("suxiaoyou-drafts", "private-b");
  activateCloudIdentity({ id: "account-a", email: "a@example.test", role: "user" }, "csrf-a");
  assert.equal(userStorage.getItem("suxiaoyou-drafts"), "private-a:suxiaoyou-drafts");
  assert.equal(records.get("suxiaoyou:cloud:account-b:suxiaoyou-drafts"), "private-b");
});

test("only mutations carry the current memory-only CSRF token", () => {
  assert.deepEqual(cloudAuthHeaders("GET"), {});
  assert.deepEqual(cloudAuthHeaders("HEAD"), {});
  assert.deepEqual(cloudAuthHeaders("POST"), { "X-CSRF-Token": "csrf-a" });
  assert.deepEqual(cloudAuthHeaders("DELETE"), { "X-CSRF-Token": "csrf-a" });
  assert.equal([...records.values()].some(value => value.includes("csrf-a")), false);
});
