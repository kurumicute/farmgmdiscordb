"""rename farm event type

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-04

- game_transactions：新增事件類型 rename_farm（玩家花金幣更改農場名稱）。

降版時把 rename_farm 的交易紀錄改成 buy_item（同樣是一般支出），不刪除：
保留「全部 gold_delta 加總 = 全服金幣」的不變式與排行統計；農場名稱維持改名後的值。
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

EVENTS_0022 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal", "boss_donate", "boss_reward",
)
NEW_EVENTS = ("rename_farm",)


def _event_check(events: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0022 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute("UPDATE game_transactions SET event_type = 'buy_item' WHERE event_type = 'rename_farm'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0022)
    )
