"""介紹網站：頁面、邀請連結、只回傳彙總統計（不含玩家資料）、快取與安全標頭。"""

from fastapi.testclient import TestClient

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
