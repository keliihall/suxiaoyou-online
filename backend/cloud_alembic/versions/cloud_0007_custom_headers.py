"""Write-only custom endpoint headers and explicit public endpoint mode."""
from alembic import op
import sqlalchemy as sa

revision = "cloud_0007"
down_revision = "cloud_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cloud_model_routes", sa.Column("custom_headers_encrypted", sa.Text(), nullable=True))
    op.add_column("cloud_model_routes", sa.Column("allow_unauthenticated", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("cloud_model_routes", "allow_unauthenticated")
    op.drop_column("cloud_model_routes", "custom_headers_encrypted")
