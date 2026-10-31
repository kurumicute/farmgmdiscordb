"""cave exploration trips: rewards are claimed after the trip ends, one trip at a time

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-31

- exploration_logs.claimed_at：探險改為「出發 → 經過探險時間 → 回來後領取獎勵」，領取時才把獎勵放進背包。
  既有紀錄在 0014 時已立即發放，升版時把 claimed_at 設為出發時間。
- uq_exploration_logs_farm_unclaimed：每座農場最多一筆未領取（進行中或待領取）的探險，
  與服務層鎖定農場列的檢查一起保證一次只能去一個區域。

降版會刪除索引與欄位；尚未領取的探險獎勵（從未放進背包）會一併視為已發放，不會補發，降版前請先確認。
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("exploration_logs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE exploration_logs SET claimed_at = explored_at")
    op.create_index(
        "uq_exploration_logs_farm_unclaimed", "exploration_logs", ["farm_id"], unique=True,
        postgresql_where=sa.text("claimed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_exploration_logs_farm_unclaimed", table_name="exploration_logs")
    op.drop_column("exploration_logs", "claimed_at")
