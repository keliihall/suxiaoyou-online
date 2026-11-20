"""Independent complete desktop engine runtime per cloud user."""
from alembic import op
import sqlalchemy as sa

revision = "cloud_0003"
down_revision = "cloud_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cloud_request_records", sa.Column("lease_owner", sa.String(64), nullable=True))
    op.add_column("cloud_request_records", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.create_table("cloud_tenant_runtimes",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("cloud_users.id"), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("capability_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("capability_encrypted", sa.Text(), nullable=False),
        sa.Column("engine_token_encrypted", sa.Text(), nullable=False),
        sa.Column("container_name", sa.String(120), nullable=False, unique=True),
        sa.Column("container_id", sa.String(128)),
        sa.Column("host_port", sa.Integer()),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("cloud_tenant_runtimes")
    op.drop_column("cloud_request_records", "lease_until")
    op.drop_column("cloud_request_records", "lease_owner")
