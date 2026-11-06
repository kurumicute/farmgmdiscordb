"""daily check-in

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-06

- daily_checkins：每日簽到紀錄（台灣日期）；(farm_id, day) 唯一，每座農場每天只能簽到一次。
- game_transactions：新增事件類型 checkin_reward（簽到獎勵）。

降版時把 checkin_reward 的交易紀錄改成 admin_adjust（同樣不計入淨賺金幣），不刪除：
保留「全部 gold_delta 加總 = 全服金幣」的不變式與排行統計；已發放的獎勵保留在玩家身上。
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

EVENTS_0023 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal", "boss_donate", "boss_reward", "rename_farm",
)
NEW_EVENTS = ("checkin_reward",)
TS = sa.DateTime(timezone=True)


def _event_check(events: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.create_table(
        "daily_checkins",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("streak", sa.Integer(), nullable=False),
        sa.Column("gold", sa.BigInteger(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], ondelete="CASCADE",
                                name=op.f("fk_daily_checkins_farm_id_farms")),
        sa.UniqueConstraint("farm_id", "day", name="uq_daily_checkins_farm_day"),
        sa.CheckConstraint("streak >= 1", name=op.f("ck_daily_checkins_streak_positive")),
        sa.CheckConstraint("gold >= 0", name=op.f("ck_daily_checkins_gold_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_daily_checkins")),
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0023 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute("UPDATE game_transactions SET event_type = 'admin_adjust' WHERE event_type = 'checkin_reward'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0023)
    )
    op.drop_table("daily_checkins")
