"""rebalance initial crop seed and sell prices

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-30

作物收入太高，調整初始 4 種作物的種子價格與單位售價（收成數量、生長時間不變）。
目標：每天登入 2 次、4 格農田全種草莓，約第 3 天買得起自動澆水器（3,500），
第 7 天累積約 8,000（全部加工設施），第 10 天累積約 11,000（加工設施＋3 座畜舍）。
直接覆蓋目前數值（包含管理員在網站調整過的值）。

降版：恢復成 Migration 0001 的初始價格（升級前若曾在網站調整過，舊值不會還原）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# code: (種子價格, 單位售價)
NEW_PRICES = {"wheat": (5, 11), "carrot": (12, 15), "tomato": (25, 22), "strawberry": (60, 34)}
OLD_PRICES = {"wheat": (10, 15), "carrot": (20, 25), "tomato": (35, 20), "strawberry": (60, 25)}


def _set(prices: dict[str, tuple[int, int]]) -> None:
    for code, (seed, sell) in prices.items():
        op.execute(
            sa.text("UPDATE crop_types SET seed_price = :seed, sell_price = :sell WHERE code = :code").bindparams(
                seed=seed, sell=sell, code=code
            )
        )


def upgrade() -> None:
    _set(NEW_PRICES)


def downgrade() -> None:
    _set(OLD_PRICES)
