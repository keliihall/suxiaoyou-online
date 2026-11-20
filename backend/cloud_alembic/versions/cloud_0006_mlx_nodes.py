"""Exclusive operator-managed Mac nodes with encrypted credentials."""
from alembic import op
import sqlalchemy as sa

revision = "cloud_0006"
down_revision = "cloud_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("cloud_mlx_nodes",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("cloud_users.id"), primary_key=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("certificate_pem", sa.Text(), nullable=False),
        sa.Column("certificate_fingerprint", sa.String(64), nullable=False),
        sa.Column("token_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("certificate_fingerprint", name="uq_cloud_mlx_node_certificate"),
        sa.UniqueConstraint("url", name="uq_cloud_mlx_node_origin"))


def downgrade():
    op.drop_table("cloud_mlx_nodes")
