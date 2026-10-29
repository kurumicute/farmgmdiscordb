"""crop quality and mutation; crop grow time 2 hours

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-27

- farm_plots：quality、mutation（播種時決定，之後固定）。既有種植中的作物補為 normal／none。
- inventory_items：quality、mutation（既有庫存為 normal／none）；唯一限制改為 (farm, item, quality, mutation)。
- game_transactions：quality、mutation、unit_price（實際成交單價）。
- crop_genetics_settings：品質與突變設定的版本紀錄，寫入預設設定為第 1 版。
- crop_types：依需求把所有作物生長時間改為 2 小時（已在生長中的作物成熟時間不變）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

QUALITY_SQL = "'normal', 'good', 'rare', 'legendary'"
MUTATION_SQL = "'none', 'giant', 'golden', 'fast', 'variant'"
LEGACY_GROW_SECONDS = {"wheat": 60, "carrot": 180, "tomato": 300, "strawberry": 600}
NEW_GROW_SECONDS = 2 * 3600

# Migration 內固定寫入預設值，不引用會變動的應用程式常數
DEFAULT_SETTINGS = {
    "quality_bp": {"normal": 7000, "good": 2000, "rare": 800, "legendary": 200},
    "quality_price_pct": {"normal": 100, "good": 120, "rare": 150, "legendary": 200},
    "mutation_bp": {"giant": 300, "golden": 100, "fast": 300, "variant": 100},
    "giant_multiplier": 2,
    "golden_price_pct": 300,
    "fast_reduction_seconds": 7200,
    "variant_price_pct": 400,
    "level_bonus_bp": 50,
    "level_bonus_max_bp": 500,
}


def upgrade() -> None:
    # ---- farm_plots ----
    op.add_column("farm_plots", sa.Column("quality", sa.String(16), nullable=True))
    op.add_column("farm_plots", sa.Column("mutation", sa.String(16), nullable=True))
    op.execute("UPDATE farm_plots SET quality = 'normal', mutation = 'none' WHERE crop_type_id IS NOT NULL")
    op.create_check_constraint(
        op.f("ck_farm_plots_genetics_consistency"),
        "farm_plots",
        "(crop_type_id IS NULL) = (quality IS NULL) AND (crop_type_id IS NULL) = (mutation IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_farm_plots_quality_valid"), "farm_plots", f"quality IS NULL OR quality IN ({QUALITY_SQL})"
    )
    op.create_check_constraint(
        op.f("ck_farm_plots_mutation_valid"), "farm_plots", f"mutation IS NULL OR mutation IN ({MUTATION_SQL})"
    )

    # ---- inventory_items ----
    op.add_column("inventory_items", sa.Column("quality", sa.String(16), server_default="normal", nullable=False))
    op.add_column("inventory_items", sa.Column("mutation", sa.String(16), server_default="none", nullable=False))
    op.drop_constraint("uq_inventory_items_farm_item", "inventory_items", type_="unique")
    op.create_unique_constraint(
        "uq_inventory_items_farm_item", "inventory_items", ["farm_id", "item_code", "quality", "mutation"]
    )
    op.create_check_constraint(op.f("ck_inventory_items_quality_valid"), "inventory_items", f"quality IN ({QUALITY_SQL})")
    op.create_check_constraint(
        op.f("ck_inventory_items_mutation_valid"), "inventory_items", f"mutation IN ({MUTATION_SQL})"
    )

    # ---- game_transactions ----
    op.add_column("game_transactions", sa.Column("quality", sa.String(16), nullable=True))
    op.add_column("game_transactions", sa.Column("mutation", sa.String(16), nullable=True))
    op.add_column("game_transactions", sa.Column("unit_price", sa.BigInteger(), nullable=True))

    # ---- 設定 ----
    settings = op.create_table(
        "crop_genetics_settings",
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_crop_genetics_settings_version_positive")),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_crop_genetics_settings")),
    )
    op.execute(
        settings.insert().values(
            version=1, settings=DEFAULT_SETTINGS, created_by="migration", created_at=sa.func.now()
        )
    )

    # ---- 作物生長時間改為 2 小時 ----
    op.execute(f"UPDATE crop_types SET grow_seconds = {NEW_GROW_SECONDS}")


def downgrade() -> None:
    # 注意：降版會把不同品質／突變的庫存合併為一般庫存（數量保留、品質資訊捨棄），並還原舊的生長時間
    for code, seconds in LEGACY_GROW_SECONDS.items():
        op.execute(f"UPDATE crop_types SET grow_seconds = {seconds} WHERE code = '{code}'")
    op.drop_table("crop_genetics_settings")

    op.drop_column("game_transactions", "unit_price")
    op.drop_column("game_transactions", "mutation")
    op.drop_column("game_transactions", "quality")

    op.execute(
        "CREATE TEMP TABLE inv_merge AS SELECT farm_id, item_code, sum(quantity) AS quantity, "
        "max(updated_at) AS updated_at FROM inventory_items GROUP BY farm_id, item_code"
    )
    op.execute("DELETE FROM inventory_items")
    op.execute(
        "INSERT INTO inventory_items (farm_id, item_code, quantity, updated_at) "
        "SELECT farm_id, item_code, quantity, updated_at FROM inv_merge"
    )
    op.execute("DROP TABLE inv_merge")
    op.drop_constraint(op.f("ck_inventory_items_mutation_valid"), "inventory_items", type_="check")
    op.drop_constraint(op.f("ck_inventory_items_quality_valid"), "inventory_items", type_="check")
    op.drop_constraint("uq_inventory_items_farm_item", "inventory_items", type_="unique")
    op.drop_column("inventory_items", "mutation")
    op.drop_column("inventory_items", "quality")
    op.create_unique_constraint("uq_inventory_items_farm_item", "inventory_items", ["farm_id", "item_code"])

    op.drop_constraint(op.f("ck_farm_plots_mutation_valid"), "farm_plots", type_="check")
    op.drop_constraint(op.f("ck_farm_plots_quality_valid"), "farm_plots", type_="check")
    op.drop_constraint(op.f("ck_farm_plots_genetics_consistency"), "farm_plots", type_="check")
    op.drop_column("farm_plots", "mutation")
    op.drop_column("farm_plots", "quality")

