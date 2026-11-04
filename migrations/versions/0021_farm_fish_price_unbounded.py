"""caught fish prices follow fish_types (no 1–200 cap)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-04

0017 移除了 fish_types 的稀有度售價區間（最低售價只要求 ≥ 0），但 farm_fish 的 CHECK 仍是
「price BETWEEN 1 AND 200」：管理網站把魚種售價設超過 200（或最低 0）後，釣到的魚寫入 farm_fish 失敗，
拉扯最後一步一直顯示「發生未預期的錯誤」直到魚逃脫。改成與 fish_types 一致：price ≥ 0。

降版時重新加上 1–200 的 CHECK，但以 NOT VALID 建立：不檢查已存在的資料，只限制之後新增或修改的資料。
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_farm_fish_price_range"), "farm_fish", type_="check")
    op.create_check_constraint(op.f("ck_farm_fish_price_non_negative"), "farm_fish", "price >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_farm_fish_price_non_negative"), "farm_fish", type_="check")
    op.execute("ALTER TABLE farm_fish ADD CONSTRAINT ck_farm_fish_price_range CHECK (price BETWEEN 1 AND 200) NOT VALID")
