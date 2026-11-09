"""raise all fish prices by 1.5x

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-09

使用者要求所有魚的價格調高 1.5 倍（四捨五入，.5 進位）：
- fish_types：最低與最高售價（包含管理員新增或修改過的魚）。
- farm_fish：還沒賣出也沒捐出的魚；已賣出的 sold_price 與捐出的魚是歷史紀錄，不變（交易紀錄與全服金幣不受影響）。
- fishing_sessions：進行中（casting／fighting）的拋竿，釣到時售價才會是新價格。

降版除以 1.5 再四捨五入：對這裡乘過的整數可以還原成原值；升級後才由管理員改過的價格與新釣到的魚同樣除以 1.5。
"""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

UNSOLD = "sold_at IS NULL AND donated_at IS NULL"
ACTIVE = "status IN ('casting', 'fighting')"


def upgrade() -> None:
    op.execute("UPDATE fish_types SET min_price = ROUND(min_price * 1.5)::int, max_price = ROUND(max_price * 1.5)::int")
    op.execute(f"UPDATE farm_fish SET price = ROUND(price * 1.5)::int WHERE {UNSOLD}")
    op.execute(f"UPDATE fishing_sessions SET price = ROUND(price * 1.5)::int WHERE {ACTIVE}")


def downgrade() -> None:
    op.execute("UPDATE fish_types SET min_price = ROUND(min_price / 1.5)::int, max_price = ROUND(max_price / 1.5)::int")
    op.execute(f"UPDATE farm_fish SET price = ROUND(price / 1.5)::int WHERE {UNSOLD}")
    op.execute(f"UPDATE fishing_sessions SET price = ROUND(price / 1.5)::int WHERE {ACTIVE}")
