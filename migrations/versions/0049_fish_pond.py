"""fish pond: fish raised in the pond and bred fry

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-24

Go 版新增（Python 版沒有對應的程式），魚塘（設計見 Go 專案「魚塘_流程圖.md」）。
- farm_fish.pond_weight_g：有值表示這隻魚放在魚塘裡，數值是養殖後的重量（捕獲時的 weight_g、price 不變，
  排行榜與圖鑑照舊讀捕獲時的數值）。魚塘裡的魚不在魚簍中，只能從魚塘賣出。CHECK 大於 0。
- farm_fish.bred：魚塘繁殖出來的魚苗（不算進釣魚排行榜、捕獲數與日報）。
- ix_farm_fish_farm_pond：讀魚塘裡還沒賣掉的魚。
魚塘的等級、飼料與結算時間存在 farm_tools（tool_code = fish_pond）的 settings；建造、升級、補飼料記成 buy_item，
賣魚記成 fish_sell，不需要新的交易類型。

降版會刪除魚苗（bred），魚塘裡其他還沒賣掉的魚回到魚簍（保留捕獲時的重量與價格），再刪除兩個欄位。
魚塘的設定（farm_tools）留在資料庫，Python 版不會讀取。
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("farm_fish", sa.Column("pond_weight_g", sa.Integer(), nullable=True))
    op.add_column("farm_fish", sa.Column("bred", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_check_constraint(
        op.f("ck_farm_fish_pond_weight_positive"), "farm_fish", "pond_weight_g IS NULL OR pond_weight_g > 0"
    )
    op.create_index(
        "ix_farm_fish_farm_pond", "farm_fish", ["farm_id"],
        postgresql_where=sa.text("pond_weight_g IS NOT NULL AND sold_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM fish_shares WHERE fish_id IN (SELECT id FROM farm_fish WHERE bred)")
    op.execute("DELETE FROM farm_fish WHERE bred")
    op.drop_index("ix_farm_fish_farm_pond", table_name="farm_fish")
    op.drop_constraint(op.f("ck_farm_fish_pond_weight_positive"), "farm_fish", type_="check")
    op.drop_column("farm_fish", "bred")
    op.drop_column("farm_fish", "pond_weight_g")
