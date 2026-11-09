"""fishing anti-script guard

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-09

- fishing_guards：每座農場一列（第一次拋竿時建立），記錄距離上次人機確認的拋竿次數、可疑分數、題目與暫停時間。
  只影響能不能拋竿，不影響任何資產。

降版會刪除這張表（進行中的題目與暫停都會消失，之後重新累計）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fishing_guards",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("casts_since_check", sa.Integer(), server_default="0", nullable=False),
        sa.Column("check_after", sa.Integer(), nullable=False),
        sa.Column("suspicion", sa.Integer(), server_default="0", nullable=False),
        sa.Column("challenge", sa.JSON(), nullable=True),
        sa.Column("challenge_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("passed_total", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("casts_since_check >= 0 AND check_after > 0 AND suspicion >= 0",
                           name=op.f("ck_fishing_guards_counters_valid")),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_fishing_guards_farm_id_farms"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("farm_id", name=op.f("pk_fishing_guards")),
    )


def downgrade() -> None:
    op.drop_table("fishing_guards")
