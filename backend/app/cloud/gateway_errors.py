"""Safe, structured platform rejections passed through the original engine."""
from app.cloud.inference_payload import (
    InferencePayloadTooLarge,
    MODEL_REQUEST_TOO_LARGE,
    MODEL_REQUEST_TOO_LARGE_MESSAGE,
)
from app.schemas.provider import (
    SAFE_PROVIDER_ERRORS, safe_provider_error_data, safe_provider_error_metadata,
)

ADMISSION_ERRORS = {
    "quota_exceeded": "本月 Token 额度不足，请联系管理员调整额度",
    "concurrency_exceeded": "当前模型并发已达上限，请等待正在运行的任务完成",
    "account_disabled": "账号已停用",
    "runtime_unavailable": "云端运行环境身份已失效，请重新登录",
    "model_unavailable": "模型配置不可用，请联系管理员检查",
    MODEL_REQUEST_TOO_LARGE: MODEL_REQUEST_TOO_LARGE_MESSAGE,
}


def admission_chunk(error):
    # A request hook runs before any network I/O. The real OpenAI SDK wraps
    # hook exceptions in APIConnectionError; preserve the fixed rejection
    # without inspecting arbitrary exception text, credentials or payloads.
    cause = error
    for _ in range(8):
        if isinstance(cause, InferencePayloadTooLarge):
            return {"code": MODEL_REQUEST_TOO_LARGE,
                    "message": MODEL_REQUEST_TOO_LARGE_MESSAGE, "retryable": False}
        cause = getattr(cause, "__cause__", None)
        if cause is None:
            break
    body = getattr(error, "body", None)
    if not isinstance(body, dict):
        return None
    body = body.get("error", body)
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    if not isinstance(code, str):
        return None
    if code in SAFE_PROVIDER_ERRORS:
        return safe_provider_error_data(code, **safe_provider_error_metadata(body))
    if code not in ADMISSION_ERRORS:
        return None
    return {"code": code, "message": ADMISSION_ERRORS[code], "retryable": False,
            **safe_provider_error_metadata(body)}
