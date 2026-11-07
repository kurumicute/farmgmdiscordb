"""介紹網站：頁面、邀請連結、只回傳彙總統計（不含玩家資料）、快取與安全標頭。"""

from fastapi.testclient import TestClient

from app.game import balance
from app.site.server import INVITE_PERMISSIONS, create_site_app
from tests.conftest import make_farmer


def client(session_factory, **kw):
    return TestClient(create_site_app(session_factory, **kw))


def test_index_and_security_headers(session_factory):
    res = client(session_factory).get("/")
    assert res.status_code == 200 and "/farm" in res.text and "/play" in res.text
    assert "default-src 'self'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"
    assert client(session_factory).get("/static/site.js").status_code == 200


def test_info_builds_invite_url(session_factory):
    info = client(session_factory, application_id=1558428174814154822, support_url="https://discord.gg/x").get(
        "/api/info").json()
    assert info["invite_url"] == (
        "https://discord.com/oauth2/authorize?client_id=1558428174814154822"
        f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}"
    )
    assert info["support_url"] == "https://discord.gg/x"
    assert client(session_factory).get("/api/info").json()["invite_url"] is None


def test_stats_are_aggregate_only_and_cached(service, session_factory):
    g = service.create_group("伺服器", "123456789012345678").id
    pid = make_farmer(service, "秘密名字", g)
    service.buy_seeds(pid, "wheat", 1)
    c = client(session_factory)
    stats = c.get("/api/stats").json()
    assert stats["players"] == 1 and stats["farms"] == 1 and stats["servers"] == 1
    assert isinstance(stats["actions_today"], int) and stats["last_activity"]  # 「今天」依資料庫的實際日期
    assert "秘密名字" not in c.get("/api/stats").text and "123456789012345678" not in c.get("/api/stats").text
    make_farmer(service, "第二人", g)
    assert c.get("/api/stats").json()["players"] == 1  # 30 秒快取內不重新查詢


def test_stats_unavailable_without_database():
    res = TestClient(create_site_app(None)).get("/api/stats")
    assert res.status_code == 503


def test_music_invite_and_no_cache(session_factory):
    c = client(session_factory, application_id=1, music_application_id=2)
    info = c.get("/api/info").json()
    assert "client_id=2" in info["music_invite_url"] and "client_id=1" in info["invite_url"]
    assert client(session_factory).get("/api/info").json()["music_invite_url"] is None
    page = c.get("/")
    assert page.headers["cache-control"] == "no-cache" and 'id="guide"' in page.text and "農場操作手冊" in page.text
    assert 'id="cave"' in page.text and "山洞冒險" in page.text


def test_feature_cards_have_guide_slots(session_factory):
    """首頁功能卡片的名稱與數字由 /api/guide 填入：頁面要有對應欄位，API 要有需要的資料。"""
    c = client(session_factory)
    page = c.get("/").text
    for slot in ("c-animals", "c-defenses", "c-areas", "c-machines", "c-rare-seeds", "c-transfer", "g-steal-day",
                 "g-area-example"):
        assert f'id="{slot}"' in page
    guide = c.get("/api/guide").json()
    assert guide["start"]["steal_plots_per_day"] == 1
    assert guide["start"]["rename_cost"] == balance.RENAME_FARM_COST
    assert any(i["kind"] == "tool" for i in guide["items"])
    assert any(not crop["shop_buyable"] for crop in guide["crops"])  # 稀有種子（琥珀瓜、月光花）


def test_guide_follows_game_settings_without_player_data(service, session_factory, clock):
    from app.services.admin_service import AdminService

    g = service.create_group("祕密伺服器", "123456789012345678").id
    make_farmer(service, "祕密農夫", g)
    admin = AdminService(session_factory, clock)
    admin.update_crop("admin", "carrot", {"name": "胡蘿蔔", "variant_name": "紫晶胡蘿蔔", "seed_price": 22,
                                          "grow_seconds": 14400, "harvest_quantity": 3, "sell_price": 25})
    admin.set_crop_active("admin", "tomato", False)
    res = client(session_factory).get("/api/guide")
    assert res.status_code == 200
    guide = res.json()
    carrot = next(c for c in guide["crops"] if c["name"] == "胡蘿蔔")
    assert carrot["grow_seconds"] == 14400 and carrot["seed_price"] == 22  # 後台調整後網站跟著變
    assert "番茄" not in [c["name"] for c in guide["crops"]]  # 停用的作物不列出
    assert {q["name"] for q in guide["qualities"]} == {"普通", "優良", "稀有", "傳說"}
    assert sum(q["chance_pct"] for q in guide["qualities"]) == 100
    assert guide["recipes"] and guide["defenses"] and guide["items"]
    x = guide["exploration"]
    assert [a["name"] for a in x["areas"]] == ["青青草原", "古老森林", "神秘礦坑"]
    assert {m["name"] for m in x["machines"]} == {"種子製造機", "自動收割機", "自動加工機", "稀有作物培育器"}
    assert all(sum(t["chance_pct"] for t in x["reward_types"][k]) == 100 for k in ("normal", "advanced"))
    assert x["areas"][0]["seeds"] == [{"name": "琥珀瓜種子", "min_level": 1}] and x["max_level"] == 13
    melon = next(c for c in guide["crops"] if c["name"] == "琥珀瓜")
    assert melon["shop_buyable"] is False  # 稀有作物：商店不販售種子
    assert "祕密農夫" not in res.text and "祕密伺服器" not in res.text and "123456789012345678" not in res.text


def test_guide_unavailable_without_database():
    assert TestClient(create_site_app(None)).get("/api/guide").status_code == 503


def test_info_exposes_music_settings_only(session_factory):
    info = client(session_factory, music_prefix_commands=True, music_prefix="?", music_playlist_limit=30,
                  music_idle_timeout=600, music_volume=0.25).get("/api/info").json()
    assert info["music"] == {"prefix_commands": True, "prefix": "?", "playlist_limit": 30,
                             "idle_minutes": 10.0, "default_volume_pct": 25}


def test_guide_has_fishing_rules_and_follows_admin_changes(session_factory, clock):
    from app.game import fishing
    from app.services.admin_service import AdminService

    admin = AdminService(session_factory, clock)
    c = client(session_factory)
    page = c.get("/").text
    assert 'id="fishing"' in page and "基礎釣魚成功率" in page and "分享到群組" in page
    f = c.get("/api/guide").json()["fishing"]
    assert [r["chance_pct"] for r in f["rarities"]] == [80, 15, 4, 1]
    assert [r["price"] for r in f["rarities"]] == [  # 依啟用中魚種的實際售價
        list(fishing.price_span([dict(zip(("rarity", "min_price", "max_price"), x[3:6], strict=True))
                                 for x in fishing.INITIAL_FISH], r)) for r in fishing.RARITIES]
    assert f["base_success_pct"] == 90 and len(f["gear"]) == len(fishing.INITIAL_GEAR)
    assert len(f["fish"]) == len(fishing.INITIAL_FISH)
    admin.set_fish_type_active("test", "golden_koi", False)
    admin.update_fishing_settings("test", {**fishing.DEFAULT_SETTINGS, "base_success_pct": 85})
    f2 = client(session_factory).get("/api/guide").json()["fishing"]  # 新的 app（沒有快取）
    assert f2["base_success_pct"] == 85 and "黃金錦鯉" not in [x["name"] for x in f2["fish"]]


def test_guide_rarity_price_follows_fish_prices(session_factory, clock):
    """魚的售價不受稀有度限制：手冊各稀有度的售價範圍依啟用中魚種的實際售價計算。"""
    from app.game import fishing
    from app.services.admin_service import AdminService

    admin = AdminService(session_factory, clock)
    code, name, emoji, rarity, _pmin, _pmax, wmin, wmax, diff, desc = next(
        x for x in fishing.INITIAL_FISH if x[3] == "common")
    admin.update_fish_type("test", code, {
        "name": name, "emoji": emoji, "rarity": rarity, "min_price": 0, "max_price": 123456,
        "min_weight_g": wmin, "max_weight_g": wmax, "difficulty": diff, "description": desc, "sort_order": 0})
    common = client(session_factory).get("/api/guide").json()["fishing"]["rarities"][0]
    assert common["code"] == "common" and common["price"] == [0, 123456]


def test_guide_lists_enabled_ranking_rules_only(session_factory, clock):
    from app.services.admin_service import AdminService

    admin = AdminService(session_factory, clock)
    admin.create_ranking_rule("admin", {
        "name": "每週群組販售王", "scope": "group", "metric": "sales", "period": "weekly",
        "rewards": [{"reward_type": "gold", "amount": 2000}, {"reward_type": "fertilizer", "amount": 3}],
    })
    off = admin.create_ranking_rule("admin", {
        "name": "停用的規則", "scope": "individual", "metric": "money", "period": "daily",
        "rewards": [{"reward_type": "gold", "amount": 100}],
    })
    admin.set_ranking_rule_enabled("admin", off["id"], False)
    r = client(session_factory).get("/api/guide").json()["rankings"]
    assert r["settle_grace_seconds"] > 0
    assert [x["name"] for x in r["rules"]] == ["每週群組販售王"]  # 停用的不列出
    rule = r["rules"][0]
    assert rule["scope"] == "group" and rule["metric"] == "農產品販售" and rule["period"] == "每週"
    assert rule["rewards"][0] == "2000 金幣" and len(rule["rewards"]) == 2
    page = client(session_factory).get("/").text
    assert 'id="g-rankings"' in page and "排名獎勵" in page


def test_guide_has_checkin_achievements_and_codex(session_factory):
    """手冊的簽到、成就與圖鑑表格由 /api/guide 填入，數量與規則模組一致，頁面預設表格也完整。"""
    from app.game import achievements, checkin

    c = client(session_factory)
    page = c.get("/").text
    for slot in ('id="progress"', 'id="g-checkin"', 'id="g-achievements"', 'id="g-codex-cats"', 'id="c-achievements"'):
        assert slot in page
    assert all(a.name in page for a in achievements.ACHIEVEMENTS)  # API 失敗時的預設表格
    p = c.get("/api/guide").json()["progress"]
    assert [x["day"] for x in p["checkin"]] == list(range(1, checkin.CYCLE_DAYS + 1))
    assert len(p["achievements"]) == len(achievements.ACHIEVEMENTS)
    assert any(a["title"] == "傳說農夫" for a in p["achievements"]) and "魚類" in p["codex"]


def test_guide_has_special_shop(session_factory):
    """特賣與升級：溫室價格、本週特賣與稱號商店由 /api/guide 提供，頁面有對應欄位。"""
    from app.game import special_shop

    c = client(session_factory)
    page = c.get("/").text
    for slot in ('id="special-shop"', 'id="g-weekly"', 'id="g-title-shop"', 'id="g-gh-prices"', "上一場排行榜"):
        assert slot in page
    sp = c.get("/api/guide").json()["special_shop"]
    assert sp["greenhouse"]["prices"] == list(special_shop.GREENHOUSE_PRICES)
    assert [t["title"] for t in sp["titles"]] == [t for t, _p in special_shop.TITLE_SHOP]
    offers = sp["weekly"]["offers"]
    assert len(offers) == special_shop.NORMAL_DEALS_PER_WEEK + 1 and sum(o["rare"] for o in offers) == 1


def test_guide_has_pets(session_factory):
    from app.game import pets

    c = client(session_factory)
    page = c.get("/").text
    assert 'id="g-pets"' in page and 'id="g-pet-ladder"' in page and "寵物（貓狗）" in page
    p = c.get("/api/guide").json()["pets"]
    assert p["buy_price"] == pets.BUY_PRICE and p["upgrade_prices"] == list(pets.UPGRADE_PRICES)
    assert len(p["pets"]) == len(pets.PETS)
    assert 'id="g-cat-odds"' in page and "50% 機率 +1" in page  # 預設表格：小麥滿級
    wheat = next(c for c in p["cat_odds"] if c["name"] == "小麥")
    assert len(wheat["levels"]) == pets.MAX_LEVEL + 1
    assert wheat["levels"][-1] == pets.odds_text(wheat["quantity"], pets.bonus_pct(pets.MAX_LEVEL))
