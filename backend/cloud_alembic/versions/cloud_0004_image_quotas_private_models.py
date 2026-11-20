"""Independent image quotas and private BYOK routes, never token equivalents."""
from alembic import op
import sqlalchemy as sa
revision = "cloud_0004"
down_revision = "cloud_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cloud_users", sa.Column("image_limit", sa.BigInteger(), nullable=False, server_default="100"))
    op.add_column("cloud_model_routes", sa.Column("kind", sa.String(16), nullable=False, server_default="chat"))
    op.add_column("cloud_model_routes", sa.Column("image_price_microusd", sa.BigInteger(), nullable=False, server_default="0"))
    with op.batch_alter_table("cloud_model_routes") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.String(32), nullable=True))
        batch.create_foreign_key("fk_cloud_model_owner", "cloud_users", ["owner_user_id"], ["id"])
        batch.create_index("ix_cloud_model_routes_owner_user_id", ["owner_user_id"])
    op.create_table("cloud_image_quota_buckets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("cloud_users.id"), nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("limit_images", sa.BigInteger(), nullable=False),
        sa.Column("used_images", sa.BigInteger(), nullable=False),
        sa.Column("reserved_images", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("user_id", "period", name="uq_cloud_image_period"),
        sa.CheckConstraint("limit_images >= 0 AND used_images >= 0 AND reserved_images >= 0", name="ck_cloud_image_counts"))
    op.create_table("cloud_image_reservations",
        sa.Column("request_id", sa.String(32), sa.ForeignKey("cloud_request_records.id"), primary_key=True),
        sa.Column("bucket_id", sa.String(32), sa.ForeignKey("cloud_image_quota_buckets.id"), nullable=False),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("cloud_users.id"), nullable=False),
        sa.Column("operation_key", sa.String(160), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.UniqueConstraint("user_id", "operation_key", name="uq_cloud_image_operation"),
        sa.CheckConstraint("state IN ('reserved','settled','released')", name="ck_cloud_image_reservation_state"))


def downgrade():
    op.drop_table("cloud_image_reservations")
    op.drop_table("cloud_image_quota_buckets")
    with op.batch_alter_table("cloud_model_routes") as batch:
        batch.drop_index("ix_cloud_model_routes_owner_user_id")
        batch.drop_constraint("fk_cloud_model_owner", type_="foreignkey")
        batch.drop_column("owner_user_id")
    op.drop_column("cloud_model_routes", "image_price_microusd")
    op.drop_column("cloud_model_routes", "kind")
    op.drop_column("cloud_users", "image_limit")
