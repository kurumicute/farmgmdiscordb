"""季節與時段限定（Migration 0034）：規則、作物只能在季節內種下、魚只在季節／時段內釣得到、管理 API 與魚餌等級。

conftest 每個測試把限定清空（ManualClock 從台灣時間 1 月 1 日 08:00 冬天開始，避免影響其他測試），
這裡以 apply_defaults() 套用和正式環境相同的預設限定。
"""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text, update

from app.db.models import Farm
from app.game import balance, exploration, fishing, seasons
from app.game.errors import ValidationError
from app.services.admin_service import AdminService

ROOT = Path(__file__).resolve().parents[1]


def taipei(month, day=1, hour=12):
    return datetime(2026, month, day, hour, tzinfo=UTC) - timedelta(hours=8)


def apply_defaults(engine):
    with engine.begin() as conn:
        for code, s in seasons.CROP_SEASONS.items():
            conn.execute(text("UPDATE crop_types SET seasons = CAST(:s AS json) WHERE code = :c"),
                         {"s": json.dumps(list(s)), "c": code})
        for code, (s, hours) in seasons.FISH_LIMITS.items():
            conn.execute(text("UPDATE fish_types SET seasons = CAST(:s AS json), hour_start = :a, hour_end = :b "
                              "WHERE code = :c"),
                         {"s": json.dumps(list(s)) if s else None, "a": hours[0] if hours else None,
                          "b": hours[1] if hours else None, "c": code})


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


# ---------- 規則 ----------


def test_rules_real_seasons_and_hours():
    assert [seasons.season_of(taipei(m)) for m in (3, 6, 9, 12, 2)] == ["spring", "summer", "autumn", "winter",
                                                                        "winter"]
    assert seasons.season_of(datetime(2026, 2, 28, 16, 0, tzinfo=UTC)) == "spring"  # 台灣時間 3/1 00:00
    assert seasons.in_hours(taipei(1, hour=20), 18, 6) and seasons.in_hours(taipei(1, hour=5), 18, 6)
    assert not seasons.in_hours(taipei(1, hour=6), 18, 6) and not seasons.in_hours(taipei(1, hour=12), 18, 6)
    assert seasons.in_hours(taipei(1, hour=6), 6, 18) and not seasons.in_hours(taipei(1, hour=18), 6, 18)
    assert seasons.available(None, None, None, taipei(1)) and not seasons.available(["summer"], None, None, taipei(1))
    assert seasons.describe(["autumn"]) == "🍂秋季限定" and seasons.describe(None, 18, 6) == "🕒 18:00–06:00"
    assert seasons.describe(None) == ""
    assert seasons.validate(["winter", "spring"], None, None) == (["spring", "winter"], None, None)
    assert seasons.validate([], 18, 6) == (None, 18, 6)
    for bad in ((["rainy"], None, None), (None, 18, None), (None, 5, 5), (None, 24, 1)):
        with pytest.raises(ValidationError):
            seasons.validate(*bad)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "migrations" / "versions" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_migration_constants_match_rules():
    m = load("0034_seasons_and_bait_levels")
    m35 = load("0035_special_fish")
    assert {c: tuple(s) for c, s in m.CROP_SEASONS.items()} == seasons.CROP_SEASONS
    limits = {c: (tuple(s) if s else None, (a, b) if a is not None else None) for c, (s, a, b) in m.FISH_LIMITS.items()}
    limits.update({f[0]: (tuple(f[12]) if f[12] else None, (f[13], f[14])) for f in m35.FISH})
    assert limits == seasons.FISH_LIMITS
    assert {f[0]: tuple(f[11]) for f in m35.FISH if f[11]} == {
        c: b for c, b in fishing.FISH_REQUIRED_BAITS.items() if c in {f[0] for f in m35.FISH}}
    # INITIAL_FISH 的售價是 Migration 0037、0038 各調高 1.5 倍（.5 進位）後的值
    def up(price):
        return (price * 3 + 1) // 2

    raised = [(*f[:4], up(up(f[4])), up(up(f[5])), *f[6:10]) for f in m35.FISH]
    assert raised == [tuple(x) for x in fishing.INITIAL_FISH[35:]]
    new_fish = {f[0] for f in fishing.INITIAL_FISH[15:]}
    assert set(seasons.FISH_LIMITS) <= new_fish  # 只限新增的魚
    # 所有作物（小麥除外）都是兩個季節，而且每季至少 4 種作物可以種
    crops = {c[0] for c in balance.INITIAL_CROPS} | {c[0] for c in exploration.RARE_CROPS}
    assert set(seasons.CROP_SEASONS) == crops - {"wheat"}
    assert all(len(s) == 2 for s in seasons.CROP_SEASONS.values())
    for season in seasons.SEASONS:
        assert sum(season in s for s in seasons.CROP_SEASONS.values()) >= 4
    # 魚餌等級全部調低 2 級（最低 Lv.1）
    levels = {g[0]: g[5] for g in fishing.INITIAL_GEAR if g[1] == "bait"}
    assert levels == {c: max(1, lv - 2) for c, lv in m.BAIT_LEVELS_0033.items()}


# ---------- 作物 ----------


def test_crop_only_planted_in_season(admin, engine, clock, player_id):
    apply_defaults(engine)
    admin.set_farm_values("test", player_id, gold=10_000)
    admin.buy_seeds(player_id, "corn", 3)  # 種子隨時可以買
    info = {c.code: c for c in admin.get_market_prices()}
    assert not info["corn"].in_season and info["corn"].season_text == "🌸☀️春、夏季限定" and info["wheat"].in_season
    assert info["strawberry"].in_season and info["carrot"].in_season  # 現在（1 月）是冬天
    with pytest.raises(ValidationError, match="春、夏季限定"):
        admin.plant_crop(player_id, 1, "corn")
    with pytest.raises(ValidationError, match="春、夏季限定"):
        admin.plant_all(player_id, "玉米")
    clock._now = taipei(3, 1, 9)
    admin.plant_crop(player_id, 2, "corn")  # 春天可以種
    clock._now = taipei(9, 1, 0) - timedelta(hours=1)  # 8/31 23:00（夏天）種下
    admin.plant_crop(player_id, 3, "corn")
    admin.water_crop(player_id, 3)
    clock._now = taipei(9, 1, 0)  # 過了夏天（秋天）：不能再種，但已經種下的照樣收成
    with pytest.raises(ValidationError, match="春、夏季限定"):
        admin.plant_crop(player_id, 1, "corn")
    assert admin.harvest_crop(player_id, 3).quantity == 3

# ---------- 魚 ----------


def fishable(admin, session_factory, player_id, monkeypatch, bait="dragon_bait") -> set[str]:
    with session_factory.begin() as s:
        s.execute(update(Farm).where(Farm.player_id == player_id).values(level=10, gold=100_000))
    for code in ("bamboo_rod", "basic_line"):
        try:
            admin.buy_fishing_gear(player_id, code)
        except Exception:  # 已經有了
            pass
    admin.buy_fishing_gear(player_id, bait, 1)
    seen = set()

    def capture(pools, rarity, rng):
        seen.update(f.code for pool in pools.values() for f in pool)
        return pools["common"][0]

    monkeypatch.setattr(fishing, "pick_fish", capture)
    view = admin.start_fishing(player_id)
    admin.give_up_fishing(player_id, view["id"])
    return seen


def test_fish_only_in_season_and_hours(admin, engine, session_factory, clock, player_id, monkeypatch):
    apply_defaults(engine)
    clock._now = taipei(1, 10, 9)  # 冬天早上 9 點
    seen = fishable(admin, session_factory, player_id, monkeypatch)
    assert {"pond_smelt", "mullet", "bluegill"} <= seen
    assert not {"eel", "snakehead", "moon_ray", "mackerel", "swordfish", "yellowfin_tuna", "dragon_king_carp"} & seen
    clock._now = taipei(4, 10, 21)  # 春天晚上 9 點
    seen = fishable(admin, session_factory, player_id, monkeypatch)
    assert {"eel", "snakehead", "moon_ray", "dragon_king_carp"} <= seen
    assert not {"bluegill", "pond_smelt", "mullet"} & seen
    codex = {x["code"]: x for x in admin.fish_codex(player_id)["entries"]}
    assert codex["eel"]["limit_text"] == "🕒 18:00–06:00" and codex["mullet"]["limit_text"] == "❄️冬季限定"


# ---------- 管理 API ----------


def test_admin_limits_kept_when_not_sent(admin):
    crop = {"name": "茄子", "variant_name": "紫晶茄子", "seed_price": 10, "grow_seconds": 600,
            "harvest_quantity": 2, "sell_price": 5}
    admin.create_crop("root", {**crop, "code": "eggplant", "variant_code": "violet_eggplant",
                               "limits": {"seasons": ["summer"], "hour_start": 6, "hour_end": 18}})
    kept = admin.update_crop("root", "eggplant", {**crop, "sell_price": 7, "limits": None})
    assert (kept["seasons"], kept["hour_start"], kept["hour_end"], kept["sell_price"]) == (["summer"], 6, 18, 7)
    cleared = admin.update_crop("root", "eggplant", {**crop, "limits": {}})
    assert (cleared["seasons"], cleared["hour_start"]) == ([], None)
    with pytest.raises(ValidationError):
        admin.update_crop("root", "eggplant", {**crop, "limits": {"hour_start": 3}})
