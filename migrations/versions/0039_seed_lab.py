"""seed lab: special seeds with genes, crossing, mutation research, seed sharing and 12 hybrid crops

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-13

Go 版新增（Python 版沒有對應的程式；套用後不能再切回 Python 版 Bot）。
- seed_labs：每座農場一列，種子實驗室的研究等級與經驗（農場 Lv.5 建造）。
- lab_seeds：特殊種子（每顆有 4 個基因等級 1–5、世代、突變）；status：stored 種子庫、busy 培育中、planted 已種下、used 已消耗。
- lab_jobs：研究槽裡的雜交／突變研究（collected_at 為 NULL 表示還沒領取；同一農場同一槽只能有一個未領取的工作）。
- lab_records：培育紀錄（鑑定、雜交、突變研究的結果）。
- lab_seed_shares：分享種子紀錄（每人每天最多送出 5 顆、每人每天只能收 1 位玩家的分享，依台灣日期 share_day 計算）。
- farm_plots.lab_seed_id：種下的特殊種子（收成、清除時改成 used）。
- 突變新增 glow（夜光）、bountiful（豐收）、crystal（水晶）：farm_plots／inventory_items／gamble_prizes 的 mutation CHECK。
- game_transactions：新增事件類型 seed_lab（實驗室操作）、seed_share（分享種子）。
- crop_types：新增 12 種雜交作物（shop_buyable = false，商店不賣，只能從實驗室取得）；種子價格與售價 = 雙親的總和 × 1.5。

降版會刪除上述表與欄位；新突變的作物改成 none，seed_lab／seed_share 交易紀錄改成 use_item（金幣不變），
沒被使用過的雜交作物刪除（種過、偷過的保留）。
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

EVENTS_0038 = (
    "create_farm", "buy_seed", "plant", "harvest", "sell", "upgrade",
    "buy_item", "use_item", "water", "clear", "steal", "stolen", "bind_group",
    "steal_blocked", "defended", "defense_place",
    "build", "buy_animal", "feed_animal", "collect_animal", "craft_start", "craft_collect",
    "quest_reward", "competition_reward", "admin_adjust", "ranking_reward", "transfer_group",
    "gamble", "explore", "build_machine", "exchange", "make_seed",
    "fishing_buy", "fish_sell", "release_animal", "boss_donate", "boss_reward",
    "rename_farm", "checkin_reward", "achievement_reward",
    "ruins_donate", "ruins_guardian", "rumor_treasure",
)
NEW_EVENTS = ("seed_lab", "seed_share")
MUTATIONS_0038 = ("none", "giant", "golden", "fast", "variant")
NEW_MUTATIONS = ("glow", "bountiful", "crystal")
MUTATION_TABLES = (("farm_plots", True), ("inventory_items", False), ("gamble_prizes", False))

# code, 名稱, 雙親 A, 雙親 B, 生長秒數, 收成數量, 變種代碼, 變種名稱, 季節（None = 不限）
# 種子價格與單位售價 = 雙親（資料庫目前的值）的總和 × 1.5，四捨五入
HYBRIDS = (
    ("golden_grain", "黃金穀物", "wheat", "corn", 7200, 3, "sun_grain", "太陽穀", None),
    ("rainbow_carrot", "彩虹蘿蔔", "carrot", "tomato", 14400, 4, "aurora_carrot", "極光蘿蔔", ["spring", "autumn", "winter"]),
    ("purple_berry", "紫莓", "strawberry", "grape", 43200, 5, "midnight_berry", "午夜紫莓", None),
    ("honey_melon", "蜜糖瓜", "pumpkin", "watermelon", 64800, 4, "sugar_crystal_melon", "糖晶瓜", ["spring", "summer", "autumn"]),
    ("lantern_gourd", "燈籠瓜", "amber_melon", "pumpkin", 43200, 4, "golden_lantern_gourd", "金燈籠瓜",
     ["summer", "autumn", "winter"]),
    ("starry_night_flower", "星夜花", "moonflower", "strawberry", 86400, 3, "galaxy_flower", "銀河花", ["spring", "winter"]),
    ("ruby_corn", "紅寶玉米", "corn", "tomato", 14400, 4, "blaze_corn", "烈焰玉米", ["spring", "summer", "winter"]),
    ("sakura_wheat", "櫻花麥", "wheat", "strawberry", 18000, 4, "night_sakura_wheat", "夜櫻麥", None),
    ("sunset_squash", "夕陽瓜", "carrot", "pumpkin", 28800, 4, "afterglow_squash", "晚霞瓜", ["summer", "autumn", "winter"]),
    ("jade_grape", "翡翠葡萄", "grape", "watermelon", 72000, 5, "imperial_jade_grape", "帝王翡翠葡萄", ["spring", "summer", "autumn"]),
    ("dawn_melon", "曙光瓜", "amber_melon", "watermelon", 72000, 3, "corona_melon", "日冕瓜", None),
    ("crimson_moon_flower", "赤月花", "moonflower", "tomato", 64800, 3, "blood_moon_flower", "血月花", ["spring", "winter"]),
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _set_checks(events: tuple[str, ...], mutations: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions", _in("event_type", events)
    )
    for table, nullable in MUTATION_TABLES:
        op.drop_constraint(op.f(f"ck_{table}_mutation_valid"), table, type_="check")
        expr = _in("mutation", mutations)
        if nullable:
            expr = f"mutation IS NULL OR {expr}"
        op.create_check_constraint(op.f(f"ck_{table}_mutation_valid"), table, expr)


def upgrade() -> None:
    _set_checks(EVENTS_0038 + NEW_EVENTS, MUTATIONS_0038 + NEW_MUTATIONS)
    mutation_sql = _in("mutation", MUTATIONS_0038 + NEW_MUTATIONS)

    op.create_table(
        "seed_labs",
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("level", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("exp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("level BETWEEN 1 AND 10", name=op.f("ck_seed_labs_level_valid")),
        sa.CheckConstraint("exp >= 0", name=op.f("ck_seed_labs_exp_non_negative")),
    )
    op.create_table(
        "lab_seeds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("breeder_farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="SET NULL"), nullable=True),
        sa.Column("crop_code", sa.String(32), nullable=False),
        sa.Column("gene_growth", sa.SmallInteger(), nullable=False),
        sa.Column("gene_yield", sa.SmallInteger(), nullable=False),
        sa.Column("gene_quality", sa.SmallInteger(), nullable=False),
        sa.Column("gene_mutation", sa.SmallInteger(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("mutation", sa.String(16), nullable=False, server_default="none"),
        sa.Column("status", sa.String(16), nullable=False, server_default="stored"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "gene_growth BETWEEN 1 AND 5 AND gene_yield BETWEEN 1 AND 5 AND gene_quality BETWEEN 1 AND 5 "
            "AND gene_mutation BETWEEN 1 AND 5", name=op.f("ck_lab_seeds_genes_valid")),
        sa.CheckConstraint("generation >= 1", name=op.f("ck_lab_seeds_generation_positive")),
        sa.CheckConstraint(mutation_sql, name=op.f("ck_lab_seeds_mutation_valid")),
        sa.CheckConstraint("status IN ('stored', 'busy', 'planted', 'used')", name=op.f("ck_lab_seeds_status_valid")),
    )
    op.create_index("ix_lab_seeds_farm_status", "lab_seeds", ["farm_id", "status"])
    op.create_table(
        "lab_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mother_seed_id", sa.BigInteger(), sa.ForeignKey("lab_seeds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("father_seed_id", sa.BigInteger(), sa.ForeignKey("lab_seeds.id", ondelete="CASCADE"), nullable=True),
        sa.Column("materials", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('cross', 'mutate')", name=op.f("ck_lab_jobs_kind_valid")),
        sa.CheckConstraint("slot >= 1", name=op.f("ck_lab_jobs_slot_positive")),
        sa.CheckConstraint("(kind = 'cross') = (father_seed_id IS NOT NULL)", name=op.f("ck_lab_jobs_father_consistency")),
    )
    op.create_index("uq_lab_jobs_farm_slot_open", "lab_jobs", ["farm_id", "slot"], unique=True,
                    postgresql_where=sa.text("collected_at IS NULL"))
    op.create_table(
        "lab_records",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mother", sa.JSON(), nullable=True),
        sa.Column("father", sa.JSON(), nullable=True),
        sa.Column("result_seed_id", sa.BigInteger(), sa.ForeignKey("lab_seeds.id", ondelete="SET NULL"), nullable=True),
        sa.Column("crop_code", sa.String(32), nullable=False),
        sa.Column("genes", sa.JSON(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("mutation", sa.String(16), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("kind IN ('identify', 'cross', 'mutate')", name=op.f("ck_lab_records_kind_valid")),
    )
    op.create_index("ix_lab_records_farm_id", "lab_records", ["farm_id", "id"])
    op.create_table(
        "lab_seed_shares",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("sender_farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("receiver_farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lab_seed_id", sa.BigInteger(), sa.ForeignKey("lab_seeds.id", ondelete="SET NULL"), nullable=True),
        sa.Column("share_day", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("sender_farm_id <> receiver_farm_id", name=op.f("ck_lab_seed_shares_not_self")),
    )
    op.create_index("ix_lab_seed_shares_sender_day", "lab_seed_shares", ["sender_farm_id", "share_day"])
    op.create_index("ix_lab_seed_shares_receiver_day", "lab_seed_shares", ["receiver_farm_id", "share_day"])
    op.add_column("farm_plots", sa.Column(
        "lab_seed_id", sa.BigInteger(), sa.ForeignKey("lab_seeds.id", ondelete="SET NULL"), nullable=True))

    price = "GREATEST(1, ROUND((COALESCE((SELECT {col} FROM crop_types WHERE code = :pa), 1) "             "+ COALESCE((SELECT {col} FROM crop_types WHERE code = :pb), 1)) * 1.5)::int)"
    crop = sa.text(
        "INSERT INTO crop_types (code, name, seed_price, grow_seconds, harvest_quantity, sell_price, variant_code, "
        f"variant_name, active, shop_buyable, seasons) SELECT :code, :name, {price.format(col='seed_price')}, :grow, :qty, "
        f"{price.format(col='sell_price')}, :vc, :vn, true, false, CAST(:seasons AS json) "
        "WHERE NOT EXISTS (SELECT 1 FROM crop_types WHERE code IN (:code, :vc) OR variant_code IN (:code, :vc) "
        "OR name IN (:name, :vn) OR variant_name IN (:name, :vn))"
    )
    for code, name, pa, pb, grow, qty, vc, vn, seasons in HYBRIDS:
        op.execute(crop.bindparams(code=code, name=name, pa=pa, pb=pb, grow=grow, qty=qty, vc=vc, vn=vn,
                                   seasons=json.dumps(seasons) if seasons else None))


def downgrade() -> None:
    op.drop_column("farm_plots", "lab_seed_id")
    op.drop_table("lab_seed_shares")
    op.drop_table("lab_records")
    op.drop_table("lab_jobs")
    op.drop_table("lab_seeds")
    op.drop_table("seed_labs")
    new = ", ".join(f"'{m}'" for m in NEW_MUTATIONS)
    for table, _ in MUTATION_TABLES:
        op.execute(f"UPDATE {table} SET mutation = 'none' WHERE mutation IN ({new})")
    op.execute("UPDATE game_transactions SET event_type = 'use_item' WHERE event_type IN ('seed_lab', 'seed_share')")
    _set_checks(EVENTS_0038, MUTATIONS_0038)
    op.execute(sa.text(
        "DELETE FROM crop_types c WHERE c.code = ANY(:codes) "
        "AND NOT EXISTS (SELECT 1 FROM farm_plots p WHERE p.crop_type_id = c.id) "
        "AND NOT EXISTS (SELECT 1 FROM steal_records s WHERE s.crop_type_id = c.id) "
        "AND NOT EXISTS (SELECT 1 FROM game_events e WHERE e.crop_type_id = c.id)"
    ).bindparams(codes=[h[0] for h in HYBRIDS]))
