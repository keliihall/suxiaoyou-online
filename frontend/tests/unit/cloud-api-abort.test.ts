import assert from "node:assert/strict";
import test from "node:test";
import { clearCloudSession, cloudRequest } from "../../src/app/cloud/cloud-api.ts";

for (const trigger of ["caller timeout", "session teardown"]) {
  test(`a response stalled after headers is cancelled by ${trigger}`, { timeout: 2_000 }, async () => {
    const previousFetch = globalThis.fetch;
    const external = new AbortController();
    const reading = Promise.withResolvers<void>();
    globalThis.fetch = async (_input, options) => ({
      ok: true,
      status: 200,
      json: () => new Promise((_resolve, reject) => {
        options!.signal!.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
        reading.resolve();
      }),
    } as Response);
    try {
      const request = cloudRequest("/auth/captcha/challenge", { signal: external.signal });
      await reading.promise;
      // The headers already arrived, but the body is still incomplete.
      if (trigger === "caller timeout") external.abort();
      else clearCloudSession();
      await assert.rejects(request, { name: "AbortError" });
    } finally {
      clearCloudSession();
      globalThis.fetch = previousFetch;
    }
  });
}
