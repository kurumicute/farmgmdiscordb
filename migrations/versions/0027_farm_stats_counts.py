"""farm_stats action counts

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-06

- farm_stats：新增 steal_count、defended_count、gamble_count（偷菜成功、防守成功、賭場次數），
  由 GameService._record 遞增；成就進度改讀這三欄，不必每次掃描玩家的全部交易紀錄。
- 從既有交易紀錄補上目前的次數（scripts/farm_stats.py check 會一併比對）。

降版只刪除這三欄（可由交易紀錄重算，不影響資產）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

COLUMNS = ("steal_count", "defended_count", "gamble_count")


def upgrade() -> None:
    for col in COLUMNS:
        op.add_column("farm_stats", sa.Column(col, sa.BigInteger(), nullable=False, server_default="0"))
    op.execute("""
        UPDATE farm_stats s
        SET steal_count = c.steal_count, defended_count = c.defended_count, gamble_count = c.gamble_count
        FROM (
            SELECT farm_id,
                   count(*) FILTER (WHERE event_type = 'steal') AS steal_count,
                   count(*) FILTER (WHERE event_type = 'defended') AS defended_count,
                   count(*) FILTER (WHERE event_type = 'gamble') AS gamble_count
            FROM game_transactions
            WHERE event_type IN ('steal', 'defended', 'gamble')
            GROUP BY farm_id
        ) AS c
        WHERE c.farm_id = s.farm_id
    """)


def downgrade() -> None:
    for col in reversed(COLUMNS):
        op.drop_column("farm_stats", col)
