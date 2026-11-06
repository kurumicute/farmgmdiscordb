"""farm codex

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-06

- farm_codex：圖鑑（每座農場第一次取得某物品的時間、累計取得數量與最高品質 0–3）。
- 從既有資料補建：交易紀錄中的收成、偷菜、收集動物產品、領取加工品，以及已領取探險的獎勵明細。
  魚類圖鑑沿用 farm_fish，不在這張表。

降版只刪除 farm_codex（可由歷史資料重建，不影響資產）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)

BACKFILL = """
INSERT INTO farm_codex (farm_id, item_code, quantity, best_quality, first_at)
SELECT farm_id, item_code, sum(quantity), max(rank), min(at)
FROM (
    SELECT farm_id, item_code, quantity,
           CASE quality WHEN 'good' THEN 1 WHEN 'rare' THEN 2 WHEN 'legendary' THEN 3 ELSE 0 END AS rank,
           created_at AS at
    FROM game_transactions
    WHERE event_type IN ('harvest', 'steal', 'collect_animal', 'craft_collect')
      AND item_code IS NOT NULL AND quantity > 0
    UNION ALL
    SELECT l.farm_id, r.key, r.value::bigint, 0, l.claimed_at
    FROM exploration_logs l CROSS JOIN LATERAL json_each_text(l.rewards) AS r
    WHERE l.claimed_at IS NOT NULL AND r.value::bigint > 0
) AS t
GROUP BY farm_id, item_code
"""


def upgrade() -> None:
    op.create_table(
        "farm_codex",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("best_quality", sa.Integer(), nullable=False),
        sa.Column("first_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE",
                                name=op.f("fk_farm_codex_farm_id_farms")),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_farm_codex_quantity_non_negative")),
        sa.CheckConstraint("best_quality BETWEEN 0 AND 3", name=op.f("ck_farm_codex_best_quality_valid")),
        sa.PrimaryKeyConstraint("farm_id", "item_code", name=op.f("pk_farm_codex")),
    )
    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_table("farm_codex")
