"""feature settings (editable rules for check-in, pets, special shop, achievements)

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-08

- feature_settings：管理網站可調整的新功能規則，每個 key（checkin／pets／special_shop／achievements）一列 JSON。
  沒有資料列時使用程式內的預設值，因此升版後遊戲數值不變。

降版會刪除這張表（之後回到程式內的預設值）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_settings",
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_feature_settings")),
    )


def downgrade() -> None:
    op.drop_table("feature_settings")
