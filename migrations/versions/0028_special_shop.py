"""special shop: greenhouse and weekly deals

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-06

- farms.greenhouse_level：溫室等級（0–10，預設 0），之後澆水的作物生長時間依等級減少。
- weekly_shop_purchases：每週特賣的購買量（每人每週每項限購）。
- 溫室與稱號商店的購買沿用交易類型 buy_item，每週特賣沿用 buy_seed，不新增交易類型。

降版會刪除溫室等級與每週特賣的購買量（已扣的金幣與買到的種子保留，交易紀錄不變）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("farms", sa.Column("greenhouse_level", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint(op.f("ck_farms_greenhouse_level_valid"), "farms", "greenhouse_level BETWEEN 0 AND 10")
    op.create_table(
        "weekly_shop_purchases",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE",
                                name=op.f("fk_weekly_shop_purchases_farm_id_farms")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_weekly_shop_purchases_quantity_positive")),
        sa.PrimaryKeyConstraint("farm_id", "week_start", "item_code", name=op.f("pk_weekly_shop_purchases")),
    )


def downgrade() -> None:
    op.drop_table("weekly_shop_purchases")
    op.drop_constraint(op.f("ck_farms_greenhouse_level_valid"), "farms", type_="check")
    op.drop_column("farms", "greenhouse_level")
