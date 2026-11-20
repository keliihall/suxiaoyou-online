export interface StreamErrorPayload {
  error_message?: string | null;
  error_type?: string | null;
  code?: string | null;
  request_id?: string | null;
  upstream_status?: number | null;
}

const CONNECTION_ERROR_MARKERS = [
  "peer closed connection",
  "incomplete chunked read",
  "remoteprotocolerror",
  "server disconnected",
  "connection reset",
  "connection closed",
  "econnreset",
  "econnrefused",
];

export function streamErrorTranslationKey(
  data: StreamErrorPayload,
): string | null {
  const code = data.code ?? data.error_type ?? "";
  switch (code) {
    case "quota_exceeded":
      return "streamErrorQuotaExceeded";
    case "concurrency_exceeded":
      return "streamErrorConcurrencyExceeded";
    case "account_disabled":
      return "streamErrorAccountDisabled";
    case "runtime_unavailable":
      return "streamErrorRuntimeUnavailable";
    case "model_unavailable":
      return "streamErrorModelUnavailable";
    case "model_request_too_large":
      return "streamErrorRequestTooLarge";
    case "model_context_too_long":
      return "streamErrorContextTooLong";
    case "model_output_exhausted":
      return "streamErrorOutputExhausted";
    case "model_response_incomplete":
      return "streamErrorResponseIncomplete";
    case "model_usage_unconfirmed":
      return "streamErrorUsageUnconfirmed";
    case "model_request_rejected":
      return "streamErrorRequestRejected";
    case "model_payment_required":
      return "streamErrorPaymentRequired";
    case "model_connection_interrupted":
      return "streamErrorConnectionInterrupted";
    case "model_timeout":
      return "streamErrorTimedOut";
    case "model_rate_limited":
      return "streamErrorRateLimited";
    case "model_authentication_failed":
      return "streamErrorAuthentication";
    case "server_busy":
    case "session_busy":
      return "streamErrorServerBusy";
    case "security_emergency_stop":
    case "invocation_source_denied":
    case "permission_required":
    case "security_audit_unavailable":
    case "hook_policy_failed":
    case "middleware_blocked":
    case "middleware_error":
      return "streamErrorPolicyBlocked";
    case "MODEL_DOES_NOT_SUPPORT_IMAGES":
      return "visionModelRequired";
    case "model_not_found":
      return "streamErrorModelNotFound";
    case "task_batch_workspace_rejected":
      return "taskBatchStartFailed";
    case "task_batch_failed":
      return "taskBatchExecutionFailed";
    case "compaction_failed":
      return "contextCompactError";
    case "model_service_error":
      return "streamErrorGeneric";
    default:
      break;
  }

  const message = (data.error_message ?? "").toLocaleLowerCase("en-US");
  if (
    /maximum context length|context_length_exceeded|context window|input too long/.test(
      message,
    )
  ) {
    return "streamErrorContextTooLong";
  }
  if (CONNECTION_ERROR_MARKERS.some((marker) => message.includes(marker))) {
    return "streamErrorConnectionInterrupted";
  }
  if (message.includes("timeout") || message.includes("timed out")) {
    return "streamErrorTimedOut";
  }
  if (
    message.includes("429") ||
    message.includes("rate limit") ||
    message.includes("too_many_requests")
  ) {
    return "streamErrorRateLimited";
  }
  if (
    message.includes("401") ||
    message.includes("unauthorized") ||
    message.includes("authentication failed")
  ) {
    return "streamErrorAuthentication";
  }
  return null;
}

/** Only platform-generated ledger IDs may appear in a user-facing reference. */
export function streamErrorRequestId(data: StreamErrorPayload): string | null {
  return typeof data.request_id === "string" && data.request_id.length === 32 && /^[a-f0-9]{32}$/.test(data.request_id)
    ? data.request_id
    : null;
}

export function streamErrorToastId(
  sessionId: string,
  data: StreamErrorPayload,
  translationKey: string | null,
): string {
  const category =
    data.code ?? data.error_type ?? translationKey ?? "unknown-stream-error";
  return `stream-error:${sessionId}:${category}`;
}
