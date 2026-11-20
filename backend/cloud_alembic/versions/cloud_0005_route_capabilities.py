"""Preserve actual model capabilities through opaque platform aliases."""
from alembic import op
import sqlalchemy as sa
revision = "cloud_0005"
down_revision = "cloud_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cloud_model_routes", sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade():
    op.drop_column("cloud_model_routes", "capabilities")
