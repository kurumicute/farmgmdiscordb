"""auto fishing machine and fish basket upgrades

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-15

Go 版新增（Python 版沒有對應的程式）。
- farms.fish_basket_level：魚簍升級等級（0 = 沒有升級，用釣魚設定的 basket_capacity；各級容量與價格在管理網站
  「自動釣魚與魚簍」調整）。CHECK 0–10。
- farm_fish.session_id 改成可以是 NULL：自動釣魚機釣到的魚沒有釣魚 Session。
- farm_fish.auto：是否為自動釣魚機釣到的魚（不算進釣魚排行榜）。

降版會刪除自動釣魚機釣到的魚（session_id 為 NULL），再改回原本的欄位。
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("farms", sa.Column("fish_basket_level", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.create_check_constraint(op.f("ck_farms_fish_basket_level_range"), "farms", "fish_basket_level BETWEEN 0 AND 10")
    op.alter_column("farm_fish", "session_id", existing_type=sa.BigInteger(), nullable=True)
    op.add_column("farm_fish", sa.Column("auto", sa.Boolean(), server_default=sa.text("false"), nullable=False))


def downgrade() -> None:
    op.execute("DELETE FROM farm_fish WHERE session_id IS NULL")
    op.drop_column("farm_fish", "auto")
    op.alter_column("farm_fish", "session_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_constraint(op.f("ck_farms_fish_basket_level_range"), "farms", type_="check")
    op.drop_column("farms", "fish_basket_level")
