"""farm feedback (last panel visit and personal records)

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-08

- farm_feedback：每座農場一列，last_seen_at 是上次打開 Discord 面板的時間（「你不在的時候」摘要），
  records 是個人紀錄（例如一次收成最多幾個、一次販售最多幾金）。只用於顯示，不影響任何遊戲數值。

降版會刪除這張表（摘要與個人紀錄重新開始累計）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "farm_feedback",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("records", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_farm_feedback_farm_id_farms"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("farm_id", name=op.f("pk_farm_feedback")),
    )


def downgrade() -> None:
    op.drop_table("farm_feedback")
