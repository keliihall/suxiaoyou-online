"""Image-count gateway and private BYOK route isolation without paid calls."""
import asyncio
import base64
import hashlib
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select, update

from app.cloud import image_inference, image_metering, inference, metering
from app.cloud.models import AuthSession, ImageQuotaBucket, ModelRoute, RequestRecord, User, utcnow
from app.cloud.routes import router as resources
from app.cloud.secrets import ModelSecretVault
from tests.test_cloud.test_inference_gateway import gateway, records, CAPABILITY

IMAGE = "/api/cloud/internal/inference/v1/images/generations"
NATIVE = "Kwai-Kolors/Kolors"


class ImageVendor:
    def __init__(self):
        self.calls, self.closed, self.fail, self.pause = 0, False, False, False
        self.entered, self.resume = asyncio.Event(), asyncio.Event()
    async def generate(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        self.entered.set()
        if self.pause:
            await self.resume.wait()
        if self.fail:
            raise RuntimeError("IMAGE_VENDOR_SECRET")
        content = BytesIO()
        Image.new("RGB", (2, 2), "red").save(content, "PNG")
        return SimpleNamespace(content=content.getvalue(), width=2, height=2)
    async def aclose(self):
        self.closed = True


async def setup(gateway, monkeypatch):
    app, database, client, _ = gateway
    app.include_router(image_inference.router)
    app.include_router(resources, prefix="/api/cloud")
    vendor = ImageVendor()
    monkeypatch.setattr(image_inference, "SiliconFlowImageClient", lambda _: vendor)
    monkeypatch.setattr(image_inference, "route_api_key", lambda *_: "synthetic-never-a-real-key")
    async with database.sessions() as db:
        db.add(ModelRoute(id="image-team", name="Image", provider="siliconflow", model=NATIVE,
                          kind="image", image_price_microusd=12345))
        db.add(User(id="other", email="other@test.local", display_name="Other", password_hash="unused"))
        await db.flush()
        db.add(ModelRoute(id="other-private", name="Private", provider="deepseek", model="deepseek-flash",
                          owner_user_id="other", api_key_encrypted="inaccessible"))
        db.add(AuthSession(id=hashlib.sha256(b"cookie-owner").hexdigest(), user_id="owner", csrf_token="csrf",
                           expires_at=utcnow() + timedelta(hours=1)))
        await db.commit()
    client.cookies.set("cloud_session", "cookie-owner")
    client.headers["X-CSRF-Token"] = "csrf"
    return app, database, client, vendor


def body(**kwargs):
    return {"model": "image-team", "prompt": "一只小猫", "operation_id": "image-operation-0001", **kwargs}


async def test_image_is_counted_once_not_invented_tokens(gateway, monkeypatch):
    _, database, client, vendor = await setup(gateway, monkeypatch)
    response = await client.post(IMAGE, json=body())
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()["data"][0]["b64_json"]).startswith(b"\x89PNG")
    rows, events, summary = await records(database)
    assert rows[0].source == "image" and rows[0].state == "settled" and rows[0].total_tokens == 0
    assert rows[0].normalized_usage["unit"] == "image" and rows[0].normalized_usage["count"] == 1
    assert rows[0].cost_microusd == 12345 and events[0].total_tokens == 0
    assert summary["used_tokens"] == 0 and summary["reserved_tokens"] == 0
    assert summary["image"] == {"period": metering.billing_period(), "limit": 100, "used": 1, "reserved": 0,
                                "remaining": 99, "unknown_requests": 0, "total_cost_microusd": 12345}
    assert vendor.calls == 1 and vendor.closed
    assert (await client.post(IMAGE, json=body())).status_code == 409
    assert vendor.calls == 1


async def test_image_quota_is_separate_from_text_tokens(gateway, monkeypatch):
    _, database, client, vendor = await setup(gateway, monkeypatch)
    async with database.sessions() as db:
        await db.execute(update(User).where(User.id == "owner").values(image_limit=0, token_limit=1000000))
        await db.commit()
    response = await client.post(IMAGE, json=body())
    assert response.status_code == 429 and response.json()["error"]["code"] == "image_quota_exceeded"
    assert vendor.calls == 0 and (await records(database))[0] == []


async def test_image_failure_retains_count_reservation_and_safe_errors(gateway, monkeypatch):
    _, database, client, vendor = await setup(gateway, monkeypatch)
    vendor.fail = True
    response = await client.post(IMAGE, json=body())
    assert response.status_code == 502 and "IMAGE_VENDOR_SECRET" not in response.text
    rows, events, summary = await records(database)
    assert rows[0].state == "unknown" and summary["image"]["reserved"] == 1 and summary["image"]["unknown_requests"] == 1
    assert summary["reserved_tokens"] == 0 and events == []
    async with database.sessions() as db:
        await image_metering.finish(db, rows[0].id, release=True, allow_unknown=True)
    assert (await records(database))[2]["image"]["reserved"] == 0


async def test_image_expired_reserved_release_and_dispatched_unknown(gateway, monkeypatch):
    _, database, client, _ = await setup(gateway, monkeypatch)
    async with database.sessions() as db:
        route = await db.get(ModelRoute, "image-team")
        one = await image_metering.reserve(db, "owner", route, "operation-expired", "a" * 64,
                 lease_owner="old", lease_until=utcnow() - timedelta(seconds=10))
        await metering.mark_dispatched(db, one.id)
    assert await inference.recover_inference_requests(database) == 1
    rows, _, summary = await records(database)
    assert rows[0].state == "unknown" and summary["image"]["reserved"] == 1
    assert summary["reserved_tokens"] == 0


async def test_browser_image_uses_csrf_and_same_count_ledger(gateway, monkeypatch):
    _, database, client, vendor = await setup(gateway, monkeypatch)
    client.headers.pop("Authorization")
    response = await client.post("/api/cloud/images/generations", json=body())
    assert response.status_code == 200 and vendor.calls == 1
    client.headers.pop("X-CSRF-Token")
    response = await client.post("/api/cloud/images/generations", json=body(operation_id="different-id"))
    assert response.status_code == 403 and vendor.calls == 1


async def test_private_byok_cannot_reference_server_env_or_other_users(gateway, monkeypatch):
    _, database, client, _ = await setup(gateway, monkeypatch)
    ordinary = await client.get("/api/cloud/models")
    assert "other-private" not in ordinary.text and "image-team" not in ordinary.text
    denied = await client.post("/api/cloud/user/models", json={"id": "key", "name": "Key", "provider": "deepseek",
                 "model": "deepseek-flash", "api_key_env": "SUXIAOYOU_MODEL_ADMIN_KEY"})
    assert denied.status_code == 400
    saved = await client.post("/api/cloud/user/models", json={"id": "key", "name": "Key", "provider": "deepseek",
                 "model": "deepseek-flash", "api_key": "synthetic-private-vendor-key"})
    assert saved.status_code == 201, saved.text
    result = saved.json()
    assert result["id"].startswith("user.owner.") and result["owner_user_id"] == "owner"
    assert "synthetic-private-vendor-key" not in saved.text and result["key_configured"] is True
    async with database.sessions() as db:
        row = await db.get(ModelRoute, result["id"])
        assert ModelSecretVault(database.settings).unseal(row.api_key_encrypted) == "synthetic-private-vendor-key"
    listing = await client.get("/api/cloud/user/models")
    assert [row["id"] for row in listing.json()] == [result["id"]]
    assert (await client.delete("/api/cloud/user/models/other-private")).status_code == 404
    assert (await client.delete("/api/cloud/user/models/" + result["id"])).status_code == 200
    assert result["id"] not in (await client.get("/api/cloud/models")).text
