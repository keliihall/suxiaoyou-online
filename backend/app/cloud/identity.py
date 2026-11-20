"""Cookie identity, CSRF and invitation-only administration for cloud mode."""

import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.database import get_db
from app.cloud import login_captcha
from app.cloud.models import AuditEvent, AuthSession, IdentityState, LoginThrottle, QuotaBucket, ImageQuotaBucket, User, utcnow

router = APIRouter()
COOKIE_NAME = "cloud_session"
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 32768, 8, 1


def error(status: int, code: str, message: str, **kwargs) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message}, **kwargs)


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not _EMAIL.fullmatch(value):
        raise ValueError("A valid email address is required")
    return value


def validate_password(value: str) -> str:
    if not 12 <= len(value) <= 128:
        raise ValueError("Password must contain 12 to 128 characters")
    return value


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > 128:
        return False
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        # Reject attacker-controlled parameters instead of allocating arbitrary resources.
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt_bytes, expected_bytes = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=int(n), r=int(r), p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError, OverflowError):
        return False


# Use equal-cost password work when an account does not exist.
_DUMMY_PASSWORD = f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$" + "00" * 16 + "$" + "00" * 32


def public_user(user: User) -> dict:
    return {name: getattr(user, name) for name in ("id", "email", "display_name", "role", "is_active", "token_limit", "image_limit", "max_concurrent_jobs")}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin != request.app.state.settings.allowed_origin:
        raise error(403, "origin_rejected", "This request origin is not allowed")
    # Reject browser cross-site requests even when an Origin header is absent.
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise error(403, "origin_rejected", "Cross-site requests are not allowed")


async def require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 128:
        raise error(401, "unauthenticated", "Sign in to continue")
    session_id = hashlib.sha256(token.encode()).hexdigest()
    result = (await db.execute(select(AuthSession, User).join(User, User.id == AuthSession.user_id).where(AuthSession.id == session_id))).one_or_none()
    if result is None:
        raise error(401, "unauthenticated", "Sign in to continue")
    auth_session, user = result
    if _aware(auth_session.expires_at) <= utcnow() or not user.is_active:
        raise error(401, "session_expired", "Your session is no longer active")
    if request.method.upper() in _UNSAFE:
        check_origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(csrf.encode(), auth_session.csrf_token.encode()):
            raise error(403, "csrf_rejected", "A valid CSRF token is required")
    request.state.cloud_user = user
    request.state.cloud_session = auth_session
    request.state.csrf_token = auth_session.csrf_token
    return user


async def require_admin(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    user = await require_user(request, db)
    if user.role != "admin":
        raise error(403, "forbidden", "Administrator access is required")
    return user


async def lock_identity(db: AsyncSession) -> None:
    """A write locks the singleton row on PostgreSQL and SQLite alike."""
    if db.bind.dialect.name == "sqlite" and db.in_transaction():
        # SQLite cannot reliably upgrade a concurrently read transaction to a
        # writer. Identity routes call this before any mutation, then refresh
        # the authenticated user under the write lock.
        await db.rollback()
    result = await db.execute(update(IdentityState).where(IdentityState.id == "identity").values(revision=IdentityState.revision + 1))
    if result.rowcount != 1:
        raise RuntimeError("Cloud identity mutex is missing; migrate the database")


async def create_user(db: AsyncSession, *, email: str, password: str, display_name: str = "", role: str = "user", token_limit: int = 1_000_000, image_limit: int = 100, max_concurrent_jobs: int = 2, actor_user_id: str | None = None) -> User:
    """Create an invited user and audit, without committing the caller's transaction."""
    email = normalize_email(email)
    validate_password(password)
    display_name = display_name.strip() or email.split("@", 1)[0]
    if role not in {"user", "admin"} or token_limit < 0 or image_limit < 0 or max_concurrent_jobs < 1 or max_concurrent_jobs > 100 or len(display_name) > 100:
        raise ValueError("Invalid role, display name, quota or concurrency")
    encoded = await asyncio.to_thread(hash_password, password)
    user = User(email=email, display_name=display_name, password_hash=encoded, role=role, is_active=True, token_limit=token_limit, image_limit=image_limit, max_concurrent_jobs=max_concurrent_jobs)
    db.add(user)
    await db.flush()
    db.add(AuditEvent(actor_user_id=actor_user_id, target_user_id=user.id, action="user.created", details={"email": email, "role": role, "token_limit": token_limit, "image_limit": image_limit, "max_concurrent_jobs": max_concurrent_jobs}))
    await db.flush()
    return user


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)
    captcha_proof: str | None = Field(default=None, max_length=128)


class CaptchaVerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=1, max_length=128)
    x: float = Field(ge=0, le=272, allow_inf_nan=False)


class PasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class CreateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    display_name: str = Field(default="", max_length=100)
    password: str = Field(min_length=12, max_length=128)
    role: Literal["user", "admin"] = "user"
    token_limit: int | None = Field(default=None, ge=0, le=2**63 - 1)
    image_limit: int = Field(default=100, ge=0, le=2**63 - 1)
    max_concurrent_jobs: int = Field(default=2, ge=1, le=100)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        return normalize_email(value)


class UpdateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_active: bool | None = None
    role: Literal["user", "admin"] | None = None
    token_limit: int | None = Field(default=None, ge=0, le=2**63 - 1)
    image_limit: int | None = Field(default=None, ge=0, le=2**63 - 1)
    max_concurrent_jobs: int | None = Field(default=None, ge=1, le=100)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value):
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()


async def _consume_login_budget(request: Request, db: AsyncSession, email: str) -> None:
    """Consume attempts before password work, atomically across API instances."""
    settings = request.app.state.settings
    peer = request.client.host if request.client else "unknown"
    # Proxy headers are deliberately not trusted here.
    keys = [
        (hashlib.sha256(f"ip:{peer}".encode()).hexdigest(), settings.login_ip_max_attempts),
        (hashlib.sha256(f"account:{email}".encode()).hexdigest(), settings.login_max_attempts),
    ]
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    now = utcnow()
    allowed = True
    # IP gate goes first so random email addresses cannot create unbounded rows
    # after the peer has already exhausted its login budget.
    for key, limit in keys:
        await db.execute(insert(LoginThrottle).values(key=key, count=0, window_started=now).on_conflict_do_nothing(index_elements=["key"]))
        # UPDATE acquires the same cross-process write lock on both databases.
        await db.execute(update(LoginThrottle).where(LoginThrottle.key == key).values(count=LoginThrottle.count + 1))
        throttle = (await db.execute(select(LoginThrottle).where(LoginThrottle.key == key).execution_options(populate_existing=True))).scalar_one()
        if (now - _aware(throttle.window_started)).total_seconds() >= settings.login_window_seconds:
            throttle.count = 1
            throttle.window_started = now
        if throttle.count > limit:
            allowed = False
            break
    # Failed requests must retain their rate limit counters even though they raise.
    await db.commit()
    if not allowed:
        raise error(429, "rate_limited", "Too many sign-in attempts; try again later", headers={"Retry-After": str(settings.login_window_seconds)})


@router.get("/auth/captcha/challenge")
async def captcha_challenge(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    return await login_captcha.issue(request, response, db)


@router.post("/auth/captcha/verify")
async def captcha_verify(body: CaptchaVerifyBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    return await login_captcha.verify(request, response, db, body.challenge_id, body.x)


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    check_origin(request)
    await login_captcha.consume_proof(request, db, body.captcha_proof)
    email = body.email.strip().lower()
    await _consume_login_budget(request, db, email)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    encoded = user.password_hash if user else _DUMMY_PASSWORD
    valid = await asyncio.to_thread(verify_password, body.password, encoded)
    if not valid or user is None or not user.is_active:
        db.add(AuditEvent(action="auth.login_failed", target_user_id=user.id if user else None, details={}))
        await db.commit()
        raise error(401, "invalid_credentials", "Email or password is incorrect")
    # Serialize with password/role revocation: a verified stale password cannot
    # create a new session after a password change has committed.
    await lock_identity(db)
    await db.refresh(user)
    if not user.is_active or not hmac.compare_digest(user.password_hash, encoded):
        await db.rollback()
        raise error(401, "invalid_credentials", "Email or password is incorrect")
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    settings = request.app.state.settings
    auth_session = AuthSession(id=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id, csrf_token=csrf, expires_at=utcnow() + timedelta(hours=settings.session_hours))
    db.add(auth_session)
    db.add(AuditEvent(actor_user_id=user.id, target_user_id=user.id, action="auth.login", details={}))
    await db.commit()
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_hours * 3600, path="/")
    login_captcha.clear_cookie(request, response)
    response.headers["Cache-Control"] = "no-store"
    return {"user": public_user(user), "csrf_token": csrf}


@router.get("/auth/me")
async def me(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user = await require_user(request, db)
    response.headers["Cache-Control"] = "no-store"
    return {"user": public_user(user), "csrf_token": request.state.csrf_token}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user = await require_user(request, db)
    await db.delete(request.state.cloud_session)
    db.add(AuditEvent(actor_user_id=user.id, target_user_id=user.id, action="auth.logout", details={}))
    await db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=request.app.state.settings.cookie_secure, httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/auth/password")
async def change_password(body: PasswordBody, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    user = await require_user(request, db)
    await lock_identity(db)
    await db.refresh(user)
    if not user.is_active or not await asyncio.to_thread(verify_password, body.current_password, user.password_hash):
        raise error(403, "invalid_password", "The current password is incorrect")
    user.password_hash = await asyncio.to_thread(hash_password, body.new_password)
    await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    db.add(AuditEvent(actor_user_id=user.id, target_user_id=user.id, action="auth.password_changed", details={"sessions_revoked": True}))
    await db.commit()
    response.delete_cookie(COOKIE_NAME, path="/", secure=request.app.state.settings.cookie_secure, httponly=True, samesite="lax")
    return {"ok": True}


@router.get("/admin/users")
async def list_users(request: Request, db: AsyncSession = Depends(get_db)):
    await require_admin(request, db)
    users = (await db.execute(select(User).order_by(User.created_at, User.id))).scalars().all()
    return [public_user(user) for user in users]


async def _refresh_admin(actor: User, db: AsyncSession) -> None:
    await db.refresh(actor)
    if not actor.is_active or actor.role != "admin":
        raise error(403, "forbidden", "Administrator access has been revoked")


@router.post("/admin/users", status_code=201)
async def add_user(body: CreateUserBody, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await require_admin(request, db)
    await lock_identity(db)
    await _refresh_admin(actor, db)
    try:
        user = await create_user(db, **body.model_dump(exclude={"token_limit"}), token_limit=body.token_limit if body.token_limit is not None else request.app.state.settings.default_token_limit, actor_user_id=actor.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise error(409, "email_exists", "A user with this email already exists")
    return public_user(user)


@router.patch("/admin/users/{user_id}")
async def modify_user(user_id: str, body: UpdateUserBody, request: Request, db: AsyncSession = Depends(get_db)):
    actor = await require_admin(request, db)
    await lock_identity(db)
    await _refresh_admin(actor, db)
    user = await db.get(User, user_id, populate_existing=True)
    if user is None:
        raise error(404, "not_found", "User not found")
    changes = body.model_dump(exclude={"reason"}, exclude_none=True)
    resulting_active = changes.get("is_active", user.is_active)
    resulting_role = changes.get("role", user.role)
    if user.is_active and user.role == "admin" and not (resulting_active and resulting_role == "admin"):
        count = await db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True), User.role == "admin"))
        if count <= 1:
            raise error(409, "last_admin", "The last active administrator cannot be disabled or demoted")
    previous = {key: getattr(user, key) for key in changes}
    for key, value in changes.items():
        setattr(user, key, value)
    if "token_limit" in changes:
        # Quotas change immediately without discarding used or reserved usage.
        from zoneinfo import ZoneInfo
        period = utcnow().astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
        await db.execute(update(QuotaBucket).where(QuotaBucket.user_id == user.id, QuotaBucket.period == period).values(limit_tokens=user.token_limit, revision=QuotaBucket.revision + 1))
    if "image_limit" in changes:
        from app.cloud.metering import billing_period
        await db.execute(update(ImageQuotaBucket).where(ImageQuotaBucket.user_id == user.id,
            ImageQuotaBucket.period == billing_period()).values(limit_images=user.image_limit, revision=ImageQuotaBucket.revision + 1))
    if ("is_active" in changes and not user.is_active) or "role" in changes:
        await db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    db.add(AuditEvent(actor_user_id=actor.id, target_user_id=user.id, action="user.updated", details={"before": previous, "after": changes, "reason": body.reason}))
    await db.commit()
    if "is_active" in changes and not user.is_active:
        # Capability authentication already rejects disabled accounts. Stop the
        # private scheduler/tools as well; retain storage for later reactivation.
        runtime = getattr(request.app.state, "runtime_supervisor", None)
        if runtime:
            from app.cloud.runtime import RuntimeUnavailable
            from contextlib import suppress
            with suppress(RuntimeUnavailable, TimeoutError):
                await runtime.stop(user.id)
    return public_user(user)
