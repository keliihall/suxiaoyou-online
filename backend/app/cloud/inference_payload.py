"""A shared, bounded wire budget for cloud Agent inference requests.

Images use base64 on this transport, independently of visual token billing.
32 MiB accommodates the original Read tool's 20 MiB image (about 26.7 MiB
after base64) plus request framing. It remains a total request limit: retain
all input images, and reject oversized batches rather than silently removing
images needed for comparisons or repeatedly attempting the same request.
"""

MAX_INFERENCE_BODY_BYTES = 32 * 1024 * 1024
MODEL_REQUEST_TOO_LARGE = "model_request_too_large"
MODEL_REQUEST_TOO_LARGE_MESSAGE = "本次模型请求的图片或上下文超过大小限制，请减少图片或拆分任务后重试"


def payload_error_body():
    return {"error": {"code": MODEL_REQUEST_TOO_LARGE, "type": "invalid_request_error",
                      "message": MODEL_REQUEST_TOO_LARGE_MESSAGE, "retryable": False}}


class InferencePayloadTooLarge(ValueError):
    """Fixed safe error, preserved through the OpenAI SDK exception cause."""

    def __init__(self):
        super().__init__(MODEL_REQUEST_TOO_LARGE_MESSAGE)


async def check_sdk_inference_payload(request):
    # HTTPX runs this hook after the real SDK has merged extra_body and encoded
    # JSON, but before sending bytes. Never estimate from characters or remove
    # content here. The private capability and body are never logged or echoed.
    if (request.method == "POST" and request.url.path.endswith("/chat/completions")
            and len(request.content) > MAX_INFERENCE_BODY_BYTES):
        raise InferencePayloadTooLarge
