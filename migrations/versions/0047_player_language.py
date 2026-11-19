"""player interface language

Revision ID: 0047
Revises: 0046
Create Date: 2026-11-19

Go 版新增（Python 版沒有對應的程式）。
- players 新增 language：玩家自己選的介面語言（zh-TW／en），NULL = 跟著 Discord 用戶端的語言。
  只新增一個可以是 NULL 的欄位，不會改動既有資料。

降版會刪除 language 欄位（玩家選過的語言設定消失，之後跟著 Discord 用戶端的語言）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("language", sa.String(10), nullable=True))
    op.create_check_constraint(op.f("ck_players_language_valid"), "players",
                               "language IS NULL OR language IN ('zh-TW', 'en')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_players_language_valid"), "players", type_="check")
    op.drop_column("players", "language")
