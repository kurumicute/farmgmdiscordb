"""fair: weekly showcase entries and popular votes

Revision ID: 0048
Revises: 0047
Create Date: 2026-11-20

Go 版新增（Python 版沒有對應的程式），品評會（設計見 Go 專案「新玩法六項_流程圖.md」#5）。
- 新表 fair_entries：參賽作品，每個群組每週每位玩家一件（week_start 是台灣時間的週一）。
  category：crop／goods／lab；status：active（進行中）／settled（已結算，有名次與獎金）／refunded（報名不足流會、已退費）。
  item_name 是報名時的名稱快照；base_value 是作品價值，judge_score 是加上本週評審偏好後的分數。
- 新表 fair_votes：人氣票，每人每件作品一票（每人每週的票數上限由程式檢查）。
- 交易類型新增 fair_entry（報名：扣報名費與作品）、fair_reward（名次獎金，或流會時退回報名費與作品）。

降版會刪除 fair_votes、fair_entries（進行中的報名費與作品不會自動退回，請先結算或手動處理），
並把這兩種交易改記成 admin_adjust（保留金額與說明，金幣與排行統計不變）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)

# 0046 之後的交易類型（0047 沒有改動交易類型）
EVENTS_0047 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal", "boss_donate", "boss_reward",
    "rename_farm", "checkin_reward", "achievement_reward",
    "ruins_donate", "ruins_guardian", "rumor_treasure",
    "seed_lab", "seed_share",
    "aid_gift", "aid_water",
    "reputation_donate", "auction_bid", "auction_refund", "auction_win",
)
NEW_EVENTS = ("fair_entry", "fair_reward")


def _set_events(events: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions",
        "event_type IN (" + ", ".join(f"'{v}'" for v in events) + ")",
    )


def upgrade() -> None:
    _set_events(EVENTS_0047 + NEW_EVENTS)

    op.create_table(
        "fair_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), sa.ForeignKey("players.id", ondelete="CASCADE"), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(10), nullable=False),
        sa.Column("item_code", sa.String(50), nullable=False),
        sa.Column("item_name", sa.String(64), nullable=False),
        sa.Column("quality", sa.String(20), nullable=False, server_default=sa.text("'normal'")),
        sa.Column("mutation", sa.String(20), nullable=False, server_default=sa.text("'none'")),
        sa.Column("base_value", sa.Integer(), nullable=False),
        sa.Column("judge_score", sa.Integer(), nullable=False),
        sa.Column("votes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("fee", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("reward", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(10), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("settled_at", TS, nullable=True),
        sa.UniqueConstraint("group_id", "week_start", "player_id", name=op.f("uq_fair_entries_group_week_player")),
        sa.CheckConstraint("category IN ('crop', 'goods', 'lab')", name=op.f("ck_fair_entries_category_valid")),
        sa.CheckConstraint("status IN ('active', 'settled', 'refunded')", name=op.f("ck_fair_entries_status_valid")),
        sa.CheckConstraint("base_value >= 0 AND judge_score >= 0 AND votes >= 0 AND fee >= 0 AND reward >= 0",
                           name=op.f("ck_fair_entries_values_non_negative")),
        sa.CheckConstraint("rank IS NULL OR rank >= 1", name=op.f("ck_fair_entries_rank_positive")),
    )
    op.create_index("ix_fair_entries_group_week", "fair_entries", ["group_id", "week_start"])
    op.create_index("ix_fair_entries_status_week", "fair_entries", ["status", "week_start"])

    op.create_table(
        "fair_votes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.BigInteger(), sa.ForeignKey("fair_entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("voter_player_id", sa.BigInteger(), sa.ForeignKey("players.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("entry_id", "voter_player_id", name=op.f("uq_fair_votes_entry_voter")),
    )
    op.create_index("ix_fair_votes_voter_week", "fair_votes", ["voter_player_id", "group_id", "week_start"])


def downgrade() -> None:
    op.drop_table("fair_votes")
    op.drop_table("fair_entries")
    op.execute("UPDATE game_transactions SET event_type = 'admin_adjust' WHERE event_type IN ("
               + ", ".join(f"'{v}'" for v in NEW_EVENTS) + ")")
    _set_events(EVENTS_0047)
