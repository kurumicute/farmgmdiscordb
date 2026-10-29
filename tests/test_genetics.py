"""作物品質與突變。"""

import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from app.auth import AdminAuth, hash_password
from app.cli.commands import CommandHandler
from app.db.models import AdminAction, CropType, Farm, FarmPlot, GameTransaction, InventoryItem
from app.db.session import make_engine, make_session_factory
from app.game import balance, genetics
from app.game.errors import (
    InsufficientItemsError,
    PlotEmptyError,
    PlotNotWateredError,
    PlotWitheredError,
    ValidationError,
)
from app.main import create_app
from app.services.admin_service import AdminService
from app.services.community_service import CommunityService
from tests.conftest import BASELINE_GENETICS, make_farmer, set_test_crop_times

ROOT = Path(__file__).resolve().parents[1]
D = balance.DEFAULT_GENETICS


class Dice:
    """依序回傳指定的亂數（用完後重複最後一個）。"""

    def __init__(self, *values: float) -> None:
        self.values = list(values)
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.calls += 1
            return self.values[min(self.calls - 1, len(self.values) - 1)]


def settings(**overrides) -> dict:
    s = {k: (dict(v) if isinstance(v, dict) else v) for k, v in D.items()}
    s.update(overrides)
    return s


def svc(session_factory, clock, *rolls) -> CommunityService:
    """genetics_rng 依序回傳 (品質亂數, 突變亂數, ...)。"""
    return CommunityService(session_factory, clock, genetics_rng=Dice(*rolls))


@pytest.fixture
def farmer(service, home_group, session_factory):
    service.update_genetics("admin", settings())  # 使用預設機率（版本 2）
    pid = make_farmer(service, "育種家", home_group)
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == pid).values(gold=100_000))
    return pid


def plant(service, pid, plot=1, crop="wheat", water=True):
    service.buy_seeds(pid, crop, 1)
    service.plant_crop(pid, plot, crop)
    if water:
        service.water_crop(pid, plot)


def plot_row(session_factory, pid, n=1):
    with session_factory() as s:
        farm_id = s.scalar(select(Farm.id).where(Farm.player_id == pid))
        return s.scalars(select(FarmPlot).where(FarmPlot.farm_id == farm_id, FarmPlot.plot_number == n)).one()


def stacks(session_factory, pid):
    with session_factory() as s:
        farm_id = s.scalar(select(Farm.id).where(Farm.player_id == pid))
        rows = s.scalars(select(InventoryItem).where(InventoryItem.farm_id == farm_id, InventoryItem.quantity > 0))
        return sorted(
            (r.item_code, r.quality, r.mutation, r.quantity) for r in rows if not r.item_code.startswith("seed_")
        )


# ---------- 純規則 ----------


def test_default_probabilities_sum_to_100():
    s = genetics.validate_settings(D)
    assert sum(s["quality_bp"].values()) == 10_000
    assert {q: v / 100 for q, v in s["quality_bp"].items()} == {"normal": 70, "good": 20, "rare": 8, "legendary": 2}
    assert {q: v / 100 for q, v in s["quality_price_pct"].items()} == {
        "normal": 1.0,
        "good": 1.2,
        "rare": 1.5,
        "legendary": 2.0,
    }


@pytest.mark.parametrize(
    "bad",
    [
        settings(quality_bp={"normal": 6900, "good": 2000, "rare": 800, "legendary": 200}),  # 總和 99%
        settings(quality_bp={"normal": 7000, "good": 2000, "rare": 800}),  # 缺少傳說
        settings(quality_price_pct={"normal": 100, "good": 120, "rare": 150, "legendary": 5000}),
        settings(mutation_bp={"giant": 2000, "golden": 2000, "fast": 2000, "variant": 0}),  # 突變總和 60%
        settings(giant_multiplier=0),
        settings(fast_reduction_seconds=-1),
        settings(level_bonus_max_bp=7500),  # 超出範圍且大於普通機率
        settings(quality_bp={"normal": 300, "good": 7500, "rare": 2000, "legendary": 200}),  # 普通 < 加成上限
        settings(golden_price_pct=2.5),
    ],
)
def test_invalid_settings_rejected(bad):
    with pytest.raises(ValidationError):
        genetics.validate_settings(bad)


def test_level_bonus_capped_and_sums_to_100():
    s = genetics.validate_settings(D)
    assert genetics.effective_quality_bp(s, 1) == s["quality_bp"]
    for level in (2, 5, 11, 50):
        bp = genetics.effective_quality_bp(s, level)
        assert sum(bp.values()) == 10_000
        assert bp["normal"] >= s["quality_bp"]["normal"] - s["level_bonus_max_bp"]
    assert genetics.effective_quality_bp(s, 50) == genetics.effective_quality_bp(s, 11)  # 上限 5%
    assert genetics.effective_quality_bp(s, 11)["normal"] == 6500


def test_rolls_and_formulas():
    s = genetics.validate_settings(D)
    assert [genetics.roll_quality(s["quality_bp"], u) for u in (0, 0.6999, 0.7, 0.9, 0.98, 0.9999)] == [
        "normal",
        "normal",
        "good",
        "rare",
        "legendary",
        "legendary",
    ]
    # 突變區間在最上方（共 8%）：[0.92, 0.95) 巨型、[0.95, 0.96) 黃金、[0.96, 0.99) 快速、[0.99, 1) 變種
    assert [genetics.roll_mutation(s, u) for u in (0, 0.9199, 0.92, 0.95, 0.96, 0.99)] == [
        "none",
        "none",
        "giant",
        "golden",
        "fast",
        "variant",
    ]
    # 最終單價 = 基本售價 × 品質倍率 × 突變售價倍率，整數運算後無條件捨去
    assert genetics.unit_price(15, "rare", "none", s) == 22  # 22.5 → 22
    assert genetics.unit_price(15, "legendary", "golden", s) == 90
    assert genetics.unit_price(15, "normal", "giant", s) == 15  # 巨型只影響數量
    assert genetics.unit_price(15, "normal", "fast", s) == 15  # 快速只影響時間
    assert genetics.unit_price(25, "good", "variant", s) == 120  # 25×4=100，×1.2
    assert genetics.harvest_quantity(2, "giant", s) == 4 and genetics.harvest_quantity(2, "golden", s) == 2
    assert genetics.grow_seconds(7200, "fast", False, s) == balance.MIN_GROW_SECONDS  # 不會變成 0 或負數
    assert genetics.grow_seconds(10_000, "fast", False, s) == 2800
    assert genetics.grow_seconds(7200, "fast", True, s) == balance.MIN_GROW_SECONDS
    assert genetics.grow_seconds(600, "none", True, s) == 450


# ---------- 固定品質與重啟 ----------


def test_quality_fixed_at_planting_and_survives_restart(farmer, session_factory, clock, database_url):
    s = svc(session_factory, clock, 0.95, 0.0)  # 稀有、無突變
    plant(s, farmer)
    row = plot_row(session_factory, farmer)
    assert (row.quality, row.mutation) == ("rare", "none")
    # 重新查詢、程式重啟都不重新抽取（新服務的亂數若被使用會得到傳說）
    engine = make_engine(database_url)
    try:
        restarted = CommunityService(make_session_factory(engine), clock, genetics_rng=Dice(0.999, 0.999))
        clock.advance(60)
        for _ in range(3):
            status = restarted.get_farm_status(farmer).plots[0]
            assert (status.quality, status.mutation) == ("rare", "none")
        result = restarted.harvest_crop(farmer, 1)
        assert "稀有" in result.message
    finally:
        engine.dispose()
    assert stacks(session_factory, farmer) == [("wheat", "rare", "none", 2)]
    with pytest.raises(PlotEmptyError):  # 不能重複收成
        s.harvest_crop(farmer, 1)
    assert stacks(session_factory, farmer) == [("wheat", "rare", "none", 2)]


def test_settings_change_does_not_affect_planted_crops(farmer, service, session_factory, clock):
    s = svc(session_factory, clock, 0.75, 0.0, 0.75, 0.0)  # 依當時設定：優良
    plant(s, farmer, 1)
    service.update_genetics("admin", settings(quality_bp={"normal": 9000, "good": 500, "rare": 300, "legendary": 200}))
    plant(s, farmer, 2)  # 新設定下 0.75 → 普通
    assert plot_row(session_factory, farmer, 1).quality == "good"
    assert plot_row(session_factory, farmer, 2).quality == "normal"


def test_level_bonus_applies_at_planting(farmer, service, session_factory, clock):
    AdminService(session_factory, clock).set_farm_values("admin", farmer, level=11)  # 加成 5%：普通 65%
    s = svc(session_factory, clock, 0.66, 0.0)
    plant(s, farmer)
    assert plot_row(session_factory, farmer).quality == "good"  # 等級 1 時 0.66 會是普通
    assert service.quality_table(farmer)["effective_bp"]["normal"] == 6500


# ---------- 售價與交易紀錄 ----------


def test_sale_price_recorded_and_history_kept(farmer, service, session_factory, clock):
    s = svc(session_factory, clock, 0.95, 0.0)  # 稀有
    plant(s, farmer)
    clock.advance(60)
    s.harvest_crop(farmer, 1)
    with pytest.raises(InsufficientItemsError):
        service.sell(farmer, "wheat", 1)  # 預設賣普通，沒有普通庫存
    before = service.get_farm_status(farmer).gold
    result = service.sell(farmer, "小麥", 2, "rare")
    assert result.gold_delta == 44 and service.get_farm_status(farmer).gold == before + 44  # 15×1.5=22.5→22，×2
    with session_factory.begin() as sess:
        sess.execute(update(CropType).where(CropType.code == "wheat").values(sell_price=999))
    with session_factory() as sess:
        tx = sess.scalars(select(GameTransaction).where(GameTransaction.event_type == "sell")).one()
        assert (tx.unit_price, tx.gold_delta, tx.quality, tx.mutation, tx.quantity) == (22, 44, "rare", "none", 2)
    with pytest.raises(ValidationError):
        service.sell(farmer, "雞蛋", 1, "rare")  # 商品沒有品質


def test_golden_and_legendary_price(farmer, service, session_factory, clock):
    s = svc(session_factory, clock, 0.99, 0.955)  # 傳說、黃金
    plant(s, farmer)
    clock.advance(60)
    s.harvest_crop(farmer, 1)
    assert stacks(session_factory, farmer) == [("wheat", "legendary", "golden", 2)]
    backpack = service.get_backpack(farmer)
    assert [(x.name, x.quality, x.mutation, x.price) for x in backpack.produce] == [("小麥", "legendary", "golden", 90)]
    assert service.sell(farmer, "wheat", 2, "傳說", "golden").gold_delta == 180


# ---------- 偷菜保留品質 ----------


def test_stolen_crop_keeps_quality_and_mutation(farmer, service, home_group, session_factory, clock):
    thief = make_farmer(service, "小偷", home_group)
    s = svc(session_factory, clock, 0.99, 0.955)  # 傳說、黃金
    plant(s, farmer, crop="strawberry")
    clock.advance(600)
    thief_svc = svc(session_factory, clock, 0.0, 0.0)  # 若重新抽取會變成普通
    assert thief_svc.steal_crop(home_group, thief, farmer, 1, 2).success
    assert stacks(session_factory, thief) == [("strawberry", "legendary", "golden", 2)]
    s.harvest_crop(farmer, 1)
    assert stacks(session_factory, farmer) == [("strawberry", "legendary", "golden", 3)]
    with session_factory() as sess:
        steal_tx = sess.scalars(select(GameTransaction).where(GameTransaction.event_type == "steal")).one()
        assert (steal_tx.quality, steal_tx.mutation) == ("legendary", "golden")


# ---------- 巨型作物 ----------


def test_giant_doubles_quantity_once(farmer, service, home_group, session_factory, clock):
    thief = make_farmer(service, "小偷", home_group)
    s = svc(session_factory, clock, 0.0, 0.93)  # 普通、巨型
    plant(s, farmer)
    assert plot_row(session_factory, farmer).remaining_quantity == 4
    clock.advance(60)
    service.steal_crop(home_group, thief, farmer, 1, 2)
    result = service.harvest_crop(farmer, 1)
    assert result.quantity == 2 and "已被偷走 2 個" in result.message
    with pytest.raises(PlotEmptyError):
        service.harvest_crop(farmer, 1)
    assert stacks(session_factory, farmer) == [("wheat", "normal", "giant", 2)]
    assert stacks(session_factory, thief) == [("wheat", "normal", "giant", 2)]


def test_concurrent_harvest_of_giant_only_once(farmer, service, session_factory, clock):
    s = svc(session_factory, clock, 0.0, 0.93)
    plant(s, farmer)
    clock.advance(60)
    barrier = threading.Barrier(5)
    results = []

    def run():
        barrier.wait()
        try:
            results.append(service.harvest_crop(farmer, 1).quantity)
        except PlotEmptyError:
            results.append(0)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [0, 0, 0, 0, 4]
    assert stacks(session_factory, farmer) == [("wheat", "normal", "giant", 4)]


# ---------- 快速生長、澆水與乾枯 ----------


def test_fast_growth_needs_water_and_withers(farmer, service, session_factory, clock):
    service.update_crop_grow_seconds("admin", "wheat", 7200)
    s = svc(session_factory, clock, 0.0, 0.97)  # 普通、快速生長
    plant(s, farmer, water=False)
    clock.advance(3 * 3600)
    with pytest.raises(PlotNotWateredError):  # 沒澆水就不會生長
        s.harvest_crop(farmer, 1)
    s.water_crop(farmer, 1)
    status = service.get_farm_status(farmer).plots[0]
    assert status.remaining_seconds == balance.MIN_GROW_SECONDS  # 2 小時 − 2 小時 → 最短 60 秒
    clock.advance(balance.MIN_GROW_SECONDS + balance.WITHER_SECONDS)
    with pytest.raises(PlotWitheredError):  # 突變不能讓乾枯作物復活
        s.harvest_crop(farmer, 1)
    s.clear_withered(farmer, 1)
    assert stacks(session_factory, farmer) == []  # 乾枯作物不產生任何物品或品質獎勵


# ---------- 珍稀變種 ----------


def test_rare_variant_is_separate_crop(farmer, service, session_factory, clock):
    s = svc(session_factory, clock, 0.75, 0.995)  # 優良、珍稀變種
    plant(s, farmer)
    clock.advance(60)
    result = s.harvest_crop(farmer, 1)
    assert "琥珀小麥" in result.message
    assert stacks(session_factory, farmer) == [("amber_wheat", "good", "variant", 2)]
    with pytest.raises(ValidationError):
        service.sell(farmer, "wheat", 1, "good", "variant")  # 須用變種名稱
    sold = service.sell(farmer, "琥珀小麥", 2, "優良")
    assert sold.gold_delta == 2 * (15 * 4 * 120 // 100)  # 基本售價 60 × 1.2 = 72
    sales = {r["player_name"]: (r["sold_quantity"], r["sales_income"]) for r in service.leaderboard("sales")}
    assert sales["育種家"] == (2, 144)  # 計入作物販售，一次販售只算一次


def test_inventory_stacks_kept_separate(farmer, service, session_factory, clock):
    for rolls, plot in (((0.0, 0.0), 1), ((0.95, 0.0), 2), ((0.95, 0.955), 3)):
        plant(svc(session_factory, clock, *rolls), farmer, plot)
    clock.advance(60)
    for n in (1, 2, 3):
        service.harvest_crop(farmer, n)
    assert stacks(session_factory, farmer) == [
        ("wheat", "normal", "none", 2),
        ("wheat", "rare", "golden", 2),
        ("wheat", "rare", "none", 2),
    ]
    assert service.get_backpack(farmer).used == 6


# ---------- 設定管理 ----------


def test_admin_settings_and_grow_time(farmer, service, session_factory):
    with pytest.raises(ValidationError, match="100%"):
        service.update_genetics(
            "admin", settings(quality_bp={"normal": 7000, "good": 2000, "rare": 800, "legendary": 100})
        )
    assert service.genetics_settings()[0] == 2  # 無效設定沒有套用
    version = service.update_genetics("admin", settings(golden_price_pct=500))
    assert version == 3 and service.genetics_settings()[1]["golden_price_pct"] == 500
    with pytest.raises(ValidationError):
        service.update_crop_grow_seconds("admin", "wheat", 10)
    service.update_crop_grow_seconds("admin", "番茄", 3600)
    assert service.find_crop_info("tomato")["grow_seconds"] == 3600
    with session_factory() as s:
        actions = [a.action for a in s.scalars(select(AdminAction).order_by(AdminAction.id))]
    assert actions == ["update_genetics", "update_genetics", "update_crop_grow_time"]


def test_migrations_set_growth_times_and_default_settings(engine, database_url):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    engine.dispose()
    command.downgrade(cfg, "0006")
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT grow_seconds FROM crop_types WHERE code = 'wheat'")) == 60
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        # 0007 設為 2 小時，0011 再改為小麥 1、胡蘿蔔 2、番茄 4、草莓 8 小時
        rows = dict(conn.execute(text("SELECT code, grow_seconds FROM crop_types")).all())
        assert rows == {"wheat": 3600, "carrot": 7200, "tomato": 14400, "strawberry": 28800}
        row = conn.execute(text("SELECT version, created_by, settings FROM crop_genetics_settings")).one()
        assert row[0] == 1 and row[1] == "migration"
        assert genetics.validate_settings(row[2]) == genetics.validate_settings(D)
    set_test_crop_times(engine)


# ---------- CMD ----------


def test_cli_crop_commands(farmer, service, session_factory, clock):
    cli = CommandHandler(svc(session_factory, clock, 0.95, 0.955))  # 稀有、黃金
    cli.handle(f"player use {farmer}")
    info = cli.handle("crop info 小麥")
    assert "琥珀小麥" in info and "稀有 22" in info and "巨型作物：4 個" in info
    quality = cli.handle("crop quality")
    assert "70.00%" in quality and "×1.5" in quality and "我的實際機率" in quality
    mutations = cli.handle("crop mutations")
    assert "巨型作物" in mutations and "3.00%" in mutations and "收成數量 ×2" in mutations
    prices = cli.handle("market prices")
    assert "白雪草莓" in prices and "最終單價" in prices
    cli.handle("buy wheat 1")
    assert "【稀有・黃金作物】" in cli.handle("plant 1 wheat")
    cli.handle("water 1")
    clock.advance(60)
    cli.handle("harvest 1")
    inv = cli.handle("inventory")
    assert "小麥【稀有・黃金作物】" in inv and "單價 67" in inv  # 15 × 1.5 × 3 = 67.5 → 67
    assert "數量不足" in cli.handle("sell wheat 1")
    assert "獲得 134 金幣" in cli.handle("sell 小麥 2 稀有 黃金")
    assert "看不懂" in cli.handle("sell 小麥 1 超級")


# ---------- 網站 ----------


def test_genetics_web_api(farmer, service, session_factory, clock):
    pw = "genetics-admin-pass"
    app = create_app(
        session_factory, clock=clock, allowed_hosts=["testserver"], admin_auth=AdminAuth("admin", hash_password(pw))
    )
    hdr = {"X-Farmgm-Admin": "1"}
    with TestClient(app) as c:
        stats = c.get("/api/monitor/genetics/stats").json()
        assert stats == {"planted": [], "harvested": [], "stolen": [], "sold": [], "holdings": []}  # 沒有紀錄時不造假
        data = c.get("/api/monitor/genetics").json()
        assert data["version"] == 2 and data["settings"]["quality_bp"]["normal"] == 7000
        assert c.put("/api/admin/genetics", json=settings(), headers=hdr).status_code == 401
        c.post("/api/auth/login", json={"username": "admin", "password": pw}, headers=hdr)
        bad = settings(quality_bp={"normal": 7000, "good": 2000, "rare": 800, "legendary": 300})
        res = c.put("/api/admin/genetics", json=bad, headers=hdr)
        assert res.status_code == 400 and "100%" in res.json()["detail"]
        assert c.get("/api/monitor/genetics").json()["version"] == 2
        assert c.put("/api/admin/genetics", json=settings(giant_multiplier=3), headers=hdr).json() == {"version": 3}
        assert c.put("/api/admin/crops/wheat/grow-seconds", json={"grow_seconds": 5}, headers=hdr).status_code == 400
        assert c.put("/api/admin/crops/wheat/grow-seconds", json={"grow_seconds": 3600}, headers=hdr).status_code == 200

        s = svc(session_factory, clock, 0.95, 0.93)  # 稀有、巨型（×3）
        plant(s, farmer)
        assert c.get("/api/monitor/genetics/stats").json()["planted"] == [
            {"quality": "rare", "mutation": "giant", "plots": 1}
        ]
        clock.advance(3600)
        s.harvest_crop(farmer, 1)
        service.sell(farmer, "wheat", 6, "rare", "giant")
        stats = c.get("/api/monitor/genetics/stats").json()
        assert stats["harvested"] == [{"quality": "rare", "mutation": "giant", "count": 1, "quantity": 6}]
        assert stats["sold"] == [{"quality": "rare", "mutation": "giant", "count": 1, "quantity": 6, "income": 132}]
        assert stats["holdings"] == [] and stats["planted"] == []
        assert c.post("/api/monitor/genetics", json={}).status_code == 405
    with session_factory() as sess:
        assert sess.scalar(select(func.count()).select_from(AdminAction)) == 3  # fixture 1 次 + 測試 2 次


def test_baseline_settings_used_by_other_tests(service):
    assert service.genetics_settings() == (1, BASELINE_GENETICS)
