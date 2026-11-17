"""processing building upgrades

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-17

Go 版新增（Python 版沒有對應的程式）。
- farm_buildings.level：加工設施（磨坊、烘焙坊、果醬工坊）的升級等級，0 = 未升級，最多 3 級；
  每級縮短加工時間（15%／40%／60%）。畜舍不使用，維持 0。CHECK 0–3。

降版會刪除這個欄位（已花的金幣不退）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("farm_buildings", sa.Column("level", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.create_check_constraint(op.f("ck_farm_buildings_level_range"), "farm_buildings", "level BETWEEN 0 AND 3")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_farm_buildings_level_range"), "farm_buildings", type_="check")
    op.drop_column("farm_buildings", "level")
