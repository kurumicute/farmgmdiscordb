"""fish prices are no longer bound to rarity bands

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-02

- fish_types：移除「售價必須在稀有度區間內」的 CHECK（普通 1–20、稀有 21–50、史詩 51–100、傳說 101–200），
  改為只要求最低售價 ≥ 0（最低不高於最高的 CHECK 保留）。售價由管理網站自由設定。

降版時重新加上稀有度區間 CHECK，但以 NOT VALID 建立：不檢查已存在的資料（升級後可能已有超出區間的魚種），
只限制之後新增或修改的資料；不會改動任何魚種資料。
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

PRICE_BAND = (
    "(rarity = 'common' AND min_price >= 1 AND max_price <= 20) OR "
    "(rarity = 'rare' AND min_price >= 21 AND max_price <= 50) OR "
    "(rarity = 'epic' AND min_price >= 51 AND max_price <= 100) OR "
    "(rarity = 'legendary' AND min_price >= 101 AND max_price <= 200)"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_fish_types_price_in_rarity_band"), "fish_types", type_="check")
    op.create_check_constraint(op.f("ck_fish_types_price_non_negative"), "fish_types", "min_price >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_fish_types_price_non_negative"), "fish_types", type_="check")
    op.execute(f"ALTER TABLE fish_types ADD CONSTRAINT ck_fish_types_price_in_rarity_band CHECK ({PRICE_BAND}) NOT VALID")
