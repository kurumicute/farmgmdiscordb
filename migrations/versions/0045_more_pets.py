"""more pets (rabbit, bird, hedgehog)

Revision ID: 0045
Revises: 0044
Create Date: 2026-11-18

Go 版新增（Python 版沒有對應的程式）。
- farm_pets.pet_code 的 CHECK 從 ('cat', 'dog') 放寬為 ('cat', 'dog', 'rabbit', 'bird', 'hedgehog')。
  兔子：地下菇房收成數量增加；小鳥：釣魚稀有魚機率提高；刺蝟：清除乾枯作物時救回一部分作物。
- 購買與升級沿用交易類型 buy_item（item_code pet_rabbit／pet_bird／pet_hedgehog），不用改交易類型。

降版會刪除兔子、小鳥、刺蝟（已扣的金幣保留，交易紀錄不變），再把 CHECK 改回只有貓狗。
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

PETS_0044 = ("cat", "dog")
NEW_PETS = ("rabbit", "bird", "hedgehog")


def _set_pets(pets: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_farm_pets_pet_code_valid"), "farm_pets", type_="check")
    op.create_check_constraint(
        op.f("ck_farm_pets_pet_code_valid"), "farm_pets",
        "pet_code IN (" + ", ".join(f"'{v}'" for v in pets) + ")",
    )


def upgrade() -> None:
    _set_pets(PETS_0044 + NEW_PETS)


def downgrade() -> None:
    op.execute("DELETE FROM farm_pets WHERE pet_code IN (" + ", ".join(f"'{v}'" for v in NEW_PETS) + ")")
    _set_pets(PETS_0044)
