"""gifts and watering help between group members

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-17

Go 版新增（Python 版沒有對應的程式）。
- 交易類型新增 aid_gift（送作物給同群組的玩家，雙方各一筆）、aid_water（幫同群組的玩家澆水，雙方各一筆）。
- 新表 aid_logs：每次送禮／幫忙澆水一列，用來計算每日上限（aid_day 是台灣日期）。
  kind：gift／water；gift 記錄送出的物品與數量，water 記錄澆了幾格。

降版會刪除 aid_logs，並把這兩種交易改記成 admin_adjust（保留金額與說明，金幣與排行統計不變）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

EVENTS_0043 = (
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
)
NEW_EVENTS = ("aid_gift", "aid_water")


def _set_events(events: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions",
        "event_type IN (" + ", ".join(f"'{v}'" for v in events) + ")",
    )


def upgrade() -> None:
    _set_events(EVENTS_0043 + NEW_EVENTS)
    op.create_table(
        "aid_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("sender_farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("receiver_farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("item_code", sa.String(length=50), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("aid_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('gift', 'water')", name=op.f("ck_aid_logs_kind_valid")),
        sa.CheckConstraint("sender_farm_id <> receiver_farm_id", name=op.f("ck_aid_logs_not_self")),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_aid_logs_quantity_non_negative")),
    )
    op.create_index("ix_aid_logs_sender_day", "aid_logs", ["sender_farm_id", "aid_day"])
    op.create_index("ix_aid_logs_receiver_day", "aid_logs", ["receiver_farm_id", "aid_day"])


def downgrade() -> None:
    op.drop_table("aid_logs")
    op.execute("UPDATE game_transactions SET event_type = 'admin_adjust' WHERE event_type IN ('aid_gift', 'aid_water')")
    _set_events(EVENTS_0043)
