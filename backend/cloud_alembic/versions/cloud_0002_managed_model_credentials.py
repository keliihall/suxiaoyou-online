"""Write-only encrypted credentials for administrator-configured models.

Revision ID: cloud_0002
Revises: cloud_0001
"""
from alembic import op
import sqlalchemy as sa

revision = "cloud_0002"
down_revision = "cloud_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cloud_model_routes", sa.Column("api_key_encrypted", sa.Text(), nullable=True))
    op.add_column("cloud_model_routes", sa.Column("credential_version", sa.Integer(), server_default="0", nullable=False))


def downgrade():
    op.drop_column("cloud_model_routes", "credential_version")
    op.drop_column("cloud_model_routes", "api_key_encrypted")
