"""admin-managed crops and animals, timed game events

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-27

- crop_types：variant_code、variant_name（原本寫死在程式中的珍稀變種）與 active（停用旗標）。
- animal_types：動物種類與產品（原本寫死在程式中的雞／牛／羊與雞蛋／牛奶／羊毛），
  farm_animals.animal_code 加上外鍵。
- game_events：限時全服加成事件（單次、每日、每週）。
- game_transactions：applied_events（該次操作套用的事件）；新事件類型 admin_adjust（管理員調整）。
- steal_records.plot_id：改為可為空、土地刪除時設為 NULL，保留歷史紀錄。
- 只新增欄位與資料表、回填既有值，不修改既有遊戲資料。

降版注意：降版會刪除 plot_id 為 NULL 的偷菜紀錄與 admin_adjust 交易紀錄；
若已新增程式未內建的作物或動物，降版後舊版程式無法辨識，請先確認再降版。
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

EVENTS_0007 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward",
)
EVENTS_0008 = EVENTS_0007 + ("admin_adjust",)

# Migration 內固定寫入既有值，不引用會變動的應用程式常數
VARIANTS = {
    "wheat": ("amber_wheat", "琥珀小麥"),
    "carrot": ("amethyst_carrot", "紫晶胡蘿蔔"),
    "tomato": ("black_tomato", "黑鑽番茄"),
    "strawberry": ("snow_strawberry", "白雪草莓"),
}
ANIMALS = [
    # code, 名稱, 價格, 餵食費用, 週期秒數, 產品代碼, 產品名稱, 每次產量, 產品售價
    ("chicken", "雞", 500, 20, 24 * 3600, "egg", "雞蛋", 2, 25),
    ("cow", "牛", 2000, 50, 24 * 3600, "milk", "牛奶", 2, 60),
    ("sheep", "羊", 1500, 35, 48 * 3600, "wool", "羊毛", 1, 120),
]


def _event_check(types: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{e}'" for e in types) + ")"


def upgrade() -> None:
    # ---- crop_types：珍稀變種與停用 ----
    op.add_column("crop_types", sa.Column("variant_code", sa.String(32), nullable=True))
    op.add_column("crop_types", sa.Column("variant_name", sa.String(32), nullable=True))
    op.add_column("crop_types", sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False))
    crops = sa.table("crop_types", sa.column("code"), sa.column("variant_code"), sa.column("variant_name"))
    for code, (variant_code, variant_name) in VARIANTS.items():
        op.execute(
            crops.update()
            .where(crops.c.code == code)
            .values(variant_code=variant_code, variant_name=variant_name)
        )
    # 程式未內建變種的作物（理論上不存在）以「<代碼>_variant」補上，確保欄位可設為必填
    op.execute(
        "UPDATE crop_types SET variant_code = left(code, 24) || '_variant', variant_name = left(name, 28) || '變種' "
        "WHERE variant_code IS NULL"
    )
    op.alter_column("crop_types", "variant_code", nullable=False)
    op.alter_column("crop_types", "variant_name", nullable=False)
    op.create_unique_constraint(op.f("uq_crop_types_variant_code"), "crop_types", ["variant_code"])

    # ---- animal_types ----
    animal_types = op.create_table(
        "animal_types",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("feed_cost", sa.Integer(), nullable=False),
        sa.Column("cycle_seconds", sa.Integer(), nullable=False),
        sa.Column("product_code", sa.String(32), nullable=False),
        sa.Column("product_name", sa.String(32), nullable=False),
        sa.Column("product_quantity", sa.Integer(), nullable=False),
        sa.Column("product_price", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("price > 0", name=op.f("ck_animal_types_price_positive")),
        sa.CheckConstraint("feed_cost >= 0", name=op.f("ck_animal_types_feed_cost_non_negative")),
        sa.CheckConstraint("cycle_seconds > 0", name=op.f("ck_animal_types_cycle_seconds_positive")),
        sa.CheckConstraint("product_quantity > 0", name=op.f("ck_animal_types_product_quantity_positive")),
        sa.CheckConstraint("product_price >= 0", name=op.f("ck_animal_types_product_price_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_animal_types")),
        sa.UniqueConstraint("code", name=op.f("uq_animal_types_code")),
        sa.UniqueConstraint("product_code", name=op.f("uq_animal_types_product_code")),
    )
    op.bulk_insert(
        animal_types,
        [
            dict(zip(
                ("code", "name", "price", "feed_cost", "cycle_seconds", "product_code", "product_name",
                 "product_quantity", "product_price"),
                row,
                strict=True,
            ))
            for row in ANIMALS
        ],
    )
    op.create_foreign_key(
        op.f("fk_farm_animals_animal_code_animal_types"),
        "farm_animals",
        "animal_types",
        ["animal_code"],
        ["code"],
        ondelete="RESTRICT",
    )

    # ---- game_events ----
    op.create_table(
        "game_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("description", sa.String(300), server_default="", nullable=False),
        sa.Column("crop_type_id", sa.Integer(), nullable=True),
        sa.Column("sell_price_pct", sa.Integer(), server_default="100", nullable=False),
        sa.Column("grow_reduction_pct", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quality_bonus_bp", sa.Integer(), server_default="0", nullable=False),
        sa.Column("mutation_bonus_bp", sa.Integer(), server_default="0", nullable=False),
        sa.Column("harvest_bonus_pct", sa.Integer(), server_default="0", nullable=False),
        sa.Column("schedule_type", sa.String(8), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_minute", sa.Integer(), nullable=True),
        sa.Column("end_minute", sa.Integer(), nullable=True),
        sa.Column("weekdays", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schedule_type IN ('once', 'daily', 'weekly')", name=op.f("ck_game_events_schedule_type_valid")
        ),
        sa.CheckConstraint(
            "schedule_type <> 'once' OR (starts_at IS NOT NULL AND ends_at IS NOT NULL)",
            name=op.f("ck_game_events_once_has_period"),
        ),
        sa.CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at", name=op.f("ck_game_events_ends_after_start")
        ),
        sa.CheckConstraint(
            "schedule_type = 'once' OR (start_minute IS NOT NULL AND end_minute IS NOT NULL)",
            name=op.f("ck_game_events_window_required"),
        ),
        sa.CheckConstraint(
            "start_minute IS NULL OR (start_minute >= 0 AND end_minute > start_minute AND end_minute <= 1440)",
            name=op.f("ck_game_events_window_range"),
        ),
        sa.CheckConstraint(
            "schedule_type <> 'weekly' OR (weekdays BETWEEN 1 AND 127)", name=op.f("ck_game_events_weekdays_valid")
        ),
        sa.CheckConstraint("sell_price_pct BETWEEN 100 AND 300", name=op.f("ck_game_events_sell_price_pct_range")),
        sa.CheckConstraint(
            "grow_reduction_pct BETWEEN 0 AND 90", name=op.f("ck_game_events_grow_reduction_pct_range")
        ),
        sa.CheckConstraint("quality_bonus_bp BETWEEN 0 AND 3000", name=op.f("ck_game_events_quality_bonus_bp_range")),
        sa.CheckConstraint(
            "mutation_bonus_bp BETWEEN 0 AND 3000", name=op.f("ck_game_events_mutation_bonus_bp_range")
        ),
        sa.CheckConstraint("harvest_bonus_pct BETWEEN 0 AND 200", name=op.f("ck_game_events_harvest_bonus_pct_range")),
        sa.ForeignKeyConstraint(
            ["crop_type_id"],
            ["crop_types.id"],
            name=op.f("fk_game_events_crop_type_id_crop_types"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_game_events")),
    )
    op.create_index(op.f("ix_game_events_crop_type_id"), "game_events", ["crop_type_id"])

    # ---- game_transactions ----
    op.add_column("game_transactions", sa.Column("applied_events", sa.JSON(), nullable=True))
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0008)
    )

    # ---- steal_records：土地刪除時保留紀錄 ----
    op.drop_constraint(op.f("fk_steal_records_plot_id_farm_plots"), "steal_records", type_="foreignkey")
    op.alter_column("steal_records", "plot_id", nullable=True)
    op.create_foreign_key(
        op.f("fk_steal_records_plot_id_farm_plots"),
        "steal_records",
        "farm_plots",
        ["plot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.execute("DELETE FROM steal_records WHERE plot_id IS NULL")
    op.drop_constraint(op.f("fk_steal_records_plot_id_farm_plots"), "steal_records", type_="foreignkey")
    op.alter_column("steal_records", "plot_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_steal_records_plot_id_farm_plots"),
        "steal_records",
        "farm_plots",
        ["plot_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.execute("DELETE FROM game_transactions WHERE event_type = 'admin_adjust'")
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _event_check(EVENTS_0007)
    )
    op.drop_column("game_transactions", "applied_events")

    op.drop_index(op.f("ix_game_events_crop_type_id"), table_name="game_events")
    op.drop_table("game_events")

    op.drop_constraint(op.f("fk_farm_animals_animal_code_animal_types"), "farm_animals", type_="foreignkey")
    op.drop_table("animal_types")

    op.drop_constraint(op.f("uq_crop_types_variant_code"), "crop_types", type_="unique")
    op.drop_column("crop_types", "active")
    op.drop_column("crop_types", "variant_name")
    op.drop_column("crop_types", "variant_code")
