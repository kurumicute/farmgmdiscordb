"""farm group transfer with cooldown, cumulative and daily farm stats

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27

- farms.last_group_transfer_at：最近一次轉移所屬群組的時間（NULL = 從未轉移）。
- 觸發器 trg_farms_home_group_immutable 改為「冷卻保護」而非完全禁止：
  - home_group_id 不可清空；舊資料（NULL）仍可一次性設定。
  - 變更 home_group_id 時必須同時把 last_group_transfer_at 設為新的時間，
    且新時間 ≥ 上次轉移 + 7 天、不晚於資料庫目前時間 + 1 分鐘（不能預先填未來時間繞過冷卻）。
  - last_group_transfer_at 不可單獨修改（不能清空或倒退來重設冷卻）。
  - 只有在同一筆交易中 SET LOCAL farmgm.admin_group_transfer = 'on'（管理員略過冷卻，服務層會寫入 admin_actions）
    時才略過冷卻檢查。
- 新事件類型 transfer_group。
- farm_stats：每座農場累積販售統計與非經營金幣；farm_daily_stats：每個台灣日期的販售與淨賺金幣。
  兩者都由既有 game_transactions 回填，之後在同一筆交易中隨販售等操作更新。
- 排行用索引：farm_stats 依作物販售／商品收入、farms 依金幣。

不刪除、不修改任何既有遊戲資料。降版會刪除兩張統計表、轉移時間欄位與 transfer_group 交易紀錄，並恢復 0003 的完全禁止觸發器。
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

EVENTS_0009 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward",
)
EVENTS_0010 = EVENTS_0009 + ("transfer_group",)
# 計算淨賺金幣時排除的事件（與 0010 當時的 app.game.ranking.NET_GOLD_EXCLUDED 相同，Migration 內固定）
NON_OPERATING = ("create_farm", "admin_adjust", "quest_reward", "competition_reward", "ranking_reward")


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


TRIGGER_FUNCTION_0010 = """
CREATE OR REPLACE FUNCTION farms_home_group_immutable() RETURNS trigger AS $$
DECLARE
    admin_override boolean := coalesce(current_setting('farmgm.admin_group_transfer', true), '') = 'on';
BEGIN
    IF NEW.home_group_id IS DISTINCT FROM OLD.home_group_id THEN
        IF OLD.home_group_id IS NULL THEN
            -- 舊資料的一次性設定
            RETURN NEW;
        END IF;
        IF NEW.home_group_id IS NULL THEN
            RAISE EXCEPTION 'farm % home_group_id cannot be cleared', OLD.id USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.last_group_transfer_at IS NULL
           OR (NEW.last_group_transfer_at IS NOT DISTINCT FROM OLD.last_group_transfer_at AND NOT admin_override) THEN
            RAISE EXCEPTION 'farm % group transfer must record last_group_transfer_at', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
        IF NOT admin_override THEN
            IF NEW.last_group_transfer_at > now() + interval '1 minute' THEN
                RAISE EXCEPTION 'farm % group transfer time is in the future', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.last_group_transfer_at IS NOT NULL
               AND NEW.last_group_transfer_at < OLD.last_group_transfer_at + interval '7 days' THEN
                RAISE EXCEPTION 'farm % group transfer is in cooldown', OLD.id USING ERRCODE = 'check_violation';
            END IF;
        END IF;
    ELSIF NEW.last_group_transfer_at IS DISTINCT FROM OLD.last_group_transfer_at AND NOT admin_override THEN
        RAISE EXCEPTION 'farm % last_group_transfer_at cannot be changed without a transfer', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

TRIGGER_FUNCTION_0003 = """
CREATE OR REPLACE FUNCTION farms_home_group_immutable() RETURNS trigger AS $$
BEGIN
    IF OLD.home_group_id IS NOT NULL AND NEW.home_group_id IS DISTINCT FROM OLD.home_group_id THEN
        RAISE EXCEPTION 'farm % home_group_id is fixed and cannot be changed', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

CROP_CODES = "(SELECT code FROM crop_types UNION SELECT variant_code FROM crop_types)"

BACKFILL_STATS = f"""
INSERT INTO farm_stats (farm_id, player_id, crop_sold_quantity, crop_sales_income,
                        goods_sold_quantity, goods_sales_income, non_operating_gold)
SELECT f.id, f.player_id,
       coalesce(sum(t.quantity) FILTER (WHERE t.event_type = 'sell' AND t.item_code IN {CROP_CODES}), 0),
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type = 'sell' AND t.item_code IN {CROP_CODES}), 0),
       coalesce(sum(t.quantity) FILTER (WHERE t.event_type = 'sell'), 0),
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type = 'sell'), 0),
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type IN {_in(NON_OPERATING)}), 0)
FROM farms f
LEFT JOIN game_transactions t ON t.farm_id = f.id
GROUP BY f.id, f.player_id
"""

# 台灣固定 UTC+8（與 app.clock.TAIPEI_TZ 相同）
BACKFILL_DAILY = f"""
INSERT INTO farm_daily_stats (farm_id, day, crop_sold_quantity, crop_sales_income,
                              goods_sold_quantity, goods_sales_income, net_gold)
SELECT t.farm_id, ((t.created_at AT TIME ZONE 'UTC') + interval '8 hours')::date,
       coalesce(sum(t.quantity) FILTER (WHERE t.event_type = 'sell' AND t.item_code IN {CROP_CODES}), 0),
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type = 'sell' AND t.item_code IN {CROP_CODES}), 0),
       coalesce(sum(t.quantity) FILTER (WHERE t.event_type = 'sell'), 0),
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type = 'sell'), 0),
       coalesce(sum(t.gold_delta) FILTER (WHERE t.event_type NOT IN {_in(NON_OPERATING)}), 0)
FROM game_transactions t
WHERE t.event_type = 'sell' OR (t.gold_delta <> 0 AND t.event_type NOT IN {_in(NON_OPERATING)})
GROUP BY 1, 2
"""


def upgrade() -> None:
    op.add_column("farms", sa.Column("last_group_transfer_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(TRIGGER_FUNCTION_0010)
    op.execute("DROP TRIGGER IF EXISTS trg_farms_home_group_immutable ON farms")
    op.execute(
        "CREATE TRIGGER trg_farms_home_group_immutable "
        "BEFORE UPDATE OF home_group_id, last_group_transfer_at ON farms "
        "FOR EACH ROW EXECUTE FUNCTION farms_home_group_immutable()"
    )
    op.create_index("ix_farms_gold_rank", "farms", [sa.text("gold DESC"), "player_id"])

    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0010)
    )

    op.create_table(
        "farm_stats",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.BigInteger(), nullable=False),
        sa.Column("crop_sold_quantity", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("crop_sales_income", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("goods_sold_quantity", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("goods_sales_income", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("non_operating_gold", sa.BigInteger(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_stats_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("farm_id", name=op.f("pk_farm_stats")),
        sa.UniqueConstraint("player_id", name=op.f("uq_farm_stats_player_id")),
    )
    op.create_index(
        "ix_farm_stats_crop_rank",
        "farm_stats",
        [sa.text("crop_sold_quantity DESC"), sa.text("crop_sales_income DESC"), "player_id"],
    )
    op.create_index(
        "ix_farm_stats_goods_rank",
        "farm_stats",
        [sa.text("goods_sales_income DESC"), sa.text("goods_sold_quantity DESC"), "player_id"],
    )
    op.create_table(
        "farm_daily_stats",
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("crop_sold_quantity", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("crop_sales_income", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("goods_sold_quantity", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("goods_sales_income", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("net_gold", sa.BigInteger(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["farm_id"], ["farms.id"], name=op.f("fk_farm_daily_stats_farm_id_farms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("farm_id", "day", name=op.f("pk_farm_daily_stats")),
    )
    op.create_index("ix_farm_daily_stats_day", "farm_daily_stats", ["day"])
    op.execute(BACKFILL_STATS)
    op.execute(BACKFILL_DAILY)


def downgrade() -> None:
    op.drop_index("ix_farm_daily_stats_day", table_name="farm_daily_stats")
    op.drop_table("farm_daily_stats")
    op.drop_index("ix_farm_stats_goods_rank", table_name="farm_stats")
    op.drop_index("ix_farm_stats_crop_rank", table_name="farm_stats")
    op.drop_table("farm_stats")

    op.execute("DELETE FROM game_transactions WHERE event_type = 'transfer_group'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0009)
    )
    op.drop_index("ix_farms_gold_rank", table_name="farms")
    op.execute("DROP TRIGGER IF EXISTS trg_farms_home_group_immutable ON farms")
    op.execute(TRIGGER_FUNCTION_0003)
    op.execute(
        "CREATE TRIGGER trg_farms_home_group_immutable BEFORE UPDATE OF home_group_id ON farms "
        "FOR EACH ROW EXECUTE FUNCTION farms_home_group_immutable()"
    )
    op.drop_column("farms", "last_group_transfer_at")
