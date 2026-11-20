import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { streamErrorRequestId, streamErrorTranslationKey } from "../../src/lib/stream-error.ts";

const cases = [
  ["quota_exceeded", "streamErrorQuotaExceeded"],
  ["concurrency_exceeded", "streamErrorConcurrencyExceeded"],
  ["account_disabled", "streamErrorAccountDisabled"],
  ["runtime_unavailable", "streamErrorRuntimeUnavailable"],
  ["model_unavailable", "streamErrorModelUnavailable"],
  ["model_request_too_large", "streamErrorRequestTooLarge"],
  ["model_context_too_long", "streamErrorContextTooLong"],
  ["model_output_exhausted", "streamErrorOutputExhausted"],
  ["model_response_incomplete", "streamErrorResponseIncomplete"],
  ["model_usage_unconfirmed", "streamErrorUsageUnconfirmed"],
  ["model_request_rejected", "streamErrorRequestRejected"],
  ["model_payment_required", "streamErrorPaymentRequired"],
] as const;

const locales = ["zh", "en"].map((language) => ({
  language,
  messages: JSON.parse(readFileSync(
    new URL(`../../src/i18n/locales/${language}/chat.json`, import.meta.url),
    "utf8",
  )) as Record<string, string>,
}));

for (const [code, key] of cases) {
  test(`${code} uses a fixed safe message in both locales`, () => {
    // Explicit cloud codes must win over untrusted provider diagnostics, even
    // if those diagnostics contain another error category or sensitive data.
    const rawMessage = "401 unauthorized; 429 rate limit; secret-token-sentinel";
    for (const field of ["code", "error_type"] as const) {
      const result = streamErrorTranslationKey({ [field]: code, error_message: rawMessage });
      assert.equal(result, key);
      for (const { language, messages } of locales) {
        const message: string = messages[result!];
        assert.equal(typeof message, "string", `${language} translation is present`);
        assert.ok(message.length > 0);
        assert.ok(!message.includes(rawMessage));
        assert.ok(!message.includes("secret-token-sentinel"));
        assert.doesNotMatch(message, /\{\{/);
      }
    }
  });
}

test("oversized request messages explain how to reduce the payload", () => {
  const zh = locales.find((locale) => locale.language === "zh")!.messages.streamErrorRequestTooLarge;
  const en = locales.find((locale) => locale.language === "en")!.messages.streamErrorRequestTooLarge;
  assert.match(zh, /减少图片/);
  assert.match(zh, /精简请求/);
  assert.match(en, /fewer images/);
  assert.match(en, /shorten the request/);
});

test("only platform ledger IDs may appear as error references", () => {
  const id = "17aaf027e6154d11b89b3ec439ad6cc9";
  assert.equal(streamErrorRequestId({ request_id: id }), id);
  for (const request_id of ["secret-token-sentinel", id.toUpperCase(), `${id}\n`, "x".repeat(33), null]) {
    assert.equal(streamErrorRequestId({ request_id }), null);
  }
});
