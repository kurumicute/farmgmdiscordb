"""farm pets (cat, dog)

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-06

- farm_pets：農場的寵物（貓、狗），每種一隻，等級 0–10（目前最高 4）。
- 購買與升級沿用交易類型 buy_item（item_code pet_cat／pet_dog）；狗擋下偷菜時 steal_records.blocked_by = 'pet_dog'
  （blocked_by 沒有限定值的 CHECK，不用改）。

降版會刪除寵物（已扣的金幣保留，交易紀錄不變）；被狗擋下的偷菜紀錄保留。
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "farm_pets",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("pet_code", sa.String(16), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("bought_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE", name=op.f("fk_farm_pets_farm_id_farms")),
        sa.CheckConstraint("pet_code IN ('cat', 'dog')", name=op.f("ck_farm_pets_pet_code_valid")),
        sa.CheckConstraint("level BETWEEN 0 AND 10", name=op.f("ck_farm_pets_level_valid")),
        sa.PrimaryKeyConstraint("farm_id", "pet_code", name=op.f("pk_farm_pets")),
    )


def downgrade() -> None:
    op.drop_table("farm_pets")
