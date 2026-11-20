"""Scoped gateway identity. Tenant engines never receive cloud account cookies."""
import hashlib

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cloud.models import TenantRuntime, User


async def get_runtime_principal(request: Request, db: AsyncSession) -> User:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 1024:
        raise HTTPException(401, detail={"code": "runtime_unauthorized", "message": "运行实例凭证无效"})
    digest = hashlib.sha256(authorization[7:].strip().encode()).hexdigest()
    user = await db.scalar(select(User).join(TenantRuntime, TenantRuntime.user_id == User.id).where(
        TenantRuntime.capability_hash == digest, TenantRuntime.state.in_(["starting", "running"]),
        User.is_active.is_(True)))
    if user is None:
        raise HTTPException(401, detail={"code": "runtime_unauthorized", "message": "运行实例凭证已撤销"})
    return user
