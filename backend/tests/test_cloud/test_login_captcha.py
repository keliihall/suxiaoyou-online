"""Real pre-auth puzzle HTTP flow, TTL and cross-process replay boundaries."""
import asyncio
import base64
import hashlib
import io
from datetime import timedelta

from fastapi import FastAPI
import httpx
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.cloud.identity import router
from app.cloud.login_captcha import COOKIE_NAME
from app.cloud.models import AuthSession, LoginCaptcha, LoginThrottle, utcnow
from captcha_helpers import _answer, captcha_proof
from test_identity import PASSWORD, PREFIX, identity_env  # noqa: F401 (shared fixture)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def puzzle(client, database):
    response = await client.get(PREFIX + "/auth/captcha/challenge")
    assert response.status_code == 200
    body = response.json()
    return body, await _answer(database, body["challenge_id"])


async def sign_in(client, proof=None, *, password=PASSWORD, email="admin@example.com"):
    return await client.post(PREFIX + "/auth/login", json={
        "email": email, "password": password, "captcha_proof": proof})


def second_app(database, settings):
    app = FastAPI()
    app.state.database = database
    app.state.settings = settings
    app.include_router(router, prefix=PREFIX)
    return app


async def test_login_requires_real_proof_before_account_budget(identity_env):
    client, database, _, _, _ = identity_env
    for proof in (None, "forged"):
        result = await sign_in(client, proof)
        assert result.status_code == 403
        assert result.json()["detail"] == {"code": "captcha_required", "message": "请先完成拼图验证"}
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 0
        assert await db.get(LoginThrottle, digest("account:admin@example.com")) is None
        assert await db.get(LoginThrottle, digest("ip:127.0.0.1")) is None


async def test_challenge_images_are_random_and_answer_is_not_exposed(identity_env):
    client, database, _, _, _ = identity_env
    first, _ = await puzzle(client, database)
    second, _ = await puzzle(client, database)
    assert set(first) == {"challenge_id", "background_image", "piece_image", "image_width", "image_height",
                          "piece_width", "piece_height", "piece_y", "expires_in"}
    assert first["expires_in"] == 120
    assert first["challenge_id"] != second["challenge_id"]
    assert first["background_image"] != second["background_image"]
    for field, size, mode in (("background_image", (320, 160), "RGB"), ("piece_image", (48, 48), "RGBA")):
        image = Image.open(io.BytesIO(base64.b64decode(first[field].split(",", 1)[1])))
        assert image.format == "PNG" and image.size == size and image.mode == mode
        assert not image.info  # no answer or deterministic seed in PNG metadata
        if mode == "RGBA":
            assert image.getchannel("A").getextrema() == (0, 255)
    cookie = client.cookies.get(COOKIE_NAME)
    async with database.sessions() as db:
        record = await db.get(LoginCaptcha, digest(second["challenge_id"]))
        assert record.id != second["challenge_id"] and record.browser_hash == digest(cookie)
        assert cookie != record.browser_hash
        assert await db.get(LoginCaptcha, digest(first["challenge_id"])) is None


async def test_wrong_position_consumes_challenge_and_does_not_count_password_attempt(identity_env):
    client, database, _, _, _ = identity_env
    body, answer = await puzzle(client, database)
    path = PREFIX + "/auth/captcha/verify"
    denied = await client.post(path, json={"challenge_id": body["challenge_id"], "x": 0})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "captcha_failed"
    retry = await client.post(path, json={"challenge_id": body["challenge_id"], "x": answer})
    assert retry.status_code == 403
    async with database.sessions() as db:
        assert await db.get(LoginCaptcha, digest(body["challenge_id"])) is None
        assert await db.get(LoginThrottle, digest("account:admin@example.com")) is None


async def test_tolerance_proof_hash_and_bad_password_all_consume_once(identity_env):
    client, database, _, _, _ = identity_env
    body, answer = await puzzle(client, database)
    verified = await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": answer + 4})
    assert verified.status_code == 200 and verified.json()["expires_in"] == 120
    proof = verified.json()["proof"]
    async with database.sessions() as db:
        stored = await db.get(LoginCaptcha, digest(proof))
        assert stored.kind == "proof" and stored.target_x is None and stored.id != proof
    wrong = await sign_in(client, proof, password="wrong-password")
    assert wrong.status_code == 401
    replay = await sign_in(client, proof)
    assert replay.status_code == 403 and replay.json()["detail"]["code"] == "captcha_required"
    async with database.sessions() as db:
        account = await db.get(LoginThrottle, digest("account:admin@example.com"))
        assert account.count == 1
        assert await db.get(LoginCaptcha, digest(proof)) is None
    success = await sign_in(client, await captcha_proof(client, database))
    assert success.status_code == 200
    assert not client.cookies.get(COOKIE_NAME)


async def test_challenge_and_proof_cannot_transfer_to_another_browser(identity_env):
    client, database, settings, _, _ = identity_env
    body, answer = await puzzle(client, database)
    other_app = second_app(database, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://localhost:3000") as other:
        await other.get(PREFIX + "/auth/captcha/challenge")
        stolen = await other.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": answer})
        assert stolen.status_code == 403
        verified = await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": answer})
        assert verified.status_code == 200
        proof = verified.json()["proof"]
        assert (await sign_in(other, proof, email="user@example.com")).status_code == 403
        assert (await sign_in(client, proof)).status_code == 200


async def test_server_ttl_rejects_expired_challenge_and_proof(identity_env):
    client, database, _, _, _ = identity_env
    body, answer = await puzzle(client, database)
    async with database.sessions() as db:
        record = await db.get(LoginCaptcha, digest(body["challenge_id"]))
        record.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    result = await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": answer})
    assert result.status_code == 403
    proof = await captcha_proof(client, database)
    async with database.sessions() as db:
        record = await db.get(LoginCaptcha, digest(proof))
        record.expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (await sign_in(client, proof)).status_code == 403
    async with database.sessions() as db:
        assert await db.get(LoginCaptcha, digest(proof)) is None
        assert await db.get(LoginThrottle, digest("account:admin@example.com")) is None


async def test_refresh_invalidates_previous_challenge_and_success_proof(identity_env):
    client, database, _, _, _ = identity_env
    first, answer = await puzzle(client, database)
    previous_cookie = client.cookies.get(COOKIE_NAME)
    await puzzle(client, database)
    assert client.cookies.get(COOKIE_NAME) != previous_cookie
    result = await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": first["challenge_id"], "x": answer})
    assert result.status_code == 403
    proof = await captcha_proof(client, database)
    await puzzle(client, database)
    assert (await sign_in(client, proof)).status_code == 403


async def test_captcha_throttle_is_shared_and_separate_from_password_limit(identity_env):
    client, database, settings, _, _ = identity_env
    settings.captcha_max_attempts = 2
    other_app = second_app(database, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://localhost:3000") as other:
        assert (await client.get(PREFIX + "/auth/captcha/challenge")).status_code == 200
        assert (await other.get(PREFIX + "/auth/captcha/challenge")).status_code == 200
        limited = await client.get(PREFIX + "/auth/captcha/challenge")
        assert limited.status_code == 429
        assert limited.json()["detail"]["code"] == "captcha_rate_limited" and limited.headers["retry-after"] == "60"
    async with database.sessions() as db:
        assert await db.get(LoginThrottle, digest("account:admin@example.com")) is None


async def test_guess_requests_are_bounded_even_with_fake_challenge_ids(identity_env):
    client, database, settings, _, _ = identity_env
    settings.captcha_max_attempts = 2
    for _ in range(2):
        assert (await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": "forged", "x": 0})).status_code == 403
    limited = await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": "forged", "x": 0})
    assert limited.status_code == 429
    async with database.sessions() as db:
        assert await db.get(LoginThrottle, digest("account:admin@example.com")) is None


async def test_concurrent_verification_and_cross_account_login_allow_only_one_use(identity_env):
    client, database, settings, _, _ = identity_env
    body, answer = await puzzle(client, database)
    other_app = second_app(database, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://localhost:3000",
                                 cookies={COOKIE_NAME: client.cookies.get(COOKIE_NAME)}) as other:
        replies = await asyncio.gather(*[
            browser.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": answer})
            for browser in (client, other)
        ])
        assert sorted(reply.status_code for reply in replies) == [200, 403]
        proof = next(reply.json()["proof"] for reply in replies if reply.status_code == 200)
        results = await asyncio.gather(sign_in(client, proof), sign_in(other, proof, email="user@example.com"))
        assert sorted(result.status_code for result in results) == [200, 403]
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AuthSession)) == 1
        assert await db.get(LoginCaptcha, digest(proof)) is None


async def test_login_rate_limit_also_consumes_valid_proof(identity_env):
    client, database, settings, _, _ = identity_env
    settings.login_max_attempts = 1
    assert (await sign_in(client, await captcha_proof(client, database), password="wrong")).status_code == 401
    proof = await captcha_proof(client, database)
    assert (await sign_in(client, proof)).status_code == 429
    assert (await sign_in(client, proof)).status_code == 403


async def test_challenge_origin_cookie_and_coordinate_validation(identity_env):
    client, database, settings, _, _ = identity_env
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        assert (await client.get(PREFIX + "/auth/captcha/challenge", headers=headers)).status_code == 403
    body, answer = await puzzle(client, database)
    denied = await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": answer},
                               headers={"Origin": "https://evil.example"})
    assert denied.status_code == 403
    for x in (-1, 273):
        assert (await client.post(PREFIX + "/auth/captcha/verify", json={"challenge_id": body["challenge_id"], "x": x})).status_code == 422
    settings.cookie_secure = True
    response = await client.get(PREFIX + "/auth/captcha/challenge")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "max-age=120" in cookie
    assert response.headers["cache-control"] == "no-store"
