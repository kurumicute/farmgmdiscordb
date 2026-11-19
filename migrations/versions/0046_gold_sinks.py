"""gold sinks: badges, themes, pet skins, material bag, reputation, weekly auction

Revision ID: 0046
Revises: 0045
Create Date: 2026-11-19

Go 版新增（Python 版沒有對應的程式），金幣回收第二波。
- farms 新增欄位：
  - badge：目前裝備的名牌代碼（排行榜、日報、Boss 排行名字前面的 emoji），NULL = 不顯示。
  - theme：目前套用的農場外觀主題代碼（snow／sakura／night…），NULL = 預設外觀。
  - material_bag_level：素材背包擴充等級 0～10（容量見 exploration 設定）。
  - reputation：農場聲望（捐給農會的金幣累積，1 金 = 1 點），不會減少。
- 新表 farm_cosmetics：擁有的外觀（kind = badge／theme／hat／color），每種只會有一列；source 記錄來源
  （shop／reputation／auction／admin）。
- farm_pets 新增 hat、color：這隻寵物目前戴的帽子與顏色（NULL = 沒有）。
- 新表 auctions：每週系統拍賣（week_start 是台灣時間的週一，唯一）；status：open／settled／unsold。
  出價時金幣先凍結（直接從出價者扣除），被超過時退回；結標時最高出價者得標，金幣全數回收。
- 新表 auction_bids：每次出價一列（含被超過的出價），refunded_at 記錄退回時間。
- 交易類型新增 reputation_donate（捐給農會）、auction_bid（出價凍結）、auction_refund（被超過退回）、
  auction_win（得標紀錄，金額 0）。購買主題、名牌、帽子、顏色沿用 buy_item。

降版會刪除 auctions、auction_bids、farm_cosmetics 與上面新增的欄位（已扣的金幣不退、聲望與擁有的外觀消失），
並把這四種交易改記成 admin_adjust（保留金額與說明，金幣與排行統計不變）。
降版前若有進行中的拍賣，凍結中的金幣不會自動退回，請先結標或手動處理。
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)

EVENTS_0045 = (
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
)
NEW_EVENTS = ("reputation_donate", "auction_bid", "auction_refund", "auction_win")


def _set_events(events: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_game_transactions_event_type_valid"), "game_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_game_transactions_event_type_valid"), "game_transactions",
        "event_type IN (" + ", ".join(f"'{v}'" for v in events) + ")",
    )


def upgrade() -> None:
    _set_events(EVENTS_0045 + NEW_EVENTS)

    op.add_column("farms", sa.Column("badge", sa.String(20), nullable=True))
    op.add_column("farms", sa.Column("theme", sa.String(20), nullable=True))
    op.add_column("farms", sa.Column("material_bag_level", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("farms", sa.Column("reputation", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    op.create_check_constraint(op.f("ck_farms_material_bag_level_range"), "farms",
                               "material_bag_level BETWEEN 0 AND 10")
    op.create_check_constraint(op.f("ck_farms_reputation_non_negative"), "farms", "reputation >= 0")

    op.create_table(
        "farm_cosmetics",
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("obtained_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("farm_id", "kind", "code", name=op.f("pk_farm_cosmetics")),
        sa.CheckConstraint("kind IN ('badge', 'theme', 'hat', 'color')", name=op.f("ck_farm_cosmetics_kind_valid")),
    )

    op.add_column("farm_pets", sa.Column("hat", sa.String(30), nullable=True))
    op.add_column("farm_pets", sa.Column("color", sa.String(30), nullable=True))

    op.create_table(
        "auctions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("item_kind", sa.String(10), nullable=False),
        sa.Column("item_code", sa.String(30), nullable=False),
        sa.Column("start_price", sa.BigInteger(), nullable=False),
        sa.Column("min_increment_pct", sa.Integer(), nullable=False),
        sa.Column("starts_at", TS, nullable=False),
        sa.Column("ends_at", TS, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default=sa.text("'open'")),
        sa.Column("top_bid", sa.BigInteger(), nullable=True),
        sa.Column("top_farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="SET NULL"), nullable=True),
        sa.Column("settled_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("week_start", name=op.f("uq_auctions_week_start")),
        sa.CheckConstraint("status IN ('open', 'settled', 'unsold')", name=op.f("ck_auctions_status_valid")),
        sa.CheckConstraint("item_kind IN ('decor', 'badge', 'title')", name=op.f("ck_auctions_item_kind_valid")),
        sa.CheckConstraint("start_price > 0", name=op.f("ck_auctions_start_price_positive")),
        sa.CheckConstraint("min_increment_pct BETWEEN 1 AND 100", name=op.f("ck_auctions_min_increment_range")),
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_auctions_ends_after_starts")),
    )
    op.create_index("ix_auctions_status_ends_at", "auctions", ["status", "ends_at"])

    op.create_table(
        "auction_bids",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("auction_id", sa.BigInteger(), sa.ForeignKey("auctions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), sa.ForeignKey("farms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("refunded_at", TS, nullable=True),
        sa.CheckConstraint("amount > 0", name=op.f("ck_auction_bids_amount_positive")),
    )
    op.create_index("ix_auction_bids_auction_amount", "auction_bids", ["auction_id", "amount"])


def downgrade() -> None:
    op.drop_table("auction_bids")
    op.drop_table("auctions")
    op.drop_column("farm_pets", "color")
    op.drop_column("farm_pets", "hat")
    op.drop_table("farm_cosmetics")
    op.drop_constraint(op.f("ck_farms_reputation_non_negative"), "farms", type_="check")
    op.drop_constraint(op.f("ck_farms_material_bag_level_range"), "farms", type_="check")
    op.drop_column("farms", "reputation")
    op.drop_column("farms", "material_bag_level")
    op.drop_column("farms", "theme")
    op.drop_column("farms", "badge")
    op.execute("UPDATE game_transactions SET event_type = 'admin_adjust' WHERE event_type IN ("
               + ", ".join(f"'{v}'" for v in NEW_EVENTS) + ")")
    _set_events(EVENTS_0045)
