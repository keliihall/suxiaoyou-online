"""Authenticated image gateway with image-count, never invented token usage."""
import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from app.cloud import image_metering, metering
from app.cloud.database import get_db
from app.cloud.identity import require_user
from app.cloud.inference import Db, InferenceAttempt, Principal, _API_OWNER, _LEASE_SECONDS, _error_response
from app.cloud.models import ModelRoute, RequestRecord, User, utcnow
from app.cloud.providers import ProviderConfigurationError, route_api_key
from app.image_generation.siliconflow import SiliconFlowImageClient, SILICONFLOW_IMAGE_MODEL, SILICONFLOW_IMAGE_SIZES

router = APIRouter(tags=["image-inference"])
CurrentUser = Annotated[User, Depends(require_user)]


class ImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=8, max_length=160)
    prompt: str = Field(min_length=1, max_length=4000)
    image_size: str = "1024x1024"
    negative_prompt: str | None = Field(default=None, max_length=2000)
    seed: int | None = Field(default=None, ge=0, le=9999999999)
    num_inference_steps: int = Field(default=20, ge=1, le=100)
    guidance_scale: float = Field(default=7.5, ge=0, le=20)


class ImageAttempt(InferenceAttempt):
    async def _principal(self, db):
        if self.runtime:
            return await super()._principal(db)
        return await require_user(self.request, db)

    async def reserve(self):
        payload = self.body.model_dump(exclude={"operation_id"})
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        async with self.database.sessions() as db:
            record = await image_metering.reserve(db, self.user.id, self.route, self.body.operation_id, digest,
                          lease_owner=_API_OWNER, lease_until=utcnow() + timedelta(seconds=_LEASE_SECONDS))
            self.id = record.id

    async def finalize(self):
        with anyio.CancelScope(shield=True):
            async with self._finalize_lock:
                if self._finalized:
                    return
                try:
                    if self.heartbeat:
                        self.heartbeat.cancel()
                        try:
                            await self.heartbeat
                        except asyncio.CancelledError:
                            pass
                    if self.id:
                        async with self.database.sessions() as db:
                            record = await db.get(RequestRecord, self.id)
                            if record.state == "reserved":
                                await image_metering.finish(db, self.id, release=True)
                            elif record.state in {"dispatched", "unknown"}:
                                if self.usage:
                                    await image_metering.finish(db, self.id)
                                else:
                                    await metering.mark_unknown(db, self.id, "图片请求已发出，生成或用量状态待核对")
                    self._finalized = True
                finally:
                    await self.provider.aclose()


async def generate_image(body, request, user, db, *, runtime):
    if body.image_size not in SILICONFLOW_IMAGE_SIZES or not body.prompt.strip():
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_image_parameters", "message": "图片参数无效"}})
    query = select(ModelRoute).where(ModelRoute.enabled.is_(True), ModelRoute.kind == "image",
                ModelRoute.provider == "siliconflow", ModelRoute.model == SILICONFLOW_IMAGE_MODEL,
                or_(ModelRoute.owner_user_id.is_(None), ModelRoute.owner_user_id == user.id))
    if body.model != SILICONFLOW_IMAGE_MODEL:
        query = query.where(ModelRoute.id == body.model)
    # The fixed-model desktop image tool can use a private route first, then a
    # shared route. Explicit platform aliases select exactly one authorized row.
    route = await db.scalar(query.order_by(ModelRoute.owner_user_id.desc().nulls_last(), ModelRoute.id).limit(1))
    if route is None:
        return JSONResponse(status_code=404, content={"error": {"code": "image_model_unavailable", "message": "请先配置并启用图片模型"}})
    attempt = None
    try:
        client = SiliconFlowImageClient(route_api_key(route, request.app.state.settings))
        attempt = ImageAttempt(request, body, user, route, client)
        attempt.runtime = runtime
        reservation = asyncio.create_task(attempt.reserve())
        try:
            await asyncio.shield(reservation)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await reservation
                await attempt.finalize()
            raise
        attempt.heartbeat = asyncio.create_task(attempt._renew(asyncio.current_task()))
        await attempt._dispatch()
        async with asyncio.timeout(240):
            result = await client.generate(**body.model_dump(exclude={"model", "operation_id"}))
        attempt.usage = {"unit": "image", "count": 1}
        await attempt.finalize()
        return JSONResponse({"created": attempt.created, "data": [{"b64_json": base64.b64encode(result.content).decode(),
                             "width": result.width, "height": result.height}], "request_id": attempt.id,
                             "usage": {"unit": "image", "count": 1, "total_tokens": 0}},
                            headers={"Cache-Control": "no-store", "X-Request-ID": attempt.id})
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return _error_response(error)
    finally:
        if attempt:
            await attempt.finalize()


@router.post("/api/cloud/internal/inference/v1/images/generations")
async def runtime_image(body: ImageRequest, request: Request, user: Principal, db: Db):
    return await generate_image(body, request, user, db, runtime=True)


@router.post("/api/cloud/images/generations")
async def user_image(body: ImageRequest, request: Request, user: CurrentUser, db: Db):
    return await generate_image(body, request, user, db, runtime=False)
