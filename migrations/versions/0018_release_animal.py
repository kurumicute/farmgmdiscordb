"""release animal event type

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-02

- game_transactions：新增事件類型 release_animal（放生動物；不涉及金幣，紀錄被放生的動物代碼與數量）。

降版時刪除 release_animal 的交易紀錄（放生的動物本身已不存在，不會復原）再還原 CHECK 約束。
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

EVENTS_0017 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell",
)
NEW_EVENTS = ("release_animal",)


def _event_check(events: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in events) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0017 + NEW_EVENTS)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM game_transactions WHERE event_type = ANY(:types)").bindparams(types=list(NEW_EVENTS))
    )
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0017)
    )
