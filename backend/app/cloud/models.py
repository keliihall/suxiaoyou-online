"""Cloud-owned metadata; intentionally independent from desktop ORM models."""

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class CloudBase(DeclarativeBase):
    pass


class User(CloudBase):
    __tablename__ = "cloud_users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="ck_cloud_user_role"),
        CheckConstraint("token_limit >= 0 AND image_limit >= 0 AND max_concurrent_jobs > 0", name="ck_cloud_user_limits"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_limit: Mapped[int] = mapped_column(BigInteger, default=1_000_000, nullable=False)
    image_limit: Mapped[int] = mapped_column(BigInteger, default=100, nullable=False)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuthSession(CloudBase):
    __tablename__ = "cloud_auth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Workspace(CloudBase):
    __tablename__ = "cloud_workspaces"
    __table_args__ = (UniqueConstraint("id", "user_id", name="uq_cloud_workspace_owner"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Conversation(CloudBase):
    __tablename__ = "cloud_conversations"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_cloud_conversation_owner"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ["cloud_workspaces.id", "cloud_workspaces.user_id"], name="fk_cloud_conversation_workspace_owner"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="New conversation", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(CloudBase):
    __tablename__ = "cloud_messages"
    __table_args__ = (ForeignKeyConstraint(["conversation_id", "user_id"], ["cloud_conversations.id", "cloud_conversations.user_id"], name="fk_cloud_message_conversation_owner", ondelete="CASCADE"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FileAsset(CloudBase):
    __tablename__ = "cloud_file_assets"
    __table_args__ = (ForeignKeyConstraint(["workspace_id", "user_id"], ["cloud_workspaces.id", "cloud_workspaces.user_id"], name="fk_cloud_file_workspace_owner"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Job(CloudBase):
    __tablename__ = "cloud_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_cloud_job_idempotency"),
        UniqueConstraint("id", "user_id", name="uq_cloud_job_owner"),
        ForeignKeyConstraint(["conversation_id", "user_id"], ["cloud_conversations.id", "cloud_conversations.user_id"], name="fk_cloud_job_conversation_owner"),
        CheckConstraint("state IN ('queued','running','completed','failed','cancelled','needs_review')", name="ck_cloud_job_state"),
        Index("ix_cloud_job_state_created", "state", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=1024, nullable=False)
    file_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    answer: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    fencing: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class JobEvent(CloudBase):
    __tablename__ = "cloud_job_events"
    __table_args__ = (ForeignKeyConstraint(["job_id", "user_id"], ["cloud_jobs.id", "cloud_jobs.user_id"], name="fk_cloud_event_job_owner", ondelete="CASCADE"),)
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ModelRoute(CloudBase):
    __tablename__ = "cloud_model_routes"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default="chat", nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("cloud_users.id"), nullable=True, index=True)
    image_price_microusd: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    api_key_env: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # Write-only managed credentials. Public views and ledger snapshots never
    # serialize ciphertext, the master key or decrypted API keys.
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_headers_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_unauthenticated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    strict_quota: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_price_microusd_per_million: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_price_microusd_per_million: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TenantRuntime(CloudBase):
    """One complete client engine per user; never share in-process singletons."""
    __tablename__ = "cloud_tenant_runtimes"
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    capability_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    capability_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    engine_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    container_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="stopped", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RequestRecord(CloudBase):
    __tablename__ = "cloud_request_records"
    __table_args__ = (
        CheckConstraint("state IN ('reserved','dispatched','settled','released','unknown')", name="ck_cloud_request_state"),
        CheckConstraint("reserved_tokens >= 0 AND total_tokens >= 0 AND cost_microusd >= 0", name="ck_cloud_request_nonnegative"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    # Logical IDs intentionally have no cascading FK to deletable content.
    job_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="chat", nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="reserved", nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    raw_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    normalized_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Immutable admission-time server pricing; absent historical prices remain
    # explicitly unknown rather than being repriced from a later model route.
    price_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuotaBucket(CloudBase):
    __tablename__ = "cloud_quota_buckets"
    __table_args__ = (UniqueConstraint("user_id", "period", name="uq_cloud_quota_period"), CheckConstraint("limit_tokens >= 0 AND used_tokens >= 0 AND reserved_tokens >= 0", name="ck_cloud_bucket_nonnegative"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    limit_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class Reservation(CloudBase):
    __tablename__ = "cloud_reservations"
    __table_args__ = (
        UniqueConstraint("request_id", "bucket_id", name="uq_cloud_reservation_request_bucket"),
        CheckConstraint("amount >= 0", name="ck_cloud_reservation_nonnegative"),
        CheckConstraint("state IN ('reserved','dispatched','settled','released','unknown')", name="ck_cloud_reservation_state"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(ForeignKey("cloud_request_records.id"), index=True, nullable=False)
    bucket_id: Mapped[str] = mapped_column(ForeignKey("cloud_quota_buckets.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="reserved", nullable=False)


class UsageEvent(CloudBase):
    __tablename__ = "cloud_usage_events"
    __table_args__ = (CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND cache_read >= 0 AND cache_write >= 0 AND reasoning >= 0 AND total_tokens >= 0 AND cost_microusd >= 0", name="ck_cloud_usage_nonnegative"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    request_id: Mapped[str] = mapped_column(ForeignKey("cloud_request_records.id"), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_read: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cache_write: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reasoning: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    raw_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(CloudBase):
    __tablename__ = "cloud_audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    target_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class IdentityState(CloudBase):
    """One mutex row serializes administrator invariants across processes."""
    __tablename__ = "cloud_identity_state"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class LoginThrottle(CloudBase):
    """Database-backed counters shared by all API instances; never store email/IP."""
    __tablename__ = "cloud_login_throttle"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LoginCaptcha(CloudBase):
    """Ephemeral puzzle/proof digests shared by API processes; no account data."""
    __tablename__ = "cloud_login_captcha"
    __table_args__ = (
        CheckConstraint("kind IN ('challenge', 'proof')", name="ck_cloud_captcha_kind"),
        CheckConstraint("(kind = 'challenge' AND target_x IS NOT NULL AND target_x >= 0 AND target_x <= 272) OR (kind = 'proof' AND target_x IS NULL)", name="ck_cloud_captcha_target"),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class ImageQuotaBucket(CloudBase):
    __tablename__ = "cloud_image_quota_buckets"
    __table_args__ = (UniqueConstraint("user_id", "period", name="uq_cloud_image_period"),
        CheckConstraint("limit_images >= 0 AND used_images >= 0 AND reserved_images >= 0", name="ck_cloud_image_counts"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    limit_images: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_images: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_images: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class ImageReservation(CloudBase):
    __tablename__ = "cloud_image_reservations"
    __table_args__ = (UniqueConstraint("user_id", "operation_key", name="uq_cloud_image_operation"),
        CheckConstraint("state IN ('reserved','settled','released')", name="ck_cloud_image_reservation_state"))
    request_id: Mapped[str] = mapped_column(ForeignKey("cloud_request_records.id"), primary_key=True)
    bucket_id: Mapped[str] = mapped_column(ForeignKey("cloud_image_quota_buckets.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)


class MlxNodeConfig(CloudBase):
    """Operator-assigned exclusive Apple Silicon node, never copied to engines."""
    __tablename__ = "cloud_mlx_nodes"
    __table_args__ = (UniqueConstraint("certificate_fingerprint", name="uq_cloud_mlx_node_certificate"),
                     UniqueConstraint("url", name="uq_cloud_mlx_node_origin"))
    user_id: Mapped[str] = mapped_column(ForeignKey("cloud_users.id"), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    certificate_pem: Mapped[str] = mapped_column(Text, nullable=False)
    certificate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
