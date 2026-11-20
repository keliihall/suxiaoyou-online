/** Cloud identity lives in memory; only user-scoped UI preferences use storage. */
export const IS_CLOUD = process.env.NEXT_PUBLIC_CLOUD_MODE === "true";

export interface CloudIdentity {
  id: string;
  email: string;
  display_name?: string;
  role: string;
}

let identity: CloudIdentity | null = null;
let csrf = "";
let navigatingAway = false;
const requests = new Set<AbortController>();

export function getCloudIdentity() { return identity; }
export function setCloudCsrfToken(token: string) { csrf = token; }
export function activateCloudIdentity(user: CloudIdentity, token: string) {
  identity = user;
  csrf = token;
  navigatingAway = false;
}

export function userStorageKey(name: string): string | null {
  if (!IS_CLOUD) return name;
  return identity ? `suxiaoyou:cloud:${identity.id}:${name}` : null;
}

export const userStorage: Pick<Storage, "getItem" | "setItem" | "removeItem"> = {
  getItem(name) {
    const key = userStorageKey(name);
    if (!key || typeof localStorage === "undefined") return null;
    try { return localStorage.getItem(key); } catch { return null; }
  },
  setItem(name, value) {
    const key = userStorageKey(name);
    if (!key || typeof localStorage === "undefined") return;
    try { localStorage.setItem(key, value); } catch { /* Storage is optional. */ }
  },
  removeItem(name) {
    const key = userStorageKey(name);
    if (!key || typeof localStorage === "undefined") return;
    try { localStorage.removeItem(key); } catch { /* Storage is optional. */ }
  },
};

export function cloudAuthHeaders(method = "GET"): Record<string, string> {
  return IS_CLOUD && !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase()) && csrf
    ? { "X-CSRF-Token": csrf }
    : {};
}

export function trackCloudRequest(controller: AbortController): () => void {
  if (!IS_CLOUD) return () => {};
  requests.add(controller);
  return () => { requests.delete(controller); };
}

/** A full navigation also discards module-level drafts, registries and Query caches. */
export async function leaveCloudWorkspace(target = "/cloud?login=1") {
  if (!IS_CLOUD || navigatingAway || typeof window === "undefined") return;
  navigatingAway = true;
  for (const controller of requests) controller.abort();
  requests.clear();
  try {
    const { disposeAllStreams } = await import("./session-stream-registry");
    disposeAllStreams();
  } catch { /* A partially loaded workspace may not have a registry. */ }
  identity = null;
  csrf = "";
  window.location.replace(target);
}

export async function logoutCloudWorkspace() {
  try {
    await fetch("/api/cloud/auth/logout", { method: "POST", credentials: "same-origin", headers: cloudAuthHeaders("POST") });
  } finally {
    await leaveCloudWorkspace("/cloud?login=1");
  }
}
