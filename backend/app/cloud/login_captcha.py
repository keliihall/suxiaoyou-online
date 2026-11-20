"""Short-lived browser-bound login puzzles, shared atomically through SQL storage.

Only the synthetic images leave the server. Answers stay in the database; both a
puzzle attempt and its resulting login proof are consumed exactly once. Existing
password/IP login budgets remain independent from this pre-authentication gate.
"""

import asyncio
import base64
import hashlib
import io
import math
import random
import re
import secrets
from datetime import timezone, timedelta

from fastapi import HTTPException, Request, Response
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.models import LoginCaptcha, LoginThrottle, utcnow

COOKIE_NAME = "cloud_login_challenge"
IMAGE_WIDTH, IMAGE_HEIGHT = 320, 160
PIECE_SIZE = 48
TOLERANCE = 4
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _error(status: int, code: str, message: str, **kwargs) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message}, **kwargs)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _browser_hash(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME, "")
    return _hash(token) if _TOKEN.fullmatch(token) else None


def _expired(value) -> bool:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware <= utcnow()


async def _budget(request: Request, db: AsyncSession, scope: str) -> None:
    """IP counters use the same cross-process write serialization as login.

    Never trust client-supplied forwarding headers or use account identifiers:
    a failed puzzle must not lock an unrelated account out of password login.
    """
    settings = request.app.state.settings
    peer = request.client.host if request.client else "unknown"
    key = _hash(f"captcha:{scope}:{peer}")
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = utcnow()
    await db.execute(insert(LoginThrottle).values(key=key, count=0, window_started=now).on_conflict_do_nothing(index_elements=["key"]))
    await db.execute(update(LoginThrottle).where(LoginThrottle.key == key).values(count=LoginThrottle.count + 1))
    counter = (await db.execute(select(LoginThrottle).where(LoginThrottle.key == key).execution_options(populate_existing=True))).scalar_one()
    started = counter.window_started if counter.window_started.tzinfo else counter.window_started.replace(tzinfo=timezone.utc)
    if (now - started).total_seconds() >= settings.captcha_window_seconds:
        counter.count = 1
        counter.window_started = now
    allowed = counter.count <= settings.captcha_max_attempts
    # Keep a bounded expiration cleanup on the indexed TTL column. There is no
    # in-memory fallback and no deployment-time background worker requirement.
    expired_ids = select(LoginCaptcha.id).where(LoginCaptcha.expires_at <= now).order_by(LoginCaptcha.expires_at).limit(200)
    await db.execute(delete(LoginCaptcha).where(LoginCaptcha.id.in_(expired_ids)))
    await db.commit()
    if not allowed:
        raise _error(429, "captcha_rate_limited", "操作频繁，请稍后再试", headers={"Retry-After": str(settings.captcha_window_seconds)})


def _png(image: Image.Image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def render_puzzle(target_x: int, piece_y: int) -> tuple[str, str]:
    """Render a fresh textured, abstract scene without remote assets or metadata."""
    rng = random.Random(secrets.randbits(256))
    background = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (54, 83, 105))
    draw = ImageDraw.Draw(background, "RGBA")
    palettes = ((100, 153, 166), (189, 166, 125), (100, 125, 168), (125, 159, 133))
    for _ in range(36):
        x, y = rng.randrange(-70, IMAGE_WIDTH), rng.randrange(-60, IMAGE_HEIGHT)
        width, height = rng.randrange(30, 160), rng.randrange(20, 110)
        draw.ellipse((x, y, x + width, y + height), fill=(*rng.choice(palettes), rng.randrange(50, 155)))
    background = background.filter(ImageFilter.GaussianBlur(5))
    draw = ImageDraw.Draw(background, "RGBA")
    for _ in range(18):
        start, phase = rng.randrange(IMAGE_HEIGHT), rng.random() * math.tau
        points = [(x, start + math.sin(x / 45 + phase) * rng.randrange(6, 20)) for x in range(-4, IMAGE_WIDTH + 5, 4)]
        draw.line(points, fill=(*rng.choice(palettes), rng.randrange(90, 175)), width=rng.randrange(2, 5))
    for _ in range(360):
        x, y, radius = rng.randrange(IMAGE_WIDTH), rng.randrange(IMAGE_HEIGHT), rng.randrange(1, 3)
        draw.ellipse((x, y, x + radius, y + radius), fill=(*rng.choice(palettes), rng.randrange(75, 165)))

    mask = Image.new("L", (PIECE_SIZE, PIECE_SIZE), 0)
    shape = ImageDraw.Draw(mask)
    shape.rounded_rectangle((6, 6, 41, 41), radius=4, fill=255)
    shape.ellipse((18, 0, 30, 12), fill=255)
    shape.ellipse((36, 18, 47, 30), fill=255)
    shape.ellipse((0, 18, 12, 30), fill=0)
    shape.ellipse((18, 36, 30, 47), fill=0)
    piece = background.crop((target_x, piece_y, target_x + PIECE_SIZE, piece_y + PIECE_SIZE)).convert("RGBA")
    piece.putalpha(mask)
    piece_edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(3)))
    piece.paste((241, 245, 249, 255), mask=piece_edge)
    hole = Image.new("RGB", (PIECE_SIZE, PIECE_SIZE), (17, 24, 39))
    original = background.crop((target_x, piece_y, target_x + PIECE_SIZE, piece_y + PIECE_SIZE))
    background.paste(Image.blend(original, hole, .72), (target_x, piece_y), mask)
    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
    background.paste((225, 234, 242), (target_x, piece_y, target_x + PIECE_SIZE, piece_y + PIECE_SIZE), edge)
    return _png(background), _png(piece)


def _set_cookie(request: Request, response: Response, browser: str) -> None:
    response.set_cookie(COOKIE_NAME, browser, httponly=True,
                        secure=request.app.state.settings.cookie_secure, samesite="lax",
                        max_age=request.app.state.settings.captcha_ttl_seconds, path="/")
    response.headers["Cache-Control"] = "no-store"


async def issue(request: Request, response: Response, db: AsyncSession) -> dict:
    await _budget(request, db, "issue")
    ttl = request.app.state.settings.captcha_ttl_seconds
    # Reissuing rotates the HttpOnly binding, so old challenges and proofs in
    # this browser cannot be replayed even before their server-side TTL ends.
    browser, challenge = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    target_x = secrets.randbelow(IMAGE_WIDTH - PIECE_SIZE - 88) + 72
    piece_y = secrets.randbelow(IMAGE_HEIGHT - PIECE_SIZE - 24) + 12
    background, piece = await asyncio.to_thread(render_puzzle, target_x, piece_y)
    previous_browser = _browser_hash(request)
    if previous_browser:
        await db.execute(delete(LoginCaptcha).where(LoginCaptcha.browser_hash == previous_browser))
    db.add(LoginCaptcha(id=_hash(challenge), kind="challenge", browser_hash=_hash(browser),
                        target_x=target_x, expires_at=utcnow() + timedelta(seconds=ttl)))
    await db.commit()
    _set_cookie(request, response, browser)
    return {"challenge_id": challenge, "background_image": background, "piece_image": piece,
            "image_width": IMAGE_WIDTH, "image_height": IMAGE_HEIGHT,
            "piece_width": PIECE_SIZE, "piece_height": PIECE_SIZE,
            "piece_y": piece_y, "expires_in": ttl}


async def verify(request: Request, response: Response, db: AsyncSession, challenge_id: str, x: float) -> dict:
    await _budget(request, db, "verify")
    browser_hash = _browser_hash(request)
    record = None
    if browser_hash and _TOKEN.fullmatch(challenge_id):
        record = (await db.execute(delete(LoginCaptcha).where(
            LoginCaptcha.id == _hash(challenge_id), LoginCaptcha.kind == "challenge",
            LoginCaptcha.browser_hash == browser_hash).returning(LoginCaptcha))).scalar_one_or_none()
    if record is None or _expired(record.expires_at) or abs(x - record.target_x) > TOLERANCE:
        # A wrong coordinate consumes the puzzle just like a correct answer.
        await db.commit()
        raise _error(403, "captcha_failed", "验证未通过，请重试")
    proof = secrets.token_urlsafe(32)
    ttl = request.app.state.settings.captcha_ttl_seconds
    db.add(LoginCaptcha(id=_hash(proof), kind="proof", browser_hash=browser_hash,
                        target_x=None, expires_at=utcnow() + timedelta(seconds=ttl)))
    await db.commit()
    _set_cookie(request, response, request.cookies[COOKIE_NAME])
    return {"proof": proof, "expires_in": ttl}


async def consume_proof(request: Request, db: AsyncSession, proof: str | None) -> None:
    await _budget(request, db, "login")
    browser_hash = _browser_hash(request)
    record = None
    if browser_hash and proof and _TOKEN.fullmatch(proof):
        record = (await db.execute(delete(LoginCaptcha).where(
            LoginCaptcha.id == _hash(proof), LoginCaptcha.kind == "proof",
            LoginCaptcha.browser_hash == browser_hash).returning(LoginCaptcha))).scalar_one_or_none()
    # Commit before password throttling/work: a bad password or exhausted login
    # budget must never roll a successful proof back into reusable storage.
    await db.commit()
    if record is None or _expired(record.expires_at):
        raise _error(403, "captcha_required", "请先完成拼图验证")


def clear_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=request.app.state.settings.cookie_secure,
                           httponly=True, samesite="lax")
