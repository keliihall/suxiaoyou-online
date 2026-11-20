"""Add expiring one-use browser-bound login challenges and proofs."""
from alembic import op
import sqlalchemy as sa

revision = "cloud_0008"
down_revision = "cloud_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cloud_login_captcha",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("target_x", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('challenge', 'proof')", name="ck_cloud_captcha_kind"),
        sa.CheckConstraint("(kind = 'challenge' AND target_x IS NOT NULL AND target_x >= 0 AND target_x <= 272) OR (kind = 'proof' AND target_x IS NULL)", name="ck_cloud_captcha_target"),
    )
    op.create_index("ix_cloud_login_captcha_browser_hash", "cloud_login_captcha", ["browser_hash"])
    op.create_index("ix_cloud_login_captcha_expires_at", "cloud_login_captcha", ["expires_at"])


def downgrade():
    op.drop_index("ix_cloud_login_captcha_expires_at", table_name="cloud_login_captcha")
    op.drop_index("ix_cloud_login_captcha_browser_hash", table_name="cloud_login_captcha")
    op.drop_table("cloud_login_captcha")
