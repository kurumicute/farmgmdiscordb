"""farm home group and transaction context group

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27

- farms.home_group_id：農場主要所屬群組。既有農場保持 NULL（不猜測歸屬），由玩家以指令一次性設定。
- 觸發器：home_group_id 一旦設定就不可更改或清空（未來的管理員移轉需另行設計並調整此保護）。
- game_transactions.context_group_id：操作發生時所在的群組，只做紀錄，不影響排行。
- 不刪除、不合併任何既有資料（farms.player_id 本來就唯一，每位玩家最多一座農場）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

EVENTS_0002 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen",
)
EVENTS_0003 = EVENTS_0002 + ("bind_group",)


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


TRIGGER_FUNCTION = """
CREATE FUNCTION farms_home_group_immutable() RETURNS trigger AS $$
BEGIN
    IF OLD.home_group_id IS NOT NULL AND NEW.home_group_id IS DISTINCT FROM OLD.home_group_id THEN
        RAISE EXCEPTION 'farm % home_group_id is fixed and cannot be changed', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column("farms", sa.Column("home_group_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_farms_home_group_id_groups"), "farms", "groups", ["home_group_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(op.f("ix_farms_home_group_id"), "farms", ["home_group_id"])
    op.execute(TRIGGER_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_farms_home_group_immutable BEFORE UPDATE OF home_group_id ON farms "
        "FOR EACH ROW EXECUTE FUNCTION farms_home_group_immutable()"
    )

    op.add_column("game_transactions", sa.Column("context_group_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_game_transactions_context_group_id_groups"),
        "game_transactions",
        "groups",
        ["context_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_game_transactions_context_group_id"), "game_transactions", ["context_group_id"])

    # 新事件類型：舊農場一次性設定所屬群組
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0003)
    )


def downgrade() -> None:
    # 注意：降版會捨棄農場歸屬、操作的群組上下文與 bind_group 紀錄（其餘遊戲資料不受影響）
    op.execute("DELETE FROM game_transactions WHERE event_type = 'bind_group'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0002)
    )
    op.drop_index(op.f("ix_game_transactions_context_group_id"), table_name="game_transactions")
    op.drop_constraint(op.f("fk_game_transactions_context_group_id_groups"), "game_transactions", type_="foreignkey")
    op.drop_column("game_transactions", "context_group_id")
    op.execute("DROP TRIGGER IF EXISTS trg_farms_home_group_immutable ON farms")
    op.execute("DROP FUNCTION IF EXISTS farms_home_group_immutable()")
    op.drop_index(op.f("ix_farms_home_group_id"), table_name="farms")
    op.drop_constraint(op.f("fk_farms_home_group_id_groups"), "farms", type_="foreignkey")
    op.drop_column("farms", "home_group_id")
