"""achievements and titles

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-06

- farm_achievements：已領取的成就（每座農場每個成就一次），保存發放內容。成就進度由既有資料即時計算，不另外存。
- farm_titles：玩家擁有的稱號（成就、世界 Boss 等來源）；從 world_boss_reward_claims 匯入既有的世界 Boss 稱號。
- farms.title：目前選用的稱號（NULL = 不顯示），顯示在總覽、排行榜與世界 Boss 排行。
- game_transactions：新增事件類型 achievement_reward（成就獎勵）。

降版時把 achievement_reward 的交易紀錄改成 admin_adjust（同樣不計入淨賺金幣）以保留金幣不變式，
並刪除成就領取紀錄、稱號與選用的稱號（世界 Boss 稱號仍保存在 world_boss_reward_claims）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

EVENTS_0025 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal", "boss_donate", "boss_reward", "rename_farm",
    "checkin_reward",
)
NEW_EVENTS = ("achievement_reward",)
TS = sa.DateTime(timezone=True)


def _event_check(events: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.create_table(
        "farm_achievements",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("gold", sa.BigInteger(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("claimed_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE",
                                name=op.f("fk_farm_achievements_farm_id_farms")),
        sa.CheckConstraint("gold >= 0", name=op.f("ck_farm_achievements_gold_non_negative")),
        sa.PrimaryKeyConstraint("farm_id", "code", name=op.f("pk_farm_achievements")),
    )
    op.create_table(
        "farm_titles",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(20), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("obtained_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE",
                                name=op.f("fk_farm_titles_farm_id_farms")),
        sa.PrimaryKeyConstraint("farm_id", "title", name=op.f("pk_farm_titles")),
    )
    op.add_column("farms", sa.Column("title", sa.String(20)))
    op.execute("""
        INSERT INTO farm_titles (farm_id, title, source, obtained_at)
        SELECT f.id, c.title, 'world_boss', min(c.claimed_at)
        FROM world_boss_reward_claims c JOIN farms f ON f.player_id = c.player_id
        WHERE c.title IS NOT NULL AND btrim(c.title) <> ''
        GROUP BY f.id, c.title
    """)
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0025 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute("UPDATE game_transactions SET event_type = 'admin_adjust' WHERE event_type = 'achievement_reward'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0025)
    )
    op.drop_column("farms", "title")
    op.drop_table("farm_titles")
    op.drop_table("farm_achievements")
